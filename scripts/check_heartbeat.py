"""
grid_botのハートビートファイル(run/heartbeat.flag)を確認し、
最終更新から一定時間以上経過していれば「ハングしている」と判断して
bot再起動とSlack通知を行うスクリプト。

背景: 2026-09、run_loop.pyが原因不明のハング(WebSocket接続の固着等が
疑われるが未特定)を起こし、reconcile処理が9日間停止したまま
気づかれずに放置される事態が発生した。この間に発生した約定は
Slackに通知されず、実質的にリスク管理・監視が機能していない状態だった。

このスクリプトはsystemd timer(grid_bot_healthcheck.timer)から
定期的に(推奨: 2分おき)起動されることを想定している。

使い方:
    venv/bin/python3 scripts/check_heartbeat.py
"""
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ハートビートがこの時間以上更新されていなければ異常とみなす。
# poll_interval_sec(通常10秒)に対して十分な余裕を持たせた値。
STALE_THRESHOLD = timedelta(minutes=5)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HEARTBEAT_PATH = PROJECT_ROOT / "run" / "heartbeat.flag"
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


def main():
    # grid_bot.serviceがそもそも稼働していなければ(EMERGENCY_STOP後の
    # 待機状態、意図的な停止、インシデント対応中等)、ハートビート監視の
    # 対象外とする。ここで再起動してしまうと、意図的な停止や
    # インシデント対応中のbotを不用意に動かしてしまう。
    status = subprocess.run(
        ["systemctl", "is-active", "grid_bot"],
        capture_output=True, text=True,
    )
    if status.stdout.strip() != "active":
        print(f"grid_bot.serviceは稼働していません(状態: {status.stdout.strip()})。ハートビート監視をスキップします。")
        return

    if not HEARTBEAT_PATH.exists():
        print("ハートビートファイルが存在しません。起動直後の可能性があるためスキップします。")
        return

    try:
        last_heartbeat = datetime.fromisoformat(HEARTBEAT_PATH.read_text().strip())
    except (ValueError, OSError) as e:
        print(f"ハートビートファイルの読み込みに失敗しました: {e}")
        return

    now = datetime.now(timezone.utc)
    age = now - last_heartbeat

    if age <= STALE_THRESHOLD:
        # 正常。何もしない(ログにも残さず、通知もしない。定常状態でのノイズを避ける)。
        return

    # --- 異常検知: 再起動する ---
    timestamp = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    age_minutes = age.total_seconds() / 60

    env_vars = load_env(PROJECT_ROOT / ".env")
    sys.path.insert(0, str(PROJECT_ROOT))
    from src.notifications import SlackNotifier
    notifier = SlackNotifier(webhook_url=env_vars.get("SLACK_WEBHOOK_URL"))

    notifier._send(
        f"🩺 ヘルスチェック通知: botのハングを検知したため再起動します\n"
        f"{timestamp}\n"
        f"最終ハートビート: {last_heartbeat.isoformat()} ({age_minutes:.1f}分前)\n"
        f"閾値: {STALE_THRESHOLD.total_seconds() / 60:.0f}分"
    )

    restart_result = subprocess.run(
        ["sudo", "systemctl", "restart", "grid_bot"],
        capture_output=True, text=True,
    )

    if restart_result.returncode == 0:
        notifier._send(
            f"🩺 ヘルスチェック通知: bot再起動が完了しました\n"
            f"{timestamp}\n"
            f"再起動前のハング時間: 約{age_minutes:.0f}分\n"
            f"実注文・帳簿の状態確認を推奨します(scripts/incident_check.py)。"
        )
        print(f"bot再起動完了(ハング検知から{age_minutes:.1f}分)")
    else:
        notifier._send(
            f"🩺 ヘルスチェック通知: bot再起動に失敗しました。手動対応が必要です\n"
            f"{timestamp}\n"
            f"エラー: {restart_result.stderr}"
        )
        print(f"bot再起動に失敗: {restart_result.stderr}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
