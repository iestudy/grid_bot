"""
統合ポーリングループ本体。

各周期で以下を実行する:
  1. reconcile_orders: 取引所と手元の注文状態を同期(約定/キャンセル検知)
  2. apply_hard_stop_loss: 含み損を評価し、必要なら強制決済・全停止
  3. (全停止していなければ) sync_grid_orders: 望ましいグリッドとの差分を発注

重要な安全設計:
  - dry_run はデフォルト True。実際に発注・キャンセルするには
    明示的に --live を指定する必要がある
  - --max-iterations で実行回数に上限を設けられる(初回テストは必ず小さい値で)
  - HardStopLossManagerがFULL_CLOSE/EMERGENCY_STOPを一度でも発動したら、
    このプロセスはループを終了する(自動再開しない。人間のレビューが必要)

現時点ではWebSocketではなくRESTポーリングによる実装。
本番の低レイテンシ化にはWebSocket化が今後の課題として残っている。
"""

import argparse
import logging
import os
import sys
import time

from .bitbank_client import BitbankClient
from .state_store import InMemoryStateStore, DynamoDBStateStore
from .grid_engine import (
    generate_grid, DriftState, should_update_base_price_bidirectional, update_base_price,
    should_halt_new_orders,
)
from .hard_stop_loss import HardStopLossManager, Action
from .order_manager import reconcile_orders, apply_hard_stop_loss, sync_grid_orders
from .position_ledger import PositionLedger
from .notifications import SlackNotifier
from .config import GRID_ENVELOPE, HARD_STOP_LOSS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _fetch_current_price(client: BitbankClient, pair: str, price_feed=None) -> tuple:
    """
    現在価格を取得する。price_feed(WebSocketPriceFeed)が渡されていて、
    かつ新鮮なデータを持っていればそちらを優先し、そうでなければ
    REST APIのget_tickerにフォールバックする。
    戻り値: (price: float, source: "websocket" | "rest")
    """
    if price_feed is not None:
        ws_price = price_feed.get_latest_price()
        if ws_price is not None:
            return ws_price, "websocket"

    ticker = client.get_ticker(pair)["data"]
    return float(ticker["last"]), "rest"


