"""S3 artifact sync via the AWS CLI (the pipeline's only cloud footprint).

Creates the bucket on first use (name from ``AUTOTRADER_S3_BUCKET``), tags the
bucket AND every uploaded object with ``Project=Vibe-Trading``, and syncs:

  - ``autotrader/state/``   (registry DB, exports, run summaries)
  - promoted run artifacts under ``runs/autotrader/final_*``

Uses the CLI rather than boto3 so no extra Python dependency is needed and
credentials/config resolution matches the operator's ``aws`` setup exactly.
Everything is a no-op when ``AUTOTRADER_S3_BUCKET`` is unset.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path

from autotrader.bootstrap import RUNS_ROOT, state_dir
from autotrader.config import AwsConfig

logger = logging.getLogger(__name__)


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    logger.debug("aws: %s", " ".join(args))
    return subprocess.run(
        args, capture_output=True, text=True, check=check, timeout=600
    )


def aws_available() -> bool:
    return shutil.which("aws") is not None


def ensure_bucket(aws: AwsConfig) -> bool:
    """Create + tag the bucket if it doesn't exist. Returns success."""
    probe = _run(
        ["aws", "s3api", "head-bucket", "--bucket", aws.bucket], check=False
    )
    if probe.returncode != 0:
        create = ["aws", "s3api", "create-bucket", "--bucket", aws.bucket]
        # us-east-1 rejects an explicit LocationConstraint.
        if aws.region != "us-east-1":
            create += [
                "--region", aws.region,
                "--create-bucket-configuration",
                f"LocationConstraint={aws.region}",
            ]
        result = _run(create, check=False)
        if result.returncode != 0:
            logger.error("bucket create failed: %s", result.stderr.strip())
            return False
        logger.info("created s3://%s", aws.bucket)
    tag = _run(
        [
            "aws", "s3api", "put-bucket-tagging",
            "--bucket", aws.bucket,
            "--tagging", f"TagSet=[{{Key={aws.tag_key},Value={aws.tag_value}}}]",
        ],
        check=False,
    )
    if tag.returncode != 0:
        logger.warning("bucket tagging failed: %s", tag.stderr.strip())
    return True


def sync_tree(aws: AwsConfig, local: Path, prefix: str) -> bool:
    """``aws s3 sync`` a local tree, then tag every object under the prefix.

    ``s3 sync`` has no ``--tagging`` flag, so object tags are applied with a
    post-sync ``put-object-tagging`` pass over the listed keys — idempotent
    and cheap at this artifact volume.
    """
    if not local.exists():
        return True
    dest = f"s3://{aws.bucket}/{prefix}"
    sync = _run(["aws", "s3", "sync", str(local), dest, "--only-show-errors"], check=False)
    if sync.returncode != 0:
        logger.error("s3 sync failed for %s: %s", local, sync.stderr.strip())
        return False

    listing = _run(
        [
            "aws", "s3api", "list-objects-v2",
            "--bucket", aws.bucket,
            "--prefix", prefix,
            "--query", "Contents[].Key",
            "--output", "text",
        ],
        check=False,
    )
    keys = [k for k in listing.stdout.split() if k and k != "None"]
    tagging = f"TagSet=[{{Key={aws.tag_key},Value={aws.tag_value}}}]"
    failed = 0
    for key in keys:
        result = _run(
            [
                "aws", "s3api", "put-object-tagging",
                "--bucket", aws.bucket,
                "--key", key,
                "--tagging", tagging,
            ],
            check=False,
        )
        if result.returncode != 0:
            failed += 1
    if failed:
        logger.warning("object tagging failed for %d/%d keys", failed, len(keys))
    logger.info("synced %s -> %s (%d objects tagged)", local, dest, len(keys) - failed)
    return True


def sync_all(aws: AwsConfig | None = None) -> bool:
    """Sync state + promoted artifacts. Returns True when fully successful."""
    aws = aws or AwsConfig()
    if not aws.enabled:
        logger.info("AUTOTRADER_S3_BUCKET not set; skipping S3 sync")
        return True
    if not aws_available():
        logger.warning("aws CLI not found on PATH; skipping S3 sync")
        return False
    if not ensure_bucket(aws):
        return False

    ok = sync_tree(aws, state_dir(), "autotrader/state")
    for final_run in sorted(RUNS_ROOT.glob("final_*")):
        ok = sync_tree(aws, final_run, f"autotrader/runs/{final_run.name}") and ok
    return ok


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(0 if sync_all() else 1)
