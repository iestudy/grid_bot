"""
手動介入(緊急時の裁量取引など)によって、bot内部の会計状態(PortfolioState/
PositionLedger/base_price)と取引所の実際の状態が乖離してしまった場合の
緊急リセットツール。

このスクリプトは以下を1つの操作としてまとめて実行する:
1. 全未約定注文をキャンセルする(ローカル状態も同期)
2. PortfolioState/PositionLedgerをゼロにリセットする
   (「今この瞬間の実際の残高」を新しい追跡開始基準点とする。これはbot起動時に
    既存保有資産をゼロ基準として扱う設計と同じ考え方)
3. base_priceを現在の市場価格で上書きする

使い方:
    python3 -m src.reset_state --pair xrp_jpy --use-dynamodb

注意:
- これは資金を動かす操作(全キャンセル)を含むため、確認プロンプトを挟む
- 会計をリセットするため、リセット前の含み損益の記録は失われる
  (取引所側の実際の資産残高には影響しない。あくまでbot内部の帳簿の話)
"""

import argparse
import logging
import os

from dotenv import load_dotenv

from .bitbank_client import BitbankClient
from .state_store import InMemoryStateStore, DynamoDBStateStore, PortfolioState
from .cleanup_orders import cancel_all_orders

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def reset_state(client: BitbankClient, store, pair: str, new_base_price: float, throttle_sec: float = 0.5) -> None:
    logger.info("Step 1: 全未約定注文をキャンセルします(ローカル状態も同期)")
    result = cancel_all_orders(client, pair, store=store, throttle_sec=throttle_sec)
    logger.info(f"キャンセル結果: 成功={len(result['succeeded'])}件 失敗={len(result['failed'])}件")
    if result["failed"]:
        logger.error(f"キャンセルに失敗した注文が残っています。手動確認が必要です: {result['failed']}")

    logger.info("Step 2: PortfolioState/PositionLedgerをゼロにリセットします")
    store.save_portfolio_state(PortfolioState())
    store.save_position_ledger_data({"buy_lots": [], "sell_lots": []})

    logger.info(f"Step 3: base_priceを{new_base_price}円に設定します")
    store.save_base_price(new_base_price)

    logger.info("リセット完了。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", default="xrp_jpy")
    parser.add_argument("--use-dynamodb", action="store_true")
    parser.add_argument("--yes", "-y", action="store_true", help="確認プロンプトをスキップする")
    args = parser.parse_args()

    load_dotenv()
    client = BitbankClient(
        api_key=os.getenv("BITBANK_API_KEY"),
        api_secret=os.getenv("BITBANK_API_SECRET"),
    )
    store = DynamoDBStateStore() if args.use_dynamodb else InMemoryStateStore()

    ticker = client.get_ticker(args.pair)["data"]
    current_price = float(ticker["last"])

    print(f"現在価格: {current_price}")
    print("この操作は以下を行います:")
    print("  1. 全ての未約定注文をキャンセルする")
    print("  2. bot内部の損益追跡(PortfolioState/PositionLedger)をゼロにリセットする")
    print(f"  3. base_priceを{current_price}円に設定する")

    if not args.yes:
        confirm = input("続行しますか？ [y/N]: ")
        if confirm.strip().lower() != "y":
            print("中止しました。")
            exit(0)

    reset_state(client, store, args.pair, current_price)
