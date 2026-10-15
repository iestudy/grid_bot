from datetime import datetime, timedelta, timezone

from src.trading_decision import apply_guardrails, pause_expiry, validate_decision

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def _d(mode="PAUSED", confidence="high", **kw):
    base = {"mode": mode, "reason": "下落が速い", "market_view": "下落", "resume_condition": "",
            "confidence": confidence, "next_review_minutes": 120}
    base.update(kw)
    return base


def _g(decision, current="ACTIVE", last_change=None, pause_start=None, last_pause_end=None):
    return apply_guardrails(decision, current, last_change, pause_start, last_pause_end, NOW)


def test_validate_rejects_bad_output():
    assert validate_decision(None) is None
    assert validate_decision({"mode": "HALT", "reason": "x"}) is None
    assert validate_decision({"mode": "PAUSED", "reason": "  "}) is None


def test_validate_clamps_review_minutes_and_defaults_confidence():
    r = validate_decision({"mode": "ACTIVE", "reason": "x", "next_review_minutes": 5, "confidence": "??"})
    assert r["next_review_minutes"] == 60 and r["confidence"] == "low"
    assert validate_decision({"mode": "ACTIVE", "reason": "x", "next_review_minutes": 9999})["next_review_minutes"] == 240


def test_low_confidence_pause_is_not_adopted():
    r = _g(_d(confidence="low"))
    assert r["mode"] == "ACTIVE" and r["overridden"]


def test_low_confidence_pause_keeps_existing_pause():
    assert _g(_d(confidence="low"), current="PAUSED")["mode"] == "PAUSED"


def test_change_within_two_hours_is_rejected():
    r = _g(_d(), current="ACTIVE", last_change=NOW - timedelta(minutes=30))
    assert r["mode"] == "ACTIVE" and r["overridden"]


def test_change_after_two_hours_is_accepted():
    assert _g(_d(), current="ACTIVE", last_change=NOW - timedelta(hours=3))["mode"] == "PAUSED"


def test_resuming_from_pause_is_not_rate_limited():
    r = _g(_d(mode="ACTIVE"), current="PAUSED", last_change=NOW - timedelta(minutes=10))
    assert r["mode"] == "ACTIVE"


def test_forced_active_after_pause_hit_limit():
    r = _g(_d(), current="ACTIVE", last_pause_end=NOW - timedelta(hours=2))
    assert r["mode"] == "ACTIVE" and r["overridden"]
    assert _g(_d(), current="ACTIVE", last_pause_end=NOW - timedelta(hours=7))["mode"] == "PAUSED"


def test_pause_expiry_is_based_on_start_not_renewed():
    start = NOW - timedelta(hours=10)
    assert pause_expiry(NOW, start) == start + timedelta(hours=24)
    assert pause_expiry(NOW, None) == NOW + timedelta(hours=24)


def _rec(hours_ago, mode, reason=None):
    r = {"at": (NOW - timedelta(hours=hours_ago)).isoformat(), "mode": mode}
    if reason:
        r["pause_end_reason"] = reason
    return r


def test_restore_state_pause_in_progress():
    from src.trading_decision import restore_state
    s = restore_state([_rec(30, "ACTIVE"), _rec(5, "PAUSED"), _rec(3, "PAUSED")], NOW)
    assert s["current_mode"] == "PAUSED"
    assert s["pause_started_at"] == NOW - timedelta(hours=5)
    assert s["last_change_at"] == NOW - timedelta(hours=5)
    assert s["last_pause_ended_at"] is None


def test_restore_state_forced_active_only_when_pause_hit_limit():
    from src.trading_decision import restore_state
    limit = restore_state([_rec(30, "PAUSED"), _rec(1, "ACTIVE", reason="limit")], NOW)
    assert limit["last_pause_ended_at"] == NOW - timedelta(hours=1)
    normal = restore_state([_rec(30, "PAUSED"), _rec(1, "ACTIVE")], NOW)
    assert normal["last_pause_ended_at"] is None  # 通常の再開では、ACTIVE強制をかけない


def test_restore_state_empty_history_is_active():
    from src.trading_decision import restore_state
    s = restore_state([], NOW)
    assert s["current_mode"] == "ACTIVE" and s["last_change_at"] is None


def test_pause_hit_limit():
    from src.trading_decision import pause_hit_limit
    assert pause_hit_limit(NOW - timedelta(hours=24), NOW) is True
    assert pause_hit_limit(NOW - timedelta(hours=23), NOW) is False
    assert pause_hit_limit(None, NOW) is False
