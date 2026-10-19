"""
総資産の実績と、基準日の保有を持ち続けた場合を並べる。
判断の仕組み(と売買全体)が価値を足したかの、粗い目安。入出金がない前提。
"""
import json
from pathlib import Path

LOG = Path(__file__).resolve().parent.parent / "run" / "asset_log.jsonl"


def load(path=LOG):
    rows = []
    for line in path.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if not r.get("error") and r.get("total_jpy") is not None:
            rows.append(r)
    return rows


def compare(rows):
    """基準=最初の行。各日の『持ち続けた場合』=基準のJPY+XRP×その日の価格。"""
    base = rows[0]
    out = []
    for r in rows:
        hold = base["jpy"] + base["xrp"] * r["price"]
        out.append({"date": r["date_jst"], "price": r["price"], "actual": r["total_jpy"],
                    "hold": round(hold), "diff": round(r["total_jpy"] - hold)})
    return out


if __name__ == "__main__":
    rows = load()
    print("日付        価格     実績     持ち続け  差(実績-持ち続け)")
    for x in compare(rows):
        print(f"{x['date']}  {x['price']:7.2f}  {x['actual']:7d}  {x['hold']:7d}  {x['diff']:+d}")
