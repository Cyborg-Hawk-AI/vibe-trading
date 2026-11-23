"""Committee overlay compilation + S3 sync no-op behavior (offline-safe)."""

from autotrader.committee import compile_overlay
from autotrader.config import AwsConfig
from autotrader.s3_sync import sync_all


def test_compile_overlay_orders_by_dominance():
    text = compile_overlay({"premature_stop": 10, "fakeout_entry": 2, "illiquid": 1})
    assert "stops placed too close" in text
    assert text.index("stops placed too close") < text.index("spike bars")


def test_compile_overlay_empty():
    assert compile_overlay({}) == ""


def test_s3_sync_noop_without_bucket(monkeypatch):
    monkeypatch.delenv("AUTOTRADER_S3_BUCKET", raising=False)
    aws = AwsConfig()
    assert aws.enabled is False
    assert sync_all(aws) is True  # graceful no-op


def test_committee_skips_without_deps_or_key(monkeypatch, tmp_path):
    """review_finalist must degrade to a skip, never raise."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_LLM_PROVIDER", raising=False)
    from autotrader.committee import review_finalist
    from autotrader.registry import Registry

    registry = Registry(path=tmp_path / "reg.db")
    status = review_finalist(
        "gid1", "equities", {"windows": []}, {}, registry, symbols=["AAPL.US"]
    )
    assert status["status"] == "skipped"
    registry.close()
