import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

NOW = datetime.now(timezone.utc)


def _load():
    spec = importlib.util.spec_from_file_location(
        "decide_mod", Path(__file__).resolve().parent.parent / "scripts" / "decide_trading_mode.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _data():
    return {
        "balances": {"jpy": {"onhand_amount": 14000.0, "free_amount": 5000.0},
                     "xrp": {"onhand_amount": 50.0, "free_amount": 10.0}},
        "bot_state": {"base_price": 206.0, "cash_flow": 0.0, "net_inventory": 0.0},
        "current_price": 204.0,
        "active_orders": {"orders": [
            {"side": "buy", "price": "202", "remaining_amount": "8.9"},
            {"side": "sell", "price": "208", "remaining_amount": "8.5"}]},
        "config": {"grid_width_default_jpy": 1.0},
    }


def _setup(monkeypatch, tmp_path, argv, claude_mode="PAUSED", bot_active=True, history=None, mode_file=None):
    m = _load()
    calls = []

    class R:
        def __init__(self, out):
            self.stdout = out

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: R("active\n" if bot_active else "inactive\n"))
    monkeypatch.setattr(m, "load_env", lambda p: {"ANTHROPIC_API_KEY": "x"})
    monkeypatch.setattr(m, "gather_bot_state", lambda: _data())
    monkeypatch.setattr(m, "fetch_hourly", lambda *a, **k: [])
    monkeypatch.setattr(m, "ask_claude", lambda key, facts: {
        "mode": claude_mode, "reason": "テスト理由", "market_view": "v", "resume_condition": "r",
        "confidence": "high", "next_review_minutes": 120})

    hist = tmp_path / "history.jsonl"
    modef = tmp_path / "mode.json"
    for rec in (history or []):
        hist.parent.mkdir(parents=True, exist_ok=True)
        with open(hist, "a") as f:
            f.write(json.dumps(rec) + chr(10))
    if mode_file:
        modef.write_text(json.dumps(mode_file))

    import src.trading_apply as ta
    monkeypatch.setattr(m, "load_history", lambda: ta.load_history(hist))
    monkeypatch.setattr(m, "append_history", lambda rec: (calls.append("append_history"), ta.append_history(rec, hist)))
    monkeypatch.setattr(m, "write_mode_file",
                        lambda mode, reason, state, now: (calls.append("write_mode_file"), ta.write_mode_file(mode, reason, state, now, modef)))
    monkeypatch.setattr(m, "cancel_buys_and_report", lambda env: (calls.append("cancel_buys"), "")[1])
    import src.trading_mode as tm
    monkeypatch.setattr(m, "read_trading_mode", lambda: tm.read_trading_mode(modef, datetime.now(timezone.utc)))
    monkeypatch.setattr(m, "DRYRUN_LOG", tmp_path / "dryrun.jsonl")
    monkeypatch.setattr(sys, "argv", ["decide_trading_mode.py", "--no-slack"] + argv)
    return m, calls, hist, modef


def test_without_apply_writes_nothing_and_cancels_nothing(monkeypatch, tmp_path):
    m, calls, hist, modef = _setup(monkeypatch, tmp_path, [])
    m.main()
    assert calls == [] and not hist.exists() and not modef.exists()


def test_apply_writes_mode_file_before_cancelling_buys(monkeypatch, tmp_path):
    m, calls, hist, modef = _setup(monkeypatch, tmp_path, ["--apply"])
    m.main()
    assert calls == ["append_history", "write_mode_file", "cancel_buys"]
    assert json.loads(modef.read_text())["mode"] == "PAUSED"


def test_already_paused_does_not_cancel_again(monkeypatch, tmp_path):
    started = NOW - timedelta(hours=5)
    exp = (started + timedelta(hours=24)).isoformat()
    m, calls, hist, modef = _setup(
        monkeypatch, tmp_path, ["--apply"],
        history=[{"at": (NOW - timedelta(hours=8)).isoformat(), "mode": "ACTIVE"},
                 {"at": started.isoformat(), "mode": "PAUSED"}],
        mode_file={"mode": "PAUSED", "expires_at": exp})
    m.main()
    assert "cancel_buys" not in calls
    assert json.loads(modef.read_text())["expires_at"] == exp  # 期限は延長されない


def test_pause_past_24h_returns_to_active_and_records_limit(monkeypatch, tmp_path):
    started = NOW - timedelta(hours=25)
    m, calls, hist, modef = _setup(
        monkeypatch, tmp_path, ["--apply"], claude_mode="PAUSED",
        history=[{"at": (NOW - timedelta(hours=30)).isoformat(), "mode": "ACTIVE"},
                 {"at": started.isoformat(), "mode": "PAUSED"}],
        mode_file={"mode": "PAUSED", "expires_at": (started + timedelta(hours=24)).isoformat()})
    m.main()
    last = [json.loads(l) for l in hist.read_text().splitlines()][-1]
    assert last["mode"] == "ACTIVE" and last.get("pause_end_reason") == "limit"
    assert "cancel_buys" not in calls


def test_bot_stopped_does_nothing(monkeypatch, tmp_path):
    m, calls, hist, modef = _setup(monkeypatch, tmp_path, ["--apply"], bot_active=False)
    m.main()
    assert calls == [] and not hist.exists() and not modef.exists()


def test_failure_notice_is_rate_limited(monkeypatch, tmp_path):
    m = _load()
    sent = []

    class FakeSlack:
        def __init__(self, webhook_url=None):
            pass

        def _send(self, text):
            sent.append(text)

    import src.notifications as n
    monkeypatch.setattr(n, "SlackNotifier", FakeSlack)
    monkeypatch.setattr(m, "FAILURE_NOTICE", tmp_path / "notice.json")
    args = type("A", (), {"no_slack": False})()
    m.notify_failure({}, "Claude API失敗", "boom", args)
    m.notify_failure({}, "Claude API失敗", "boom", args)   # 6時間以内: 送らない
    m.notify_failure({}, "Claude出力が不正", "bad", args)  # 別の種類: 送る
    assert len(sent) == 2
