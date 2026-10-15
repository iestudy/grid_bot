"""
取引モードの適用(履歴の読み書き、モードファイルの書き込み、買い注文の取り消し)。

decide_trading_mode.pyから呼ばれる。--applyのときだけ使う。
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

from src.trading_decision import pause_expiry

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HISTORY_LOG = PROJECT_ROOT / "run" / "decision_history.jsonl"
MODE_FILE = PROJECT_ROOT / "run" / "trading_mode.json"


def load_history(path: Path = None) -> List[dict]:
    """適用済みの判断履歴(古い順)。壊れた行は飛ばす。"""
    path = path or HISTORY_LOG
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def append_history(record: dict, path: Path = None) -> None:
    path = path or HISTORY_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + chr(10))


def write_mode_file(mode: str, reason: str, state: dict, now: datetime, path: Path = None) -> None:
    """取引モードファイルを書く。PAUSEDの期限は、開始時刻から最長24時間(判断のたびに延長しない)。"""
    path = path or MODE_FILE
    if mode == "PAUSED":
        continuing = state["current_mode"] == "PAUSED"
        started = state["pause_started_at"] if continuing else None
        expires = pause_expiry(now, started)
    else:
        expires = now + timedelta(hours=24)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "mode": mode, "set_at": now.isoformat(), "expires_at": expires.isoformat(), "reason": reason,
    }, ensure_ascii=False))


def format_cancel_report(result: dict) -> str:
    """買い注文の取り消し結果を、Slack用の文字列にする。"""
    lines = [chr(10) + f"買い注文の取り消し: 成功{len(result['succeeded'])}件 / 失敗{len(result['failed'])}件"]
    for o in result["succeeded"]:
        lines.append(f"  取消済み: {o['amount']} XRP @ {o['price']}円")
    for o in result["failed"]:
        lines.append(f"  取り消せず: {o['amount']} XRP @ {o['price']}円 (手動確認が必要)")
    return chr(10).join(lines)


def cancel_buys_and_report(env: dict) -> str:
    """買い注文を取り消し、結果を文字列で返す。失敗しても例外は投げない。"""
    try:
        from src.bitbank_client import BitbankClient
        from src.state_store import DynamoDBStateStore
        from src.cleanup_orders import cancel_buy_orders
        client = BitbankClient(api_key=env.get("BITBANK_API_KEY"), api_secret=env.get("BITBANK_API_SECRET"))
        result = cancel_buy_orders(client, "xrp_jpy", store=DynamoDBStateStore())
    except Exception as e:
        return chr(10) + f"買い注文の取り消しに失敗しました: {e}" + chr(10) + "(新規の買いは止まっていますが、既存の買いは残っています)"
    return format_cancel_report(result)
