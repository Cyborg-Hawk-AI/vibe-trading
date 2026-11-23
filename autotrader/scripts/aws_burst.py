"""AWS spot-burst orchestrator for massive GA sweeps.

Local CLI:

    python -m autotrader.scripts.aws_burst push     # code+data -> S3
    python -m autotrader.scripts.aws_burst launch   # spot instance runs burst
    python -m autotrader.scripts.aws_burst status   # instance + spend state
    python -m autotrader.scripts.aws_burst merge    # S3 registry -> local db
    python -m autotrader.scripts.aws_burst run      # push+launch (--wait merges)

Cost guardrails: refuses to launch when the ``vibe-trading-compute`` monthly
budget ($100) is past --max-budget-pct, and the instance self-terminates
after the burst (or a hard 6h timeout), whichever comes first.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "autotrader" / "state"
BUCKET = "vibe-trading-ga-465768367549"
REGION = "us-east-1"
ACCOUNT = "465768367549"
BUDGET_NAME = "vibe-trading-compute"
INSTANCE_PROFILE = "vibe-trading-ga-profile"
DEFAULT_INSTANCE = "c7i.24xlarge"   # 96 vCPU / 192 GiB
AMI_SSM_PARAM = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
TAGS = [{"Key": "project", "Value": "vibe-trading"}, {"Key": "role", "Value": "ga-burst"}]

logger = logging.getLogger("autotrader.aws_burst")


def _session():
    return boto3.Session(region_name=REGION)


# ---------------------------------------------------------------------------
# push: code tarball + data snapshots -> S3
# ---------------------------------------------------------------------------

def _build_code_tarball(dest: Path) -> Path:
    tar_path = dest / "vibe-code.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        for rel in ("autotrader", "agent", "knowledge-base"):
            base = ROOT / rel
            if not base.exists():
                continue
            for p in base.rglob("*"):
                if not p.is_file():
                    continue
                parts = set(p.parts)
                if "__pycache__" in parts or ".git" in parts:
                    continue
                if p.suffix in (".pyc", ".pyo"):
                    continue
                if rel == "autotrader" and "state" in p.relative_to(base).parts:
                    continue  # state syncs separately
                tar.add(p, arcname=str(p.relative_to(ROOT)))
    return tar_path


def push() -> None:
    s3 = _session().client("s3")
    with tempfile.TemporaryDirectory() as tmp:
        tar_path = _build_code_tarball(Path(tmp))
        size_mb = tar_path.stat().st_size / 1e6
        logger.info("code tarball: %.1f MB", size_mb)
        s3.upload_file(str(tar_path), BUCKET, "code/vibe-code.tar.gz")
    logger.info("code uploaded; syncing data snapshots (incremental)")
    for interval in ("5m", "15m"):
        local = STATE / "data" / f"daytrade__{interval}"
        if local.exists():
            subprocess.run(
                ["aws", "s3", "sync", str(local), f"s3://{BUCKET}/data/daytrade__{interval}"],
                check=True,
            )
    seed = STATE / "daytrade_gate_passers_seed.json"
    if seed.exists():
        s3.upload_file(str(seed), BUCKET, "state/daytrade_gate_passers_seed.json")
    logger.info("push complete")


# ---------------------------------------------------------------------------
# launch: spot instance with self-terminating user-data
# ---------------------------------------------------------------------------

def _user_data(population: int, generations: int, workers: int) -> str:
    return f"""#!/bin/bash
set -x
exec > /var/log/ga-burst.log 2>&1
dnf install -y python3.11 python3.11-pip
mkdir -p /opt/vibe && cd /opt/vibe
aws s3 cp s3://{BUCKET}/code/vibe-code.tar.gz . && tar xzf vibe-code.tar.gz
mkdir -p autotrader/state/data
aws s3 sync s3://{BUCKET}/data/daytrade__5m autotrader/state/data/daytrade__5m
aws s3 sync s3://{BUCKET}/data/daytrade__15m autotrader/state/data/daytrade__15m
aws s3 cp s3://{BUCKET}/state/daytrade_gate_passers_seed.json autotrader/state/ || true
python3.11 -m venv .venv
.venv/bin/pip install -q numpy pandas scipy pyarrow scikit-learn pydantic pyyaml requests httpx tenacity boto3 defusedxml yfinance
RUN_ID=$(date +%Y%m%d-%H%M%S)
echo "burst run $RUN_ID starting"
timeout 6h .venv/bin/python -m autotrader.scripts.ga_burst_runner \\
    --population {population} --generations {generations} --workers {workers}
for f in daytrade_registry.db daytrade_registry_export.json burst_summary.json; do
  aws s3 cp autotrader/state/$f s3://{BUCKET}/runs/$RUN_ID/$f || true
