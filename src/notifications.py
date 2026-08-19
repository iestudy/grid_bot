"""
Slack Incoming Webhookへの通知送信。

SLACK_WEBHOOK_URLが未設定の場合は、通知をスキップしてログに警告を出すのみとし、
botの取引ロジック自体には一切影響を与えない(通知は補助機能であり、
通知の成否がリスク管理判断を左右してはならない)。
"""

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class SlackNotifier:
    def __init__(self, webhook_url: Optional[str] = None):
        self.webhook_url = webhook_url or os.getenv("SLACK_WEBHOOK_URL")

    def _send(self, text: str) -> bool:
        if not self.webhook_url:
            logger.warning(f"SLACK_WEBHOOK_URL未設定のため通知をスキップ: {text[:50]}...")
            return False
        try:
            r = requests.post(self.webhook_url, json={"text": text}, timeout=5)
            r.raise_for_status()
            return True
        except Exception as e:
            # 通知失敗は取引ロジックを止める理由にはしない。ログだけ残す。
            logger.error(f"Slack通知送信に失敗: {e}")
            return False

    def notify_round_trip(self, buy_price: float, sell_price: float, amount: float, profit_jpy: float) -> bool:
        emoji = "🟢" if profit_jpy >= 0 else "🔴"
        text = (
            f"{emoji} グリッド往復決済\n"
            f"買値: {buy_price} / 売値: {sell_price} / 数量: {amount} XRP\n"
            f"実現損益: {profit_jpy:+.2f} 円"
        )
        return self._send(text)

    def notify_daily_summary(self, date_str: str, realized_profit_jpy: float, fill_count: int) -> bool:
        emoji = "📈" if realized_profit_jpy >= 0 else "📉"
        text = (
            f"{emoji} {date_str} の稼働サマリー\n"
            f"当日の実現損益: {realized_profit_jpy:+.2f} 円\n"
            f"当日の約定件数: {fill_count} 件"
        )
        return self._send(text)

    def notify_emergency(self, action: str, current_price: float, unrealized_pnl_jpy: float) -> bool:
        text = (
            f"🚨 緊急停止発動: {action}\n"
            f"現在価格: {current_price} / 含み損益: {unrealized_pnl_jpy:+.2f} 円\n"
            f"botは自動停止しました。人間のレビューが必要です。"
        )
        return self._send(text)
