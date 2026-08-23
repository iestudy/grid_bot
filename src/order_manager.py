"""
注文管理レイヤー。以下3つの責務を持つ:

1. reconcile_orders: 取引所側の実際の注文状態とローカルstate_storeを突き合わせる。
   ローカルでOPENだったはずの注文が取引所側に見当たらない場合、個別に状態照会して
   「約定した」のか「キャンセルされた」のかを判別し、約定していればportfolio_state
   (cash_flow/net_inventory)を更新する。

2. sync_grid_orders: 望ましいグリッド(generate_gridの出力)と、現在実際に開いている
   注文を比較し、不足分だけ新規発注する。冪等キー(request_id)による二重発注防止付き。

3. apply_hard_stop_loss: HardStopLossManagerの判定結果に従って、必要なら
   成行相当の強制決済を実行し、EMERGENCY_STOP/FULL_CLOSE時は全ての未約定注文を
   キャンセルしてグリッド運用を停止する。
"""

import logging
import time
from dataclasses import dataclass
from typing import List, Optional

import requests

from .bitbank_client import BitbankClient, BitbankAPIError
from .state_store import StateStore, OrderRecord, OrderState, PortfolioState
from .grid_engine import GridLevel, synthetic_position_from_portfolio
from .hard_stop_loss import HardStopLossManager, Action

logger = logging.getLogger(__name__)

# bitbankの一時的なエラーコード(要求自体は無効ではなく、時間を置けば成功しうるもの)
#   70011: システムが混雑しています。しばらくしてから再度お試しください
#   70013: システム負荷上昇のため、注文および注文キャンセルを一時的に制限中
#   70019: 注文はキャンセル中です(直前のキャンセル要求がまだ処理中)
RETRYABLE_BITBANK_CODES = {70011, 70013, 70019}

# bitbankの注文ステータス文字列（公式ドキュメントで要最終確認）
STATUS_FULLY_FILLED = "FULLY_FILLED"
STATUS_PARTIALLY_FILLED = "PARTIALLY_FILLED"
STATUS_CANCELED = ("CANCELED_UNFILLED", "CANCELED_PARTIALLY_FILLED")


@dataclass
class ReconcileResult:
    newly_filled: List[OrderRecord]
    still_open_count: int


def _get_order_status_with_retry(
    client: BitbankClient,
    pair: str,
    order_id: int,
    max_retries: int = 3,
    backoff_base_sec: float = 1.0,
):
    attempt = 0
    while True:
        try:
            return client.get_order_status(pair, order_id)
        except Exception as e:
            if _is_rate_limit_error(e) and attempt < max_retries:
                wait_sec = backoff_base_sec * (2 ** attempt)
                logger.warning(f"レート制限(429)、{wait_sec:.1f}秒待機してリトライ (get_order_status order_id={order_id})")
                time.sleep(wait_sec)
                attempt += 1
                continue
            raise


def _is_rate_limit_error(e: Exception) -> bool:
    if isinstance(e, requests.exceptions.HTTPError) and e.response is not None and e.response.status_code == 429:
        return True
    if isinstance(e, BitbankAPIError) and e.code in RETRYABLE_BITBANK_CODES:
        return True
    return False


