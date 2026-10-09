import importlib.util
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _load():
    spec = importlib.util.spec_from_file_location(
        "remind_escalation_mod",
        Path(__file__).resolve().parent.parent / "scripts" / "remind_escalation.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pending(hours_ago_notified, hours_ago_escalated=None, count=0):
    now = datetime.now(timezone.utc)
    escalated = hours_ago_escalated if hours_ago_escalated is not None else hours_ago_notified
    return {
        "escalated_at": (now - timedelta(hours=escalated)).isoformat(),
        "last_notified_at": (now - timedelta(hours=hours_ago_notified)).isoformat(),
        "reminder_count": count,
        "headline": "エスカレーション通知: テスト",
    }


def test_should_remind_boundary():
    m = _load()
    now = datetime.now(timezone.utc)
    assert m.should_remind(_pending(7), now) is True
    assert m.should_remind(_pending(2), now) is False


def test_reminder_text_has_real_newlines_not_escaped():
    """改行が、バックスラッシュ+nの文字として出ないこと(過去に二重エスケープで混入した)。"""
    m = _load()
    text = m.build_reminder(_pending(7), datetime.now(timezone.utc))
    assert "\n" in text
    assert "\\n" not in text
    assert "再通知は1回目" in text


def _run_main(m, tmp_path, monkeypatch, pending, bot_status):
    path = tmp_path / "run" / "escalation_pending.json"
    monkeypatch.setattr(m, "PENDING_PATH", path)
    if pending is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(pending))
    notifier = MagicMock()
    notifier.notify_incident_escalation.return_value = True
    fake_run = MagicMock(return_value=MagicMock(stdout=bot_status + "\n"))
    with patch.object(m.subprocess, "run", fake_run), \
         patch("src.notifications.SlackNotifier", return_value=notifier):
        m.main()
    return path, notifier


def test_main_does_nothing_without_pending_file(tmp_path, monkeypatch):
    m = _load()
    path, notifier = _run_main(m, tmp_path, monkeypatch, None, "inactive")
    notifier.notify_incident_escalation.assert_not_called()


def test_main_clears_pending_when_bot_is_active(tmp_path, monkeypatch):
    """復旧した(botが稼働中)なら、再通知せず、状態ファイルを削除する。"""
    m = _load()
    path, notifier = _run_main(m, tmp_path, monkeypatch, _pending(7), "active")
    assert not path.exists()
    notifier.notify_incident_escalation.assert_not_called()


def test_main_does_not_remind_before_interval(tmp_path, monkeypatch):
    m = _load()
    path, notifier = _run_main(m, tmp_path, monkeypatch, _pending(2), "inactive")
    notifier.notify_incident_escalation.assert_not_called()
    assert path.exists()


def test_main_reminds_and_updates_state_after_interval(tmp_path, monkeypatch):
    m = _load()
    path, notifier = _run_main(m, tmp_path, monkeypatch, _pending(7, count=1), "inactive")
    notifier.notify_incident_escalation.assert_called_once()
    saved = json.loads(path.read_text())
    assert saved["reminder_count"] == 2
    # 通知後は last_notified_at が更新され、直後の再実行では再通知されない
    assert m.should_remind(saved, datetime.now(timezone.utc)) is False


def test_main_keeps_state_when_send_fails(tmp_path, monkeypatch):
    """送信に失敗したら、状態を更新せず、次回の実行で再試行する。"""
    m = _load()
    path = tmp_path / "run" / "escalation_pending.json"
    monkeypatch.setattr(m, "PENDING_PATH", path)
    path.parent.mkdir(parents=True)
    original = _pending(7)
    path.write_text(json.dumps(original))
    notifier = MagicMock()
    notifier.notify_incident_escalation.return_value = False
    with patch.object(m.subprocess, "run", MagicMock(return_value=MagicMock(stdout="inactive\n"))), \
         patch("src.notifications.SlackNotifier", return_value=notifier):
        with pytest.raises(SystemExit):
            m.main()
    assert json.loads(path.read_text()) == original


def _load_incident_response():
    spec = importlib.util.spec_from_file_location(
        "run_incident_response_mod",
        Path(__file__).resolve().parent.parent / "scripts" / "run_incident_response.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_record_escalation_pending_writes_headline(tmp_path, monkeypatch):
    m = _load_incident_response()
    path = tmp_path / "run" / "escalation_pending.json"
    monkeypatch.setattr(m, "ESCALATION_PENDING_PATH", path)
    m._record_escalation_pending("エスカレーション通知: テスト\n2026-10-09\n詳細")
    saved = json.loads(path.read_text())
    assert saved["headline"] == "エスカレーション通知: テスト"
    assert saved["reminder_count"] == 0


def test_notify_records_pending_only_for_real_escalation(tmp_path, monkeypatch):
    """escalationのときだけ記録し、responseや--dry-runでは記録しない。"""
    m = _load_incident_response()
    path = tmp_path / "run" / "escalation_pending.json"
    monkeypatch.setattr(m, "ESCALATION_PENDING_PATH", path)
    monkeypatch.setattr(m, "run", MagicMock())  # notify_incident.pyの実行を止める

    monkeypatch.setattr(m, "DRY_RUN", False)
    m.notify("response", "自動復旧通知: テスト")
    assert not path.exists()

    monkeypatch.setattr(m, "DRY_RUN", True)
    m.notify("escalation", "エスカレーション通知: dry-run")
    assert not path.exists()

    monkeypatch.setattr(m, "DRY_RUN", False)
    m.notify("escalation", "エスカレーション通知: 本番")
    assert path.exists()


def test_reminder_does_not_repeat_escalation_prefix():
    m = _load()
    text = m.build_reminder(_pending(7), datetime.now(timezone.utc))
    assert "最初の通知: テスト" in text
    assert "最初の通知: エスカレーション通知:" not in text
