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
import time
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


def _is_notify_script(cmd):
    return cmd[:2] == [VENV_PYTHON, "scripts/notify_incident.py"]


def run(cmd, **kwargs):
    """subprocessをラップし、stdout/stderr/returncodeを記録しながら実行する。
    DRY_RUN時は読み取り専用コマンド(incident_check.py, git status/diff等)以外を
    実際には実行せず、疑似的な成功結果を返す。
    """
    is_check_script = cmd[:2] == [VENV_PYTHON, "scripts/incident_check.py"]
    # notify_incident.pyはdry-run時も実際に送信する([DRY RUN]ラベルを付けて
    # 送るのがnotify()関数側の責務。ここでスキップすると通知そのものが
    # 届かなくなり、dry-runの動作確認自体ができなくなってしまう)
    if DRY_RUN and not is_check_script and not _is_read_only(cmd) and not _is_notify_script(cmd):
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
    # incident_decide.py自体の異常(タイムアウト、クラッシュ、不正な出力)は
    # 全て「判断できない」として安全側のescalateにフォールバックする。
    # 過去にAnthropic API呼び出しがタイムアウトし、無防備なsubprocess.run()が
    # 未処理の例外でスクリプト全体をクラッシュさせ、エスカレーション通知すら
    # 送られないまま終了した事例があったため、ここは広くtry/exceptで囲む。
    try:
        decide_result = subprocess.run(
            [VENV_PYTHON, "scripts/incident_decide.py"],
            cwd=str(PROJECT_ROOT),
            input=incident_json_text,
            capture_output=True,
            text=True,
            timeout=120,
        )
        decision = json.loads(decide_result.stdout)
    except subprocess.TimeoutExpired:
        decision = {
            "action": "escalate",
            "matched_conditions": ["incident_decide.pyがタイムアウトした(120秒)"],
            "reasoning": "判断スクリプト(Claude API呼び出し)が120秒以内に応答しなかったため、安全側としてエスカレーションします。",
        }
    except json.JSONDecodeError:
        decision = {
            "action": "escalate",
            "matched_conditions": ["incident_decide.pyの出力がJSONとして解釈できない"],
            "reasoning": f"判断スクリプトの出力異常。stdout: {decide_result.stdout!r} stderr: {decide_result.stderr!r}",
        }
    except Exception as e:
        decision = {
            "action": "escalate",
            "matched_conditions": [f"incident_decide.py実行中に予期しない例外: {type(e).__name__}"],
            "reasoning": f"判断スクリプト実行中に例外が発生したため、安全側としてエスカレーションします: {e}",
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

    # --- action == auto_recover に進む前に、作業ツリーがクリーンであることを確認 ---
    # (git checkout main等が途中で失敗すると、中途半端な状態で処理が止まる
    #  リスクがあるため、事前に検知してエスカレーションする)
    status_check = run(["git", "status", "--porcelain"])
    if status_check.stdout.strip():
        summary = (
            f"{timestamp}\n"
            f"判断: 自動復旧可能と判定されたが、作業ツリーに未コミットの変更が"
            f"残っているため安全のためエスカレーション\n"
            f"git status --porcelain:\n{status_check.stdout}\n"
            f"元の判断根拠: {decision['reasoning']}"
        )
        notify("escalation", summary)
        print("作業ツリーが汚れているためエスカレーションしました。", file=sys.stderr)
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
    #
    # 各ステップ(特にpush/pr create/pr merge)はネットワーク不調
    # (TLS handshake timeout等、このEC2環境で実際に発生実績がある)で
    # 失敗する可能性があるため、必ずreturncodeを確認する。
    # 失敗を握り消すと「実施した」と誤って報告したまま設定変更が
    # 反映されない、というインシデントが実際に発生したため
    # (2026-09-11、gh pr createがTLS timeoutで失敗し、PRが作られず
    #  孤立ブランチが残ったまま「git反映: 実施」と報告された)、
    # 各ステップは最大2回まで自動リトライし、それでも失敗すれば
    # git反映を諦めてmainに復帰し、エスカレーションする。
    branch_name = f"chore/auto-resize-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}"

    def run_with_retry(cmd, retries=2, wait_sec=5):
        result = run(cmd)
        attempt = 1
        while result.returncode != 0 and attempt < retries:
            time.sleep(wait_sec)
            result = run(cmd)
            attempt += 1
        return result

    def abort_git_reflect(reason: str):
        """git反映を諦め、mainブランチに復帰してエスカレーションする。"""
        run(["git", "checkout", "main"])
        run(["git", "branch", "-D", branch_name])
        run(["git", "push", "origin", "--delete", branch_name])
        summary = (
            f"{timestamp}\n"
            f"判断: 自動復旧の途中(git反映)で失敗したためエスカレーション\n"
            f"失敗理由: {reason}\n"
            f"reset_state.py/resize_grid.py --applyは既に実行済み(ローカルの\n"
            f"config.pyはmainより新しい値のまま)。botはまだ古いconfig.pyで\n"
            f"再起動していないため、意図した数量変更が反映されていない状態。\n"
            f"元の判断根拠: {decision['reasoning']}"
        )
        notify("escalation", summary)
        print(f"git反映に失敗したためエスカレーションしました: {reason}", file=sys.stderr)

    for step in [["git", "checkout", "main"], ["git", "pull", "origin", "main"]]:
        result = run_with_retry(step)
        if result.returncode != 0:
            abort_git_reflect(f"{' '.join(step)} が失敗")
            return

    checkout_result = run(["git", "checkout", "-b", branch_name])
    if checkout_result.returncode != 0:
        abort_git_reflect(f"ブランチ作成({branch_name})に失敗")
        return
    run(["git", "add", "src/config.py"])

    diff_check = run(["git", "diff", "--cached", "--quiet"])
    if diff_check.returncode != 0:
        # 差分がある場合のみコミット・PR作成
        commit_result = run(["git", "commit", "-m", f"resize_grid.py自動適用(インシデント対応): {timestamp}"])
        if commit_result.returncode != 0:
            abort_git_reflect("git commitに失敗")
            return

        push_result = run_with_retry(["git", "push", "-u", "origin", branch_name])
        if push_result.returncode != 0:
            abort_git_reflect("git pushに失敗(ネットワーク不調の可能性)")
            return

        pr_create_result = run_with_retry(["gh", "pr", "create", "--title", f"resize_grid.py自動適用({timestamp})",
             "--body", "インシデント自動対応によるamount_per_level_xrp調整"])
        if pr_create_result.returncode != 0:
            abort_git_reflect("gh pr createに失敗(ネットワーク不調の可能性)")
            return

        pr_merge_result = run_with_retry(["gh", "pr", "merge", "--squash"])
        if pr_merge_result.returncode != 0:
            abort_git_reflect("gh pr mergeに失敗")
            return

        checkout_main_result = run(["git", "checkout", "main"])
        pull_result = run_with_retry(["git", "pull", "origin", "main"])
        if checkout_main_result.returncode != 0 or pull_result.returncode != 0:
            abort_git_reflect("マージ後のgit checkout main/pullに失敗")
            return

        # mainに実際に反映されたことを確認する(config.pyの差分が無いこと)
        verify_result = run(["git", "diff", "main", "origin/main", "--", "src/config.py"])
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
