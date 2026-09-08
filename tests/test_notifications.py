from unittest.mock import patch, MagicMock

import pytest

from src.notifications import SlackNotifier


def test_send_skips_when_webhook_url_not_set():
    notifier = SlackNotifier(webhook_url=None)
    with patch("requests.post") as mock_post:
        result = notifier.notify_round_trip(100.0, 102.0, 8.0, 16.0)
    assert result is False
    mock_post.assert_not_called()


def test_notify_round_trip_sends_correct_payload():
    notifier = SlackNotifier(webhook_url="https://hooks.slack.com/dummy")
    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        result = notifier.notify_round_trip(100.0, 102.0, 8.0, 16.0)

    assert result is True
    args, kwargs = mock_post.call_args
    assert args[0] == "https://hooks.slack.com/dummy"
    assert "16.00" in kwargs["json"]["text"]
    assert "🟢" in kwargs["json"]["text"]  # 利益はプラスなので緑


def test_notify_round_trip_uses_red_emoji_for_loss():
    notifier = SlackNotifier(webhook_url="https://hooks.slack.com/dummy")
    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        notifier.notify_round_trip(160.0, 155.0, 8.0, -40.0)

    args, kwargs = mock_post.call_args
    assert "🔴" in kwargs["json"]["text"]


def test_send_failure_does_not_raise():
    notifier = SlackNotifier(webhook_url="https://hooks.slack.com/dummy")
    with patch("requests.post", side_effect=RuntimeError("network error")):
        result = notifier.notify_daily_summary("2026-08-19", 500.0, 12)
    assert result is False  # 例外を握りつぶし、Falseを返すのみ


def test_notify_emergency_sends_expected_content():
    notifier = SlackNotifier(webhook_url="https://hooks.slack.com/dummy")
    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        notifier.notify_emergency("FULL_CLOSE", current_price=140.0, unrealized_pnl_jpy=-3000.0)

    args, kwargs = mock_post.call_args
    assert "FULL_CLOSE" in kwargs["json"]["text"]
    assert "🚨" in kwargs["json"]["text"]


def test_notify_incident_response_sends_summary_with_marker():
    notifier = SlackNotifier(webhook_url="https://hooks.slack.com/dummy")
    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        notifier.notify_incident_response("トリガー: EMERGENCY_STOP\n実施内容: reset_state.py実行")

    args, kwargs = mock_post.call_args
    assert "インシデント自動対応" in kwargs["json"]["text"]
    assert "reset_state.py実行" in kwargs["json"]["text"]


def test_notify_incident_escalation_sends_summary_with_marker():
    notifier = SlackNotifier(webhook_url="https://hooks.slack.com/dummy")
    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        notifier.notify_incident_escalation("エスカレーション理由: 帳簿乖離が閾値超過")

    args, kwargs = mock_post.call_args
    assert "要人手対応" in kwargs["json"]["text"]
    assert "帳簿乖離が閾値超過" in kwargs["json"]["text"]
