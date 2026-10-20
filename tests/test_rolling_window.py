import os
os.environ.setdefault("TOKEN_ENC_KEY", "x" * 44)

from app.admin.routes import rolling_window_info
from app import quota_estimate as qe

NOW = 1792420000.0  # 2026-10-19 22:26:40 Asia/Taipei


def test_within_limit_has_reset_and_iso():
    w = rolling_window_info({"status": "within_limit", "rolling_pct": 13.7, "monthly_pct": 1.6,
                             "rolling_reset_at": int(NOW + 7980), "resume_at": None,
                             "monthly_reset_at": 1795096800, "window": "6h"}, now=NOW)
    assert w["rolling_reset_at"] == int(NOW + 7980)
    assert w["next_refresh_at"] == w["rolling_reset_at"]
    assert w["next_refresh_in_s"] == 7980
    assert w["rolling_reset_iso"].endswith("+08:00")
    assert w["rolling_reset_iso"].startswith("2026-10-20T00:39")
    assert w["limited"] is False and w["limited_by"] is None
    assert w["monthly_reset_iso"].endswith("+08:00")


def test_no_active_window_is_null():
    w = rolling_window_info({"status": "within_limit", "rolling_pct": 0.0, "rolling_reset_at": None}, now=NOW)
    assert w["rolling_reset_at"] is None and w["rolling_reset_iso"] is None
    assert w["next_refresh_at"] is None and w["next_refresh_in_s"] is None


def test_rate_limited_rolling_uses_resume_at():
    w = rolling_window_info({"status": "rate_limited", "limited_by": "rolling", "rolling_pct": 107.9,
                             "rolling_reset_at": int(NOW + 100), "resume_at": int(NOW + 16000)}, now=NOW)
    assert w["limited"] is True and w["limited_by"] == "rolling"
    assert w["next_refresh_at"] == int(NOW + 16000)
    assert w["resume_iso"].endswith("+08:00")


def test_billing_period_limit_keeps_rolling_reset():
    w = rolling_window_info({"status": "rate_limited", "limited_by": "billing_period",
                             "rolling_reset_at": int(NOW + 50), "resume_at": int(NOW + 86400 * 9)}, now=NOW)
    assert w["next_refresh_at"] == int(NOW + 50)


def test_missing_usage_safe():
    w = rolling_window_info(None, now=NOW)
    assert w["status"] is None and w["next_refresh_at"] is None and w["rolling_reset_iso"] is None


def test_quota_row_exposes_resume_at():
    d = qe.estimate([{"id": 1, "label": "a", "rate_limit": {"status": "rate_limited", "limited_by": "rolling",
                     "rolling_pct": 101, "rolling_reset_at": int(NOW + 10), "resume_at": int(NOW + 999)}}],
                    lambda label, ts: 0.0, lambda label: None, now=NOW)
    row = d["items"][0]
    assert row["resume_at"] == int(NOW + 999) and row["rolling_reset_at"] == int(NOW + 10)
