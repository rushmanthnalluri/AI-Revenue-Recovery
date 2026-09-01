"""Export endpoint timestamps are tz-aware UTC (no deprecated naive utcnow)."""

from datetime import datetime


def test_export_summary_generated_at_is_tz_aware_z(client):
    r = client.get("/api/v1/export/summary")
    assert r.status_code == 200
    ts = r.json()["generated_at"]
    assert ts.endswith("Z")
    parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


def test_export_filename_timestamp_is_utc_z(client):
    r = client.get("/api/v1/export/audit")
    assert r.status_code == 200
    disposition = r.headers["Content-Disposition"]
    # attachment; filename="audit_real_test_20260901T071403Z.csv"
    name = disposition.split('filename="')[1].rstrip('"')
    stamp = name.removesuffix(".csv").split("_")[-1]
    parsed = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ")
    assert parsed.year >= 2026
