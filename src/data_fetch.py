"""
過去の約定履歴（transactions）をbitbank Public APIから取得する。

なぜcandlestick(OHLC)ではなくtransactionsを使うか:
  candlestickは期間内の高値・安値しか分からず、「その価格帯で実際に
  約定した量」が分からない。一方transactionsは実際に売買が成立した
  1件ごとの記録（取引方向・価格・数量・時刻）なので、
  「自分の指値がその瞬間に約定したはずか」をより正確に検証できる。

制約:
- Public APIは日付単位(YYYYMMDD)でのみ取得可能。1日ごとにAPIコールが必要
- 過去に遡れる期間はbitbank側の保持期間に依存する（要確認）
- レート制限（Public APIは目安10 req/sec）を超えないよう、日毎にsleepを入れる
"""

import argparse
import csv
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Iterator, Dict

from .bitbank_client import BitbankClient


def fetch_transactions_for_date(client: BitbankClient, pair: str, day: date) -> list:
    yyyymmdd = day.strftime("%Y%m%d")
    data = client.get_transactions(pair, yyyymmdd)
    return data.get("transactions", [])


def fetch_range(client: BitbankClient, pair: str, start_date: date, end_date: date, sleep_sec: float = 0.3) -> Iterator[Dict]:
    day = start_date
    while day <= end_date:
        txs = fetch_transactions_for_date(client, pair, day)
        print(f"  {day}: {len(txs)}件")
        for tx in txs:
            yield {
                "timestamp": tx["executed_at"] / 1000.0,
                "side": tx["side"],       # taker側の売買方向
                "price": tx["price"],
                "amount": tx["amount"],
            }
        time.sleep(sleep_sec)
        day += timedelta(days=1)


def save_to_csv(rows: Iterator[Dict], out_path: str) -> int:
    out_path_obj = Path(out_path)
    out_path_obj.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(out_path_obj, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "side", "price", "amount"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", default="xrp_jpy")
    parser.add_argument("--days", type=int, default=14, help="過去何日分を取得するか")
    parser.add_argument("--out", default="data/xrp_jpy_transactions.csv")
    args = parser.parse_args()

    client = BitbankClient()  # Public APIのみなのでキー不要

    end_date = date.today() - timedelta(days=1)  # 当日分は未確定なので除外
    start_date = end_date - timedelta(days=args.days - 1)

    print(f"{args.pair} の約定履歴を {start_date} 〜 {end_date} の範囲で取得します...")
    rows = fetch_range(client, args.pair, start_date, end_date)
    count = save_to_csv(rows, args.out)
    print(f"取得件数: {count}")
    print(f"保存先: {args.out}")