done
echo "burst run $RUN_ID uploaded; terminating"
shutdown -h now
"""


def _budget_spend_pct() -> float | None:
    """Current month spend as % of the compute budget; None if unknown."""
    try:
        resp = _session().client("budgets").describe_budget(
            AccountId=ACCOUNT, BudgetName=BUDGET_NAME
        )
        b = resp["Budget"]
        limit = float(b["BudgetLimit"]["Amount"])
        spend = float(b["CalculatedSpend"]["ActualSpend"]["Amount"])
        return 100.0 * spend / limit if limit > 0 else None
    except Exception as exc:  # noqa: BLE001 — budgets API is best-effort
        logger.warning("budget check failed (%s); proceeding", exc)
        return None


def launch(instance_type: str, population: int, generations: int,
           workers: int, max_budget_pct: float) -> str:
    pct = _budget_spend_pct()
    if pct is not None and pct >= max_budget_pct:
        raise SystemExit(
            f"budget guard: {BUDGET_NAME} at {pct:.0f}% >= {max_budget_pct:.0f}% — refusing launch"
        )
    ec2 = _session().client("ec2")
    ssm = _session().client("ssm")
    ami = ssm.get_parameter(Name=AMI_SSM_PARAM)["Parameter"]["Value"]
    resp = ec2.run_instances(
        ImageId=ami,
        InstanceType=instance_type,
        MinCount=1,
        MaxCount=1,
        IamInstanceProfile={"Name": INSTANCE_PROFILE},
        InstanceMarketOptions={
            "MarketType": "spot",
            "SpotOptions": {"SpotInstanceType": "one-time"},
        },
        InstanceInitiatedShutdownBehavior="terminate",
        UserData=base64.b64encode(
            _user_data(population, generations, workers).encode()
        ).decode(),
        TagSpecifications=[{
            "ResourceType": "instance",
            "Tags": TAGS + [{"Key": "Name", "Value": "vibe-trading-ga-burst"}],
        }],
    )
    iid = resp["Instances"][0]["InstanceId"]
    logger.info("launched %s (%s, spot) — pop %d x gens %d x %d workers",
                iid, instance_type, population, generations, workers)
    return iid


# ---------------------------------------------------------------------------
# status / merge
# ---------------------------------------------------------------------------

def status() -> dict:
    ec2 = _session().client("ec2")
    resp = ec2.describe_instances(
        Filters=[{"Name": "tag:project", "Values": ["vibe-trading"]},
                 {"Name": "instance-state-name",
                  "Values": ["pending", "running", "stopping", "stopped"]}]
    )
    instances = [
        {
            "id": i["InstanceId"], "type": i["InstanceType"],
            "state": i["State"]["Name"], "launched": str(i.get("LaunchTime")),
        }
        for r in resp["Reservations"] for i in r["Instances"]
    ]
    pct = _budget_spend_pct()
    out = {"instances": instances, "budget_pct": pct}
    print(json.dumps(out, indent=2))
    return out


def _latest_run_prefix() -> str | None:
    s3 = _session().client("s3")
    resp = s3.list_objects_v2(Bucket=BUCKET, Prefix="runs/", Delimiter="/")
    prefixes = [p["Prefix"] for p in resp.get("CommonPrefixes", [])]
    return sorted(prefixes)[-1] if prefixes else None


def merge(run_prefix: str | None = None) -> dict:
    s3 = _session().client("s3")
    prefix = run_prefix or _latest_run_prefix()
    if not prefix:
        logger.warning("no burst runs found in s3://%s/runs/", BUCKET)
        return {"merged": 0}
    with tempfile.TemporaryDirectory() as tmp:
        remote_db = Path(tmp) / "remote_registry.db"
        s3.download_file(BUCKET, f"{prefix}daytrade_registry.db", str(remote_db))
        for name in ("burst_summary.json", "daytrade_registry_export.json"):
            try:
                s3.download_file(BUCKET, f"{prefix}{name}", str(STATE / f"burst_{name}"))
            except Exception:  # noqa: BLE001
                pass
        local_db = STATE / "daytrade_registry.db"
        conn = sqlite3.connect(local_db)
        counts: dict[str, int] = {}
        try:
            conn.execute("ATTACH DATABASE ? AS remote", (str(remote_db),))
            for table in ("strategies", "results"):
                before = conn.total_changes
                conn.execute(
                    f"INSERT OR IGNORE INTO {table} SELECT * FROM remote.{table}"
                )
                counts[table] = conn.total_changes - before
            conn.commit()
        finally:
            conn.close()
    logger.info("merged from %s: %s", prefix, counts)
    return {"run": prefix, "merged": counts}


def main() -> int:
    parser = argparse.ArgumentParser(description="AWS spot GA burst orchestrator")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("push")
    p_launch = sub.add_parser("launch")
    p_launch.add_argument("--instance-type", default=DEFAULT_INSTANCE)
    p_launch.add_argument("--population", type=int, default=256)
    p_launch.add_argument("--generations", type=int, default=32)
    p_launch.add_argument("--workers", type=int, default=48)
    p_launch.add_argument("--max-budget-pct", type=float, default=80.0)
    sub.add_parser("status")
    p_merge = sub.add_parser("merge")
    p_merge.add_argument("--run", default=None, help="S3 run prefix (default: latest)")
    p_run = sub.add_parser("run")
    p_run.add_argument("--instance-type", default=DEFAULT_INSTANCE)
    p_run.add_argument("--population", type=int, default=256)
    p_run.add_argument("--generations", type=int, default=32)
    p_run.add_argument("--workers", type=int, default=48)
    p_run.add_argument("--max-budget-pct", type=float, default=80.0)
    p_run.add_argument("--wait", action="store_true",
                       help="poll until the instance terminates, then merge")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.cmd == "push":
        push()
    elif args.cmd == "launch":
        launch(args.instance_type, args.population, args.generations,
               args.workers, args.max_budget_pct)
    elif args.cmd == "status":
        status()
    elif args.cmd == "merge":
        merge(args.run)
    elif args.cmd == "run":
        push()
        iid = launch(args.instance_type, args.population, args.generations,
                     args.workers, args.max_budget_pct)
        if args.wait:
            ec2 = _session().client("ec2")
            logger.info("waiting for %s to terminate...", iid)
            while True:
                time.sleep(60)
                state = ec2.describe_instances(InstanceIds=[iid])[
                    "Reservations"][0]["Instances"][0]["State"]["Name"]
                logger.info("instance %s: %s", iid, state)
                if state in ("terminated", "stopped"):
                    break
            time.sleep(30)  # let final S3 uploads land
            merge()
    return 0


if __name__ == "__main__":
    sys.exit(main())