def reconcile_orders(client: BitbankClient, store: StateStore, pair: str) -> ReconcileResult:
    """
    state_store上でOPEN/PENDINGだが、取引所のactive_ordersに存在しない注文を
    個別照会し、約定/キャンセルを判別してportfolio_stateとorder状態を更新する。
    """
    active = client.get_active_orders(pair)
    active_order_ids = {str(o["order_id"]) for o in active.get("orders", [])}

    newly_filled: List[OrderRecord] = []
    local_open = store.list_open_orders()

    for request_id, record in local_open.items():
        if record.state == OrderState.PENDING:
            # 発注APIが応答不明のまま終わっていたケース。exchange_order_idが
            # まだ無い場合は、取引所側にこの内容の注文が実在するか確認するすべが
            # request_idだけでは無いため、ここでは経過時間などの上位ロジックで
            # 別途タイムアウト処理することを前提とし、本関数では触らない。
            continue

        if record.exchange_order_id is None:
            continue

        if record.exchange_order_id in active_order_ids:
            continue  # まだ板に残っている

        # active_ordersから消えている = 約定 or キャンセル。個別照会で確定させる
        try:
            exchange_order_id_int = int(record.exchange_order_id)
        except (TypeError, ValueError):
            # dry-run由来の偽ID等、実際の取引所注文IDでない不正な値が
            # 紛れ込んでいた場合の防御。ループ全体を止めず、警告のみ出して
            # このレコードはスキップする(手動でのクリーンアップが必要)。
            logger.warning(
                f"不正なexchange_order_idのためスキップ: request_id={request_id} "
                f"exchange_order_id={record.exchange_order_id!r}。手動確認・削除を推奨します。"
            )
            continue

        status = _get_order_status_with_retry(client, pair, exchange_order_id_int)
        status_str = status.get("status")

        if status_str == STATUS_FULLY_FILLED:
            executed_amount = float(status.get("executed_amount", record.amount))
            average_price = float(status.get("average_price", record.price))
            _apply_fill_to_portfolio(store, side=record.side, price=average_price, amount=executed_amount)
            store.update_state(request_id, OrderState.FILLED)
            newly_filled.append(record)
            logger.info(f"約定検知: {record.side} {executed_amount}@{average_price} (request_id={request_id})")

        elif status_str in STATUS_CANCELED:
            store.update_state(request_id, OrderState.CANCELED)
            logger.info(f"キャンセル確認: request_id={request_id}")

        else:
            # PARTIALLY_FILLEDのままactive_ordersから消えることは通常ないが、
            # 万一の不整合はログに残して次周期で再確認する
            logger.warning(f"予期しない状態: request_id={request_id} status={status_str}")

    still_open = len(store.list_open_orders())
    return ReconcileResult(newly_filled=newly_filled, still_open_count=still_open)


def _apply_fill_to_portfolio(store: StateStore, side: str, price: float, amount: float) -> None:
    state = store.get_portfolio_state()
    if side == "buy":
        state.cash_flow -= price * amount
        state.net_inventory += amount
    else:
        state.cash_flow += price * amount
        state.net_inventory -= amount
    store.save_portfolio_state(state)


def place_grid_level(
    client: BitbankClient,
    store: StateStore,
    pair: str,
    level: GridLevel,
    dry_run: bool = True,
    max_retries: int = 3,
    backoff_base_sec: float = 1.0,
) -> Optional[str]:
    """
    1本のグリッドレベルを冪等に発注する。
    先にPENDING状態でstate_storeに記録してからAPIを呼ぶことで、
    応答不明のまま二重発注するリスクを避ける。

    レート制限(429)を検知した場合は指数バックオフでリトライする
    (最大max_retries回)。それ以外のエラーは即座にFAILEDとして扱う。

    dry_run=Trueの場合、実際のAPI呼び出しは行わずログのみ出す(安全デフォルト)。
    戻り値: request_id (発注またはdry_runシミュレーション成功時) または None (失敗時)
    """
    request_id = store.new_request_id()
    record = OrderRecord(
        request_id=request_id, pair=pair, side=level.side,
        price=level.price, amount=level.amount, state=OrderState.PENDING,
    )
    store.save_order(record)

    if dry_run:
        logger.info(f"[DRY RUN] 発注シミュレーション: {level.side} {level.amount}@{level.price}")
        # dry_runでも実際に発注したのと同じOPEN状態で記録する。CANCELEDのままだと
        # sync_grid_ordersの重複判定(PENDING/OPENのみ対象)に引っかからず、
        # 毎周期同じ注文を「新規」と誤判定し続けてしまう。
        fake_exchange_order_id = f"dryrun-{request_id[:8]}"
        store.update_state(request_id, OrderState.OPEN, exchange_order_id=fake_exchange_order_id)
        return request_id

    attempt = 0
    while True:
        try:
            result = client.create_order(
                pair=pair, price=level.price, amount=level.amount, side=level.side,
                order_type="limit", post_only=True,
            )
            exchange_order_id = str(result.get("order_id"))
            store.update_state(request_id, OrderState.OPEN, exchange_order_id=exchange_order_id)
            logger.info(f"発注成功: {level.side} {level.amount}@{level.price} (order_id={exchange_order_id})")
            return request_id
        except Exception as e:
            if _is_rate_limit_error(e) and attempt < max_retries:
                wait_sec = backoff_base_sec * (2 ** attempt)
                logger.warning(
                    f"レート制限(429)を検知。{wait_sec:.1f}秒待機してリトライします "
                    f"(attempt {attempt + 1}/{max_retries}): {level.side} {level.amount}@{level.price}"
                )
                time.sleep(wait_sec)
                attempt += 1
                continue
            store.update_state(request_id, OrderState.FAILED)
            logger.error(f"発注失敗: {level.side} {level.amount}@{level.price} error={e}")
            return None


