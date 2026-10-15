import json
from datetime import datetime, timedelta, timezone

from src.trading_apply import (
    append_history, format_cancel_report, load_history, write_mode_file,
)
from src.trading_mode import read_trading_mode

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def test_history_roundtrip_and_skips_broken_lines(tmp_path):
    p = tmp_path / "h.jsonl"
    append_history({"at": NOW.isoformat(), "mode": "ACTIVE"}, p)
    p.write_text(p.read_text() + "{broken" + chr(10))
    append_history({"at": NOW.isoformat(), "mode": "PAUSED"}, p)
    assert [r["mode"] for r in load_history(p)] == ["ACTIVE", "PAUSED"]


def test_load_history_missing_file_is_empty(tmp_path):
    assert load_history(tmp_path / "none.jsonl") == []


def test_new_pause_expires_24h_from_now(tmp_path):
    p = tmp_path / "m.json"
    state = {"current_mode": "ACTIVE", "pause_started_at": None}
    write_mode_file("PAUSED", "理由", state, NOW, p)
    assert json.loads(p.read_text())["expires_at"] == (NOW + timedelta(hours=24)).isoformat()


def test_continuing_pause_does_not_extend_expiry(tmp_path):
    p = tmp_path / "m.json"
    started = NOW - timedelta(hours=10)
    state = {"current_mode": "PAUSED", "pause_started_at": started}
    write_mode_file("PAUSED", "理由", state, NOW, p)
    assert json.loads(p.read_text())["expires_at"] == (started + timedelta(hours=24)).isoformat()


def test_written_mode_file_is_read_back_by_bot_reader(tmp_path):
    """適用側の書式を、bot側の読み取り(read_trading_mode)が解釈できること。"""
    p = tmp_path / "m.json"
    write_mode_file("PAUSED", "理由", {"current_mode": "ACTIVE", "pause_started_at": None}, NOW, p)
    assert read_trading_mode(p, NOW + timedelta(hours=1)) == "PAUSED"
    assert read_trading_mode(p, NOW + timedelta(hours=25)) == "ACTIVE"  # 期限後は自動でACTIVE


def test_cancel_report_lists_succeeded_and_failed():
    r = {"succeeded": [{"order_id": 1, "price": "200", "amount": "8.9"}],
         "failed": [{"order_id": 2, "price": "199", "amount": "8.9"}]}
    text = format_cancel_report(r)
    assert "成功1件 / 失敗1件" in text and "取消済み: 8.9 XRP @ 200円" in text and "取り消せず" in text
