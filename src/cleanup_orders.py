"""
指定ペアの全未約定注文をキャンセルするユーティリティ。

これまで対話的なワンライナーとして何度も手動実行してきたが、429エラーで
キャンセル漏れが発生した事故があったため、指数バックオフ・スロットリングを
組み込んだ再利用可能なスクリプトとして整備する。

使い方:
    python3 -m src.cleanup_orders --pair xrp_jpy
"""

import argparse
import logging
import os
import time

import requests
from dotenv import load_dotenv

from .bitbank_client import BitbankClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _is_rate_limit_error(e: Exception) -> bool:
    return (
        isinstance(e, requests.exceptions.HTTPError)
        and e.response is not None
        and e.response.status_code == 429
    )


def cancel_order_with_retry(
    client: BitbankClient,
    pair: str,
    order_id,
    max_retries: int = 3,
    backoff_base_sec: float = 1.0,
) -> bool:
    """1件のキャンセルを、429時は指数バックオフでリトライする。成功したらTrue。"""
    attempt = 0
    while True:
        try:
            result = client.cancel_order(pair, order_id)
            logger.info(f"cancelled: {order_id} -> {result.get('status')}")
            return True
        except Exception as e:
            if _is_rate_limit_error(e) and attempt < max_retries:
                wait_sec = backoff_base_sec * (2 ** attempt)
                logger.warning(f"レート制限(429)、{wait_sec:.1f}秒待機してリトライ (order_id={order_id})")
                time.sleep(wait_sec)
                attempt += 1
                continue
            logger.error(f"キャンセル失敗: order_id={order_id} error={e}")
            return False


def cancel_all_orders(client: BitbankClient, pair: str, store=None, throttle_sec: float = 0.5) -> dict:
    """
    指定ペアの全未約定注文をキャンセルする。
    連続キャンセルの間にthrottle_sec秒の間隔を空け、429自体を予防する。

    store を渡した場合、キャンセル成功時にローカルのstate_store側の
    対応レコードもCANCELED状態に更新する。これを渡さずに実行すると、
    取引所側の状態とローカルDB側の状態が乖離したまま残り、次回起動時の
    reconcile_ordersが後追いで大量の個別照会を行うことになる
    (実際に過去、この乖離が原因でレート制限に接触した事故があった)。
    run_loopと同じstate_store(InMemory/DynamoDB)を使う場合は必ず指定すること。

    戻り値: {"succeeded": [...], "failed": [...]}
    """
    active = client.get_active_orders(pair)
    order_ids = [o["order_id"] for o in active.get("orders", [])]
    logger.info(f"未約定注文: {len(order_ids)}件 {order_ids}")

    exchange_id_to_request_id = {}
    if store is not None:
        from .state_store import OrderState  # 循環import回避のため遅延import
        for request_id, record in store.list_open_orders().items():
            if record.exchange_order_id:
                exchange_id_to_request_id[record.exchange_order_id] = request_id

    succeeded, failed = [], []
    for i, order_id in enumerate(order_ids):
        if i > 0:
            time.sleep(throttle_sec)
        if cancel_order_with_retry(client, pair, order_id):
            succeeded.append(order_id)
            if store is not None:
                from .state_store import OrderState
                request_id = exchange_id_to_request_id.get(str(order_id))
                if request_id:
                    store.update_state(request_id, OrderState.CANCELED)
                    logger.info(f"ローカル状態も同期: request_id={request_id} -> CANCELED")
        else:
            failed.append(order_id)

    return {"succeeded": succeeded, "failed": failed}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", default="xrp_jpy")
    parser.add_argument("--throttle", type=float, default=0.5, help="連続キャンセルの間隔(秒)")
    parser.add_argument("--use-dynamodb", action="store_true", help="run_loopと同じDynamoDBの状態も同期する(強く推奨)")
    args = parser.parse_args()

    load_dotenv()
    client = BitbankClient(
        api_key=os.getenv("BITBANK_API_KEY"),
        api_secret=os.getenv("BITBANK_API_SECRET"),
    )

    store = None
    if args.use_dynamodb:
        from .state_store import DynamoDBStateStore
        store = DynamoDBStateStore()
    else:
        logger.warning(
            "--use-dynamodbが指定されていません。run_loopをDynamoDBで運用している場合、"
            "このキャンセルはローカル状態に反映されず、次回起動時のreconcileで"
            "大量の個別照会が発生します。可能な限り --use-dynamodb を付けてください。"
        )

    result = cancel_all_orders(client, args.pair, store=store, throttle_sec=args.throttle)
    print(f"成功: {len(result['succeeded'])}件 / 失敗: {len(result['failed'])}件")
    if result["failed"]:
        print(f"失敗した注文ID(手動確認が必要): {result['failed']}")

    remaining = client.get_active_orders(args.pair)
    print(f"最終確認・残存注文数: {len(remaining.get('orders', []))}")
