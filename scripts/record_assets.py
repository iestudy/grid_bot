"""
総資産を1日1回記録する。取引モード判断の効果を、あとから実績で比べるための材料。

run/asset_log.jsonl に1行ずつ追記する。JPY・XRPの内訳と価格を残すので、
「基準日の保有をそのまま持ち続けた場合」との比較もあとで計算できる。
取得に失敗したときは、0で埋めず、errorつきの行を残す。
"""
import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG = PROJECT_ROOT / "run" / "asset_log.jsonl"
JST = timezone(timedelta(hours=9))


def build_record(data: dict, now: datetime) -> dict:
    """incident_checkの出力から記録を作る。取れない値はNone、理由はerrorに残す。"""
    rec = {"at": now.isoformat(), "date_jst": now.astimezone(JST).strftime("%Y-%m-%d")}
    b = (data or {}).get("balances") or {}
    jpy, xrp = b.get("jpy") or {}, b.get("xrp") or {}
    price = (data or {}).get("current_price")
    if not (jpy and xrp and price):
        rec.update({"jpy": None, "xrp": None, "price": price, "total_jpy": None,
                    "error": "; ".join((data or {}).get("errors") or []) or "残高または価格を取得できませんでした"})
        return rec
    rec.update({
        "jpy": jpy["onhand_amount"], "xrp": xrp["onhand_amount"], "price": price,
        "total_jpy": round(jpy["onhand_amount"] + xrp["onhand_amount"] * price),
        "net_inventory": ((data.get("bot_state") or {}).get("net_inventory")),
    })
    return rec


def already_recorded(date_jst: str, path: Path = LOG) -> bool:
    if not path.exists():
        return False
    for line in path.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("date_jst") == date_jst and not r.get("error"):
            return True
    return False


def main():
    now = datetime.now(timezone.utc)
    date_jst = now.astimezone(JST).strftime("%Y-%m-%d")
    if already_recorded(date_jst):
        print(f"{date_jst} は記録済みです。")
        return
    try:
        r = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "incident_check.py")],
                           capture_output=True, text=True, timeout=120, cwd=str(PROJECT_ROOT))
        data = json.loads(r.stdout)
    except Exception as e:
        data = {"errors": [f"incident_check失敗: {e}"]}
    rec = build_record(data, now)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + chr(10))
    print(json.dumps(rec, ensure_ascii=False))


if __name__ == "__main__":
    main()
