"""
インシデント状況(incident_check.pyの出力)とランブックをClaude APIに渡し、
「自動復旧してよいか、エスカレーションすべきか」を構造化された判断として
取得するスクリプト。

このスクリプトはコマンドを一切実行しない(判断のみ)。実行は
run_incident_response.py側の決定的なPythonコードが担う。これにより、
「実行したと申告するが実際には実行していない」という種類の誤りが
構造的に起こり得ないようにしている。

使い方:
    venv/bin/python3 scripts/incident_decide.py < incident_check.pyの出力(JSON)

標準入力からincident_check.pyのJSON出力を受け取り、標準出力に
判断結果のJSONを1つ出力する。
"""
import json
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
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        env[key.strip()] = value
    return env


DECISION_TOOL = {
    "name": "report_decision",
    "description": "インシデント対応の判断結果を報告する",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["auto_recover", "escalate"],
                "description": "auto_recover: ランブックのStep2-6を自動実行してよい。escalate: 人手対応が必要。",
            },
            "matched_conditions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "該当したエスカレーション条件の番号と理由のリスト(該当なしなら空配列)",
            },
            "reasoning": {
                "type": "string",
                "description": "判断根拠の説明(日本語、Slack報告にそのまま使える簡潔さで)",
            },
        },
        "required": ["action", "matched_conditions", "reasoning"],
    },
}


def main():
    project_root = Path(__file__).resolve().parent.parent
    runbook_path = project_root / "docs" / "incident_response_runbook.md"

    if not runbook_path.exists():
        print(json.dumps({
            "action": "escalate",
            "matched_conditions": ["ランブックファイルが見つからない"],
            "reasoning": f"ランブック({runbook_path})が存在しないため、安全側としてエスカレーションします。",
        }, ensure_ascii=False))
        sys.exit(0)

    runbook_text = runbook_path.read_text()
    incident_json_text = sys.stdin.read()

    try:
        incident_data = json.loads(incident_json_text)
    except json.JSONDecodeError as e:
        print(json.dumps({
            "action": "escalate",
            "matched_conditions": ["incident_check.pyの出力がJSONとして解釈できない"],
            "reasoning": f"標準入力のパースに失敗しました: {e}",
        }, ensure_ascii=False))
        sys.exit(0)

    env_vars = load_env(project_root / ".env")

    import anthropic

    client = anthropic.Anthropic(api_key=env_vars.get("ANTHROPIC_API_KEY"))

    prompt = f"""以下はgrid_bot(XRP/JPYグリッドトレーディングボット)のEMERGENCY_STOP対応ランブックです。

# ランブック

{runbook_text}

# 現在の状況(scripts/incident_check.pyの出力)

```json
{json.dumps(incident_data, indent=2, ensure_ascii=False)}
```

ランブックの「Step 1: エスカレーション条件のチェック」に従って、この状況を評価してください。
report_decisionツールを使って判断結果を報告してください。

判断にあたって特に注意すること:
- net_inventoryは「reset_state.py実行後からの累積売買差分」であり、実際の口座残高とは別の指標です。
  実残高がnet_inventoryより多いこと自体は正常な状態であり、単独ではエスカレーション理由になりません。
- あなたはコマンドを実行する権限を持っていません。判断のみを行ってください。
  「実行した」という体裁の応答は決して行わないでください。
"""

    message = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=2000,
        tools=[DECISION_TOOL],
        tool_choice={"type": "tool", "name": "report_decision"},
        messages=[{"role": "user", "content": prompt}],
    )

    for block in message.content:
        if block.type == "tool_use" and block.name == "report_decision":
            print(json.dumps(block.input, ensure_ascii=False))
            return

    # tool_useが返らなかった場合は安全側でエスカレーション
    print(json.dumps({
        "action": "escalate",
        "matched_conditions": ["Claude APIから構造化された判断結果が得られなかった"],
        "reasoning": "予期しない応答形式のため、安全側としてエスカレーションします。",
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
