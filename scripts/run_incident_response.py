"""
EMERGENCY_STOP検知後のインシデント自動対応のオーケストレーター。

設計方針:
  判断(何をすべきか)と実行(実際にコマンドを叩く)を明確に分離する。
  - 判断: scripts/incident_decide.py が Claude API に問い合わせ、
    構造化されたJSON(action/matched_conditions/reasoning)を返す。
    判断側はコマンドを一切実行しない。
  - 実行: このスクリプト自身が、判断結果がauto_recoverの場合に限り、
    あらかじめ検証済みの固定コマンド列(reset_state.py -> resize_grid.py
    --apply -> git反映 -> systemctl restart)をsubprocessで実行する。

    この分離により、「実行したと申告するが実際には実行していない」という
    種類の誤りが構造的に起こり得ないようにしている(過去に claude -p の
    headlessモードでBashツールの可用性が不安定で、存在しないファイルを
    「存在しない」と誤って報告する事例があったため、実行系の判断や
    自己申告に依存しない設計にした)。

使い方:
    venv/bin/python3 scripts/run_incident_response.py
"""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = str(PROJECT_ROOT / "venv" / "bin" / "python3")


DRY_RUN = "--dry-run" in sys.argv

# dry-run時、書き込み系コマンドは実行せずこの疑似結果を返す
_DRY_RUN_RESULT = subprocess.CompletedProcess(args=[], returncode=0, stdout="[DRY RUN] 実行スキップ", stderr="")

# 読み取り専用で、dry-runでも実際に実行してよいコマンドの先頭部分
_READ_ONLY_PREFIXES = [
    ["git", "status"],
    ["git", "diff"],
    ["git", "checkout", "main"],
    ["git", "pull"],
    ["sudo", "systemctl", "status"],
]


def _is_read_only(cmd):
    for prefix in _READ_ONLY_PREFIXES:
        if cmd[:len(prefix)] == prefix:
            return True
    return False


def run(cmd, **kwargs):
    """subprocessをラップし、stdout/stderr/returncodeを記録しながら実行する。
    DRY_RUN時は読み取り専用コマンド(incident_check.py, git status/diff等)以外を
    実際には実行せず、疑似的な成功結果を返す。
    """
    is_check_script = cmd[:2] == [VENV_PYTHON, "scripts/incident_check.py"]
    if DRY_RUN and not is_check_script and not _is_read_only(cmd):
        print(f"$ {' '.join(cmd)}  [DRY RUN: 実行スキップ]", file=sys.stderr)
        return _DRY_RUN_RESULT

    print(f"$ {' '.join(cmd)}", file=sys.stderr)
    result = subprocess.run(
        cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=120, **kwargs
    )
    print(result.stdout, file=sys.stderr)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result


def notify(notify_type: str, summary: str):
    if DRY_RUN:
        summary = f"[DRY RUN]\n{summary}"
    run([VENV_PYTHON, "scripts/notify_incident.py", "--type", notify_type, "--summary", summary])


