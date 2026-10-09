"""
エスカレーション後、botが停止したままの間、一定間隔で再通知するスクリプト。

背景: 2026-10-05(約2日)と10-09、自動対応の誤判定でエスカレーションされ、
botが停止したまま、通知が1回流れて終わり、人が気づくまで放置された。
エスカレーション通知のたびに run/escalation_pending.json が書かれ、
このスクリプトがsystemd timerから定期的に呼ばれて、停止が続く間だけ再通知する。

動作:
  - escalation_pending.jsonが無ければ、何もしない。
  - grid_bot.serviceが稼働中(active)なら、復旧したとみなして、ファイルを削除する。
  - 停止中で、前回の通知から REMIND_INTERVAL 以上経っていれば、再通知する。

使い方:
    venv/bin/python3 scripts/remind_escalation.py
"""
import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

REMIND_INTERVAL = timedelta(hours=6)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PENDING_PATH = PROJECT_ROOT / "run" / "escalation_pending.json"
JST = timezone(timedelta(hours=9))


def load_env(env_path: Path) -> dict:
    env = {}
    if not env_path.exists():
        return env
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        env[key.strip()] = value
    return env


def should_remind(pending: dict, now: datetime, interval: timedelta = REMIND_INTERVAL) -> bool:
    """前回の通知から interval 以上経っていれば True。"""
    last = datetime.fromisoformat(pending["last_notified_at"])
    return now - last >= interval


def _strip_prefix(headline: str) -> str:
    """見出しの「エスカレーション通知: 」を取り除く(再通知の文面で重複するため)。"""
    prefix = "エスカレーション通知: "
    return headline[len(prefix):] if headline.startswith(prefix) else headline


def build_reminder(pending: dict, now: datetime) -> str:
    escalated = datetime.fromisoformat(pending["escalated_at"])
    elapsed_hours = (now - escalated).total_seconds() / 3600
    count = pending.get("reminder_count", 0) + 1
    ts = now.astimezone(JST).strftime("%Y-%m-%d %H:%M JST")
    return (
        f"再通知: botは停止したままです(エスカレーションから約{elapsed_hours:.0f}時間経過)\n"
        f"{ts}\n"
        f"最初の通知: {_strip_prefix(pending.get('headline', '(不明)'))}\n"
        f"再通知は{count}回目です。復旧すると、この再通知は止まります。\n"
        f"状況の確認: venv/bin/python3 scripts/incident_check.py"
    )


def main():
    if not PENDING_PATH.exists():
        return

    status = subprocess.run(["systemctl", "is-active", "grid_bot"], capture_output=True, text=True)
    if status.stdout.strip() == "active":
        # 復旧した。再通知を止める。
        PENDING_PATH.unlink(missing_ok=True)
        print("grid_botが稼働中のため、再通知を終了しました。")
        return

    try:
        pending = json.loads(PENDING_PATH.read_text())
    except (ValueError, OSError) as e:
        print(f"escalation_pending.jsonの読み込みに失敗しました: {e}", file=sys.stderr)
        return

    now = datetime.now(timezone.utc)
    if not should_remind(pending, now):
        return

    env_vars = load_env(PROJECT_ROOT / ".env")
    sys.path.insert(0, str(PROJECT_ROOT))
    from src.notifications import SlackNotifier

    notifier = SlackNotifier(webhook_url=env_vars.get("SLACK_WEBHOOK_URL"))
    ok = notifier.notify_incident_escalation(build_reminder(pending, now))
    if ok:
        pending["last_notified_at"] = now.isoformat()
        pending["reminder_count"] = pending.get("reminder_count", 0) + 1
        PENDING_PATH.write_text(json.dumps(pending, ensure_ascii=False))
        print(f"再通知を送信しました(累計{pending['reminder_count']}回)。")
    else:
        print("再通知の送信に失敗しました。次回の実行で再試行します。", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
