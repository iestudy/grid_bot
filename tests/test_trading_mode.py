import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src.trading_mode import exclude_buys_if_paused, read_trading_mode

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def _write(tmp_path, **kw):
    p = tmp_path / "trading_mode.json"
    p.write_text(json.dumps(kw))
    return p


def test_missing_file_is_active(tmp_path):
    assert read_trading_mode(tmp_path / "none.json", NOW) == "ACTIVE"


def test_paused_before_expiry(tmp_path):
    p = _write(tmp_path, mode="PAUSED", expires_at=(NOW + timedelta(hours=1)).isoformat())
    assert read_trading_mode(p, NOW) == "PAUSED"


def test_paused_after_expiry_returns_to_active(tmp_path):
    p = _write(tmp_path, mode="PAUSED", expires_at=(NOW - timedelta(seconds=1)).isoformat())
    assert read_trading_mode(p, NOW) == "ACTIVE"


def test_paused_without_expiry_is_invalid(tmp_path):
    """期限のないPAUSEDは、止め続けるおそれがあるため無効(ACTIVE)。"""
    assert read_trading_mode(_write(tmp_path, mode="PAUSED"), NOW) == "ACTIVE"


def test_unknown_mode_and_broken_json_are_active(tmp_path):
    assert read_trading_mode(_write(tmp_path, mode="HALT", expires_at=NOW.isoformat()), NOW) == "ACTIVE"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert read_trading_mode(bad, NOW) == "ACTIVE"


def test_exclude_buys_only_when_paused():
    levels = [SimpleNamespace(side="buy"), SimpleNamespace(side="sell")]
    assert [l.side for l in exclude_buys_if_paused(levels, "PAUSED")] == ["sell"]
    assert len(exclude_buys_if_paused(levels, "ACTIVE")) == 2
    assert len(exclude_buys_if_paused(levels, "CAUTIOUS")) == 2