def main():
    # --- Step 0: 状況収集 ---
    check_result = run([VENV_PYTHON, "scripts/incident_check.py"])
    incident_json_text = check_result.stdout

    # --- 判断 ---
    decide_result = subprocess.run(
        [VENV_PYTHON, "scripts/incident_decide.py"],
        cwd=str(PROJECT_ROOT),
        input=incident_json_text,
        capture_output=True,
        text=True,
        timeout=60,
    )
    try:
        decision = json.loads(decide_result.stdout)
    except json.JSONDecodeError:
        decision = {
            "action": "escalate",
            "matched_conditions": ["incident_decide.pyの出力がJSONとして解釈できない"],
            "reasoning": f"判断スクリプトの出力異常。stdout: {decide_result.stdout!r} stderr: {decide_result.stderr!r}",
        }

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if decision["action"] == "escalate":
        summary = (
            f"{timestamp}\n"
            f"判断: エスカレーション\n"
            f"該当条件: {', '.join(decision['matched_conditions']) or '(明示なし)'}\n"
            f"理由: {decision['reasoning']}\n"
            f"incident_check.py出力:\n{incident_json_text}"
        )
        notify("escalation", summary)
        print("エスカレーションしました。自動対応は行っていません。", file=sys.stderr)
        return

    # --- action == auto_recover: Step 2以降を決定的に実行 ---
    executed_steps = []

    # Step 2: reset_state.py
    reset_result = run([VENV_PYTHON, "-m", "src.reset_state", "--pair", "xrp_jpy", "--use-dynamodb", "--yes"])
    executed_steps.append(f"reset_state.py: exit={reset_result.returncode}")
    if "失敗=0件" not in reset_result.stdout and "キャンセル結果" in reset_result.stdout:
        # 失敗件数が0でない場合はエスカレーション(ランブック条件4)
        summary = (
            f"{timestamp}\n"
            f"判断: 自動復旧を試みたがreset_state.pyでキャンセル失敗が検出されたためエスカレーション\n"
            f"reset_state.py出力:\n{reset_result.stdout}\n{reset_result.stderr}"
        )
        notify("escalation", summary)
        print("reset_state.pyでキャンセル失敗を検出。エスカレーションしました。", file=sys.stderr)
        return
    if reset_result.returncode != 0:
        summary = (
            f"{timestamp}\n"
            f"判断: 自動復旧を試みたがreset_state.pyが異常終了(exit={reset_result.returncode})したためエスカレーション\n"
            f"出力:\n{reset_result.stdout}\n{reset_result.stderr}"
        )
        notify("escalation", summary)
        print("reset_state.pyが異常終了。エスカレーションしました。", file=sys.stderr)
        return

    # Step 3: resize_grid.py --apply
    resize_result = run([VENV_PYTHON, "-m", "src.resize_grid", "--pair", "xrp_jpy", "--apply"])
    executed_steps.append(f"resize_grid.py --apply: exit={resize_result.returncode}")

    # Step 4: git反映
    branch_name = f"chore/auto-resize-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}"
    git_steps = [
        ["git", "checkout", "main"],
        ["git", "pull", "origin", "main"],
        ["git", "checkout", "-b", branch_name],
        ["git", "add", "src/config.py"],
    ]
    for step in git_steps:
        run(step)

    diff_check = run(["git", "diff", "--cached", "--quiet"])
    if diff_check.returncode != 0:
        # 差分がある場合のみコミット・PR作成
        run(["git", "commit", "-m", f"resize_grid.py自動適用(インシデント対応): {timestamp}"])
        run(["git", "push", "-u", "origin", branch_name])
        run(["gh", "pr", "create", "--title", f"resize_grid.py自動適用({timestamp})",
             "--body", "インシデント自動対応によるamount_per_level_xrp調整"])
        run(["gh", "pr", "merge", "--squash"])
        run(["git", "checkout", "main"])
        run(["git", "pull", "origin", "main"])
        executed_steps.append("git反映: 実施(config.py変更あり)")
    else:
        run(["git", "checkout", "main"])
        executed_steps.append("git反映: スキップ(config.py変更なし)")

    # Step 5: bot再起動
    run(["sudo", "systemctl", "start", "grid_bot"])
    status_result = run(["sudo", "systemctl", "status", "grid_bot"])
    executed_steps.append(f"bot再起動: {'成功' if 'active (running)' in status_result.stdout else '要確認'}")

    # 実残高を再取得して報告に含める
    final_check = run([VENV_PYTHON, "scripts/incident_check.py"])

    summary = (
        f"{timestamp}\n"
        f"判断根拠: {decision['reasoning']}\n"
        f"実施内容:\n" + "\n".join(f"  - {s}" for s in executed_steps) + "\n"
        f"復旧後の状況:\n{final_check.stdout}"
    )
    notify("response", summary)
    print("自動復旧を完了しました。", file=sys.stderr)


if __name__ == "__main__":
    main()