def sync_grid_orders(
    client: BitbankClient,
    store: StateStore,
    pair: str,
    desired_levels: List[GridLevel],
    dry_run: bool = True,
    min_interval_sec: float = 0.3,
) -> int:
    """
    望ましいグリッド(desired_levels)のうち、現在OPENでない(価格・方向が一致する
    注文が無い)ものだけを新規発注する。過剰発注を防ぐため、同一price+sideの
    組み合わせが既にOPENなら発注をスキップする。

    min_interval_sec: 連続発注の間隔(秒)。bitbankのレート制限に配慮した
    プロアクティブなスロットリング。個別発注側の指数バックオフ(429対応)と
    合わせて二段構えの対策とする。dry_runでは待機しない。

    戻り値: 新規発注した本数
    """
    open_orders = store.list_open_orders()
    existing_keys = {
        (rec.side, round(rec.price, 4)) for rec in open_orders.values()
        if rec.state in (OrderState.PENDING, OrderState.OPEN)
    }

    placed_count = 0
    for level in desired_levels:
        key = (level.side, round(level.price, 4))
        if key in existing_keys:
            continue
        if placed_count > 0 and not dry_run:
            time.sleep(min_interval_sec)
        place_grid_level(client, store, pair, level, dry_run=dry_run)
        placed_count += 1

    return placed_count