def run_loop(
    client: BitbankClient,
    store,
    pair: str,
    base_price: float,
    poll_interval_sec: float,
    max_iterations: int,
    dry_run: bool,
    use_websocket: bool = False,
) -> None:
    manager = HardStopLossManager(HARD_STOP_LOSS, base_price=base_price)
    desired_levels = generate_grid(base_price, GRID_ENVELOPE)
    drift_state = DriftState(
        base_price=base_price, last_fill_timestamp=time.time(),
        open_sell_count=0, open_buy_count=0,
    )
    notifier = SlackNotifier()
    ledger_data = store.get_position_ledger_data()
    ledger = PositionLedger.from_dict(ledger_data) if ledger_data else PositionLedger()

    mode_label = "DRY RUN（実発注なし）" if dry_run else "LIVE（実資金が動きます）"
    logger.info(f"ループ開始: mode={mode_label} pair={pair} base_price={base_price} max_iterations={max_iterations}")

    price_feed = None
    if use_websocket:
        from .ws_public_client import WebSocketPriceFeed
        price_feed = WebSocketPriceFeed(pair)
        try:
            price_feed.connect(timeout=10.0)
            logger.info("WebSocket価格フィードに接続しました。未接続・データ陳腐化時はRESTへ自動フォールバックします。")
        except Exception as e:
            logger.warning(f"WebSocket接続に失敗しました。RESTポーリングのみで継続します: {e}")
            price_feed = None

    try:
        iteration = 0
        while max_iterations <= 0 or iteration < max_iterations:
            iteration += 1
            logger.info(f"--- iteration {iteration} ---")

            try:
                current_price, price_source = _fetch_current_price(client, pair, price_feed)
            except Exception as e:
                logger.error(f"価格取得失敗、この周期はスキップ: {e}")
                time.sleep(poll_interval_sec)
                continue

            if not dry_run:
                try:
                    reconcile_result = reconcile_orders(client, store, pair)
                    logger.info(f"リコンサイル結果: 新規約定={len(reconcile_result.newly_filled)}件 未約定残={reconcile_result.still_open_count}件")
                    if reconcile_result.newly_filled:
                        drift_state.last_fill_timestamp = time.time()

                        portfolio_for_ledger = store.get_portfolio_state()
                        for filled_record in reconcile_result.newly_filled:
                            round_trips = ledger.process_fill(
                                filled_record.side, filled_record.price, filled_record.amount,
                            )
                            portfolio_for_ledger.total_fill_count += 1
                            for rt in round_trips:
                                portfolio_for_ledger.realized_profit_jpy += rt.profit_jpy
                                logger.info(
                                    f"往復決済成立: 買い{rt.buy_price}円 -> 売り{rt.sell_price}円 "
                                    f"{rt.amount}XRP 損益={rt.profit_jpy:+.2f}円"
                                )
                                notifier.notify_round_trip(rt.buy_price, rt.sell_price, rt.amount, rt.profit_jpy)
                        store.save_portfolio_state(portfolio_for_ledger)
                        store.save_position_ledger_data(ledger.to_dict())
                except Exception as e:
                    logger.error(f"リコンサイル失敗: {e}")
                    time.sleep(poll_interval_sec)
                    continue

            portfolio = store.get_portfolio_state()
            logger.info(
                f"現在価格={current_price} (source={price_source}) / "
                f"portfolio: cash_flow={portfolio.cash_flow:.2f}円 net_inventory={portfolio.net_inventory:.4f}XRP"
            )

            try:
                action = apply_hard_stop_loss(
                    client, store, HARD_STOP_LOSS, manager, pair, current_price, dry_run=dry_run,
                    ledger=ledger, notifier=notifier,
                )
            except Exception as e:
                logger.error(f"HardStopLoss評価に失敗しました。安全のためこの周期は新規発注を行いません: {e}")
                time.sleep(poll_interval_sec)
                continue

            if action in (Action.FULL_CLOSE, Action.EMERGENCY_STOP):
                logger.critical(f"{action}が発動しました。ループを終了します。人間のレビューが必要です。")
                portfolio_snapshot = store.get_portfolio_state()
                unrealized = portfolio_snapshot.cash_flow + portfolio_snapshot.net_inventory * current_price
                notifier.notify_emergency(action.value, current_price, unrealized)
                break

            # --- base_price自動ドリフト補正 ---
            open_orders = store.list_open_orders()
            drift_state.open_buy_count = sum(1 for r in open_orders.values() if r.side == "buy")
            drift_state.open_sell_count = sum(1 for r in open_orders.values() if r.side == "sell")
            now = time.time()

            if should_update_base_price_bidirectional(drift_state, current_price, now, GRID_ENVELOPE):
                new_base_price = update_base_price(drift_state, current_price)
                logger.warning(
                    f"base_price自動更新: {drift_state.base_price} -> {new_base_price} "
                    f"(現在価格={current_price}, 買い残={drift_state.open_buy_count} 売り残={drift_state.open_sell_count})"
                )
                drift_state.base_price = new_base_price
                drift_state.last_fill_timestamp = now
                manager.base_price = new_base_price
                desired_levels = generate_grid(new_base_price, GRID_ENVELOPE)
                if not dry_run:
                    try:
                        store.save_base_price(new_base_price)
                    except Exception as e:
                        logger.warning(f"base_price永続化に失敗(次回再起動時は今回のドリフト前の値から再開されます): {e}")

            if should_halt_new_orders(current_price, drift_state.base_price, GRID_ENVELOPE.new_order_halt_deviation_jpy):
                logger.info(
                    f"base_priceからの乖離({abs(current_price - drift_state.base_price):.3f}円)が"
                    f"閾値({GRID_ENVELOPE.new_order_halt_deviation_jpy}円)を超えたため、"
                    f"新規発注を一時停止します(既存注文はそのまま維持)。"
                )
            else:
                try:
                    placed = sync_grid_orders(client, store, pair, desired_levels, current_price=current_price, dry_run=dry_run)
                    if placed:
                        logger.info(f"新規発注: {placed}件")
                except Exception as e:
                    logger.error(f"グリッド発注同期に失敗: {e}")

            time.sleep(poll_interval_sec)

        logger.info("ループ終了。")
    finally:
        if price_feed is not None:
            try:
                price_feed.disconnect()
                logger.info("WebSocket価格フィードを切断しました。")
            except Exception as e:
                logger.warning(f"WebSocket切断時にエラー(無視して終了します): {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", default="xrp_jpy")
    parser.add_argument("--base-price", type=float, default=None, help="省略時は起動時の現在価格を使用")
    parser.add_argument("--poll-interval", type=float, default=10.0, help="ポーリング間隔(秒)")
    parser.add_argument("--max-iterations", type=int, default=5, help="0以下で無限ループ。初回は必ず小さい値で試すこと")
    parser.add_argument("--live", action="store_true", help="指定しない限りdry-run(実発注なし)で動作する")
    parser.add_argument("--yes", "-y", action="store_true", help="--live時の対話確認をスキップする。systemd等の非対話環境での自動起動に必須")
    parser.add_argument("--use-dynamodb", action="store_true", help="指定しない限りInMemory(プロセス終了で状態が消える)を使う")
    parser.add_argument("--use-websocket", action="store_true", help="価格取得にWebSocket(Socket.IO)を使う。未接続・データ陳腐化時はRESTへ自動フォールバック")
    args = parser.parse_args()

    if args.live:
        if args.yes:
            logger.warning("--yesが指定されたため、--live確認プロンプトをスキップして起動します。")
        else:
            confirm = input(
                "警告: --live が指定されています。実際の資金が動きます。\n"
                "続行しますか？ [y/N]: "
            )
            if confirm.strip().lower() != "y":
                print("中止しました。")
                sys.exit(0)

    from dotenv import load_dotenv
    load_dotenv()
    client = BitbankClient(
        api_key=os.getenv("BITBANK_API_KEY"),
        api_secret=os.getenv("BITBANK_API_SECRET"),
    )

    store = DynamoDBStateStore() if args.use_dynamodb else InMemoryStateStore()
    if not args.use_dynamodb:
        logger.warning("InMemoryStateStoreを使用しています。プロセスを終了すると状態(未約定注文の追跡・含み損計算)が失われます。")

    if not args.live:
        # dry-runは本番の永続ストアを絶対に汚染してはならない。
        # --use-dynamodbが指定されていても、dry-run時は常にInMemoryへ強制する。
        # (過去にdry-runの偽注文IDがDynamoDBに残り、その後のlive実行の
        #  リコンサイル処理をクラッシュさせた事故があったため)
        if args.use_dynamodb:
            logger.warning("dry-runのため、--use-dynamodbが指定されていてもInMemoryStateStoreを強制使用します（本番データ保護のため）。")
        store = InMemoryStateStore()

    if args.base_price is not None:
        # 明示的にbase_priceが指定された場合、既存の未約定注文と衝突しないか確認する。
        # ここでノーガードのまま進めると、価格のズレた新しいグリッドが積み重なる事故に
        # つながる(実際に緊急対応中、複数回このパターンで注文が積み重なった事例がある)。
        if args.live and args.use_dynamodb:
            existing_open = store.list_open_orders()
            if existing_open:
                logger.error(
                    f"--base-priceが明示指定されましたが、DynamoDB上に既存の未約定注文が"
                    f"{len(existing_open)}件あります。このまま続行すると、価格のズレた新しい"
                    f"グリッドが積み重なる事故につながるため起動を中止します。"
                    f"先に `python3 -m src.reset_state --pair {args.pair} --use-dynamodb` を"
                    f"実行して状態をクリーンにしてから、再度起動してください。"
                )
                sys.exit(1)

    if args.base_price is None:
        # 永続ストア(DynamoDB)に前回のbase_priceが残っていれば、それを優先して
        # 再利用する。ここで毎回「現在価格」を新規base_priceにしてしまうと、
        # サービス再起動のたびに全く別のグリッドが発注され、既存の未約定注文が
        # キャンセルされないまま取引所に残り続ける事故につながる
        # (実際に過去、再起動を繰り返して21本の孤立した注文が残った事例がある)。
        persisted_base_price = None
        if args.use_dynamodb and args.live:
            persisted_base_price = store.get_base_price()

        if persisted_base_price is not None:
            args.base_price = persisted_base_price
            logger.info(f"base_price未指定のため、DynamoDBに永続化されていた前回値を使用: {args.base_price}")
        else:
            ticker = client.get_ticker(args.pair)["data"]
            args.base_price = float(ticker["last"])
            logger.info(f"base_price未指定・永続化データなしのため現在価格を使用: {args.base_price}")

    if args.live and args.use_dynamodb:
        store.save_base_price(args.base_price)

    run_loop(
        client=client, store=store, pair=args.pair, base_price=args.base_price,
        poll_interval_sec=args.poll_interval, max_iterations=args.max_iterations,
        dry_run=not args.live, use_websocket=args.use_websocket,
    )


if __name__ == "__main__":
    main()
