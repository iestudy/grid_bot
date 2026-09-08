"""
インシデント対応結果をSlackに通知するCLIラッパー。

Claude Codeがインシデント対応(EMERGENCY_STOP後の自動復旧)完了後に
呼び出すことを想定している。src.notifications.SlackNotifierの薄いラッパーで、
コマンドライン引数からテキストを受け取りSlackへ送信するだけの役割に限定する
(Webhook URLをClaude Code側に直接渡さず、既存の検証済みnotifierモジュール
経由に限定するため)。

使い方:
    venv/bin/python3 scripts/notify_incident.py --type response --summary "..."
    venv/bin/python3 scripts/notify_incident.py --type escalation --summary "..."
"""
import argparse
import sys
from pathlib import Path


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
        # .envの値がクォートで囲まれている場合(例: KEY="value")は剥がす
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        env[key.strip()] = value
    return env


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=["response", "escalation"], required=True)
    parser.add_argument("--summary", required=True, help="通知本文(ランブックのフォーマットに沿ったテキスト)")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(project_root))

    env_vars = load_env(project_root / ".env")

    from src.notifications import SlackNotifier

    notifier = SlackNotifier(webhook_url=env_vars.get("SLACK_WEBHOOK_URL"))

    if args.type == "response":
        ok = notifier.notify_incident_response(args.summary)
    else:
        ok = notifier.notify_incident_escalation(args.summary)

    if not ok:
        print("通知送信に失敗しました(webhook未設定または送信エラー)。詳細はログを確認してください。", file=sys.stderr)
        sys.exit(1)

    print("通知を送信しました。")


if __name__ == "__main__":
    main()