def apply_hard_stop_loss(
    client: BitbankClient,
    store: StateStore,
    hard_stop_cfg,
    manager: HardStopLossManager,
    pair: str,
    current_price: float,
    dry_run: bool = True,
    ledger=None,
    notifier=None,
) -> Action:
    """
    portfolio_stateから合成ポジションを作り、HardStopLossManagerで判定する。
    PARTIAL_CLOSE/FULL_CLOSE/EMERGENCY_STOP時は、必要な決済分を成行相当
    (post_only=False)で執行し、EMERGENCY_STOP/FULL_CLOSE時は残存する
    未約定注文を全てキャンセルする。

    ledger(PositionLedger)を渡した場合、強制決済もFIFO往復損益の計算対象に
    含める。これを渡さないと、緊急停止時の実現損益が日次サマリー・往復益通知に
    一切反映されない(cash_flow/net_inventoryというリスク判定用の内部値は
    正しく更新されるが、レポーティング用のrealized_profit_jpyには載らない)。
    """
    portfolio = store.get_portfolio_state()
    position = synthetic_position_from_portfolio(portfolio.cash_flow, portfolio.net_inventory)
    positions = [position] if position else []

    result = manager.evaluate(current_price=current_price, positions=positions)
    logger.info(f"HardStopLoss評価: action={result.action} pnl={result.unrealized_pnl_jpy:.2f} drawdown={result.drawdown_ratio:.4f}")

    if result.action == Action.NONE:
        return result.action

    if portfolio.net_inventory <= 0:
        # 現物取引では実在の空売りは発生し得ない。net_inventory<=0は
        # 「起動時点で保有していた在庫を使い切った」という会計上のラベルに
        # 過ぎず、買い戻しで解消すべき実在のリスクポジションではないため、
        # 強制決済(売買)は行わない。EMERGENCY_STOP/FULL_CLOSE自体(新規発注停止・
        # 既存注文の全キャンセル)は、価格乖離という別の観点から引き続き有効。
        logger.info(
            f"net_inventory={portfolio.net_inventory}のため強制決済(買い戻し)は行いません"
            f"(現物取引では実在のショートポジションが発生しないため)。"
        )
    else:
        close_amount = result.close_amount if result.action == Action.PARTIAL_CLOSE else portfolio.net_inventory
        close_side = "sell"

        if close_amount > 0:
            if dry_run:
                logger.warning(f"[DRY RUN] 強制決済シミュレーション: {close_side} {close_amount}@市場価格付近 action={result.action}")
            else:
                executed_price = current_price
                succeeded = False
                try:
                    client.create_order(
                        pair=pair, amount=close_amount, side=close_side,
                        order_type="market", post_only=False,
                    )
                    succeeded = True
                except Exception as e:
                    is_market_quantity_limit = isinstance(e, BitbankAPIError) and e.code == 60002
                    if is_market_quantity_limit:
                        # 成行注文の数量上限(板の流動性に応じて動的に変動する可能性がある)に
                        # 引っかかった場合、成行の代わりに板を確実に突き抜ける指値注文
                        # (post_only=False)にフォールバックする。
                        fallback_margin = 0.02  # 2%のマージン。板の急変動を吸収する目的
                        fallback_price = round(current_price * (1 - fallback_margin), 4)
                        logger.warning(
                            f"成行注文が数量上限(60002)で拒否されました。指値(post_only=False)へ"
                            f"フォールバックします: {close_side} {close_amount}@{fallback_price}"
                        )
                        try:
                            client.create_order(
                                pair=pair, amount=close_amount, side=close_side, price=fallback_price,
                                order_type="limit", post_only=False,
                            )
                            executed_price = fallback_price
                            succeeded = True
                        except Exception as fallback_error:
                            logger.error(f"指値フォールバックも失敗しました。手動対応が必要です: {fallback_error}")
                    else:
                        logger.error(f"強制決済に失敗しました。手動対応が必要です: {e}")

                if succeeded:
                    _apply_fill_to_portfolio(store, side=close_side, price=executed_price, amount=close_amount)
                    logger.warning(f"強制決済執行: {close_side} {close_amount}@約{executed_price} action={result.action}")

                    if ledger is not None:
                        round_trips = ledger.process_fill(close_side, executed_price, close_amount)
                        if round_trips:
                            updated_portfolio = store.get_portfolio_state()
                            updated_portfolio.total_fill_count += 1
                            for rt in round_trips:
                                updated_portfolio.realized_profit_jpy += rt.profit_jpy
                                logger.info(
                                    f"強制決済による往復決済: 買い{rt.buy_price}円 -> 売り{rt.sell_price}円 "
                                    f"{rt.amount}XRP 損益={rt.profit_jpy:+.2f}円"
                                )
                                if notifier is not None:
                                    notifier.notify_round_trip(rt.buy_price, rt.sell_price, rt.amount, rt.profit_jpy)
                            store.save_portfolio_state(updated_portfolio)
                            store.save_position_ledger_data(ledger.to_dict())

    if result.action in (Action.FULL_CLOSE, Action.EMERGENCY_STOP):
        _cancel_all_open_orders(client, store, pair, dry_run=dry_run)

    return result.action


def _cancel_all_open_orders(client: BitbankClient, store: StateStore, pair: str, dry_run: bool = True) -> None:
    open_orders = store.list_open_orders()
    for request_id, record in open_orders.items():
        if record.exchange_order_id is None:
            continue
        if dry_run:
            logger.warning(f"[DRY RUN] 全キャンセルシミュレーション: order_id={record.exchange_order_id}")
            continue
        try:
            client.cancel_order(pair, int(record.exchange_order_id))
            store.update_state(request_id, OrderState.CANCELED)
        except Exception as e:
            logger.error(f"キャンセル失敗: order_id={record.exchange_order_id} error={e}")
