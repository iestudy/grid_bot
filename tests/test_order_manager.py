from unittest.mock import MagicMock
import requests

import pytest

from src.order_manager import (
    reconcile_orders, sync_grid_orders, place_grid_level,
    apply_hard_stop_loss, _apply_fill_to_portfolio,
)
from src.state_store import InMemoryStateStore, OrderRecord, OrderState
from src.grid_engine import GridLevel
from src.hard_stop_loss import HardStopLossManager, Action
from src.config import HardStopLossConfig


def make_mock_client():
    client = MagicMock()
    return client


def test_place_grid_level_dry_run_does_not_call_api():
    client = make_mock_client()
    store = InMemoryStateStore()
    level = GridLevel(side="buy", price=100.0, amount=8.0)

    result = place_grid_level(client, store, "xrp_jpy", level, dry_run=True)

    client.create_order.assert_not_called()
    assert result is not None  # dry_runでもrequest_idは返る
    record = store.get_order(result)
    assert record.state == OrderState.OPEN  # 重複発注防止のためdry_runでもOPEN記録する
    assert record.exchange_order_id.startswith("dryrun-")


def test_sync_grid_orders_does_not_repeat_same_dry_run_orders_across_cycles():
    """
    dry-runを複数周期実行しても、同じレベルへの発注が毎回「新規」と
    誤判定されないことを確認する回帰テスト。
    """
    client = make_mock_client()
    store = InMemoryStateStore()
    desired = [GridLevel(side="buy", price=100.0, amount=8.0)]

    placed_1 = sync_grid_orders(client, store, "xrp_jpy", desired, dry_run=True)
    placed_2 = sync_grid_orders(client, store, "xrp_jpy", desired, dry_run=True)

    assert placed_1 == 1
    assert placed_2 == 0  # 2周期目は既存とみなされスキップされるべき


def test_place_grid_level_live_calls_api_and_records_open():
    client = make_mock_client()
    client.create_order.return_value = {"order_id": 12345, "status": "UNFILLED"}
    store = InMemoryStateStore()
    level = GridLevel(side="buy", price=100.0, amount=8.0)

    request_id = place_grid_level(client, store, "xrp_jpy", level, dry_run=False)

    assert request_id is not None
    record = store.get_order(request_id)
    assert record.state == OrderState.OPEN
    assert record.exchange_order_id == "12345"


def test_place_grid_level_handles_api_failure():
    client = make_mock_client()
    client.create_order.side_effect = RuntimeError("network error")
    store = InMemoryStateStore()
    level = GridLevel(side="buy", price=100.0, amount=8.0)

    request_id = place_grid_level(client, store, "xrp_jpy", level, dry_run=False)

    assert request_id is None


def test_sync_grid_orders_skips_existing_levels():
    client = make_mock_client()
    client.create_order.return_value = {"order_id": 1, "status": "UNFILLED"}
    store = InMemoryStateStore()

    # 既にOPENな注文を1本用意
    existing = OrderRecord(request_id="r1", pair="xrp_jpy", side="buy", price=100.0, amount=8.0, state=OrderState.OPEN, exchange_order_id="999")
    store.save_order(existing)

    desired = [
        GridLevel(side="buy", price=100.0, amount=8.0),   # 既存と同一 → スキップされるはず
        GridLevel(side="buy", price=99.0, amount=8.0),     # 新規発注されるはず
    ]

    placed = sync_grid_orders(client, store, "xrp_jpy", desired, dry_run=False)

    assert placed == 1
    assert client.create_order.call_count == 1


def test_reconcile_orders_detects_fill_and_updates_portfolio():
    client = make_mock_client()
    client.get_active_orders.return_value = {"orders": []}  # 板から消えている
    client.get_order_status.return_value = {
        "status": "FULLY_FILLED", "executed_amount": "8.0000", "average_price": "100.000",
    }
    store = InMemoryStateStore()
    record = OrderRecord(request_id="r1", pair="xrp_jpy", side="buy", price=100.0, amount=8.0, state=OrderState.OPEN, exchange_order_id="999")
    store.save_order(record)

    result = reconcile_orders(client, store, "xrp_jpy")

    assert len(result.newly_filled) == 1
    updated = store.get_order("r1")
    assert updated.state == OrderState.FILLED

    portfolio = store.get_portfolio_state()
    assert portfolio.net_inventory == pytest.approx(8.0)
    assert portfolio.cash_flow == pytest.approx(-800.0)


def test_reconcile_orders_detects_cancellation():
    client = make_mock_client()
    client.get_active_orders.return_value = {"orders": []}
    client.get_order_status.return_value = {"status": "CANCELED_UNFILLED"}
    store = InMemoryStateStore()
    record = OrderRecord(request_id="r1", pair="xrp_jpy", side="buy", price=100.0, amount=8.0, state=OrderState.OPEN, exchange_order_id="999")
    store.save_order(record)

    result = reconcile_orders(client, store, "xrp_jpy")

    assert len(result.newly_filled) == 0
    assert store.get_order("r1").state == OrderState.CANCELED


def test_reconcile_orders_leaves_still_open_orders_untouched():
    client = make_mock_client()
    client.get_active_orders.return_value = {"orders": [{"order_id": 999}]}
    store = InMemoryStateStore()
    record = OrderRecord(request_id="r1", pair="xrp_jpy", side="buy", price=100.0, amount=8.0, state=OrderState.OPEN, exchange_order_id="999")
    store.save_order(record)

    result = reconcile_orders(client, store, "xrp_jpy")

    assert len(result.newly_filled) == 0
    assert store.get_order("r1").state == OrderState.OPEN
    client.get_order_status.assert_not_called()


def test_apply_hard_stop_loss_none_when_healthy():
    client = make_mock_client()
    store = InMemoryStateStore()
    _apply_fill_to_portfolio(store, side="buy", price=100.0, amount=8.0)
    cfg = HardStopLossConfig(total_capital_jpy=30_000.0, max_price_deviation_jpy=1000.0)
    manager = HardStopLossManager(cfg, base_price=100.0)

    action = apply_hard_stop_loss(client, store, cfg, manager, "xrp_jpy", current_price=100.5, dry_run=True)

    assert action == Action.NONE
    client.create_order.assert_not_called()


def test_apply_hard_stop_loss_full_close_cancels_orders_when_live():
    client = make_mock_client()
    client.create_order.return_value = {"order_id": 1, "status": "FULLY_FILLED"}
    store = InMemoryStateStore()
    _apply_fill_to_portfolio(store, side="buy", price=100.0, amount=100.0)  # 大きめのポジション

    # 未約定注文も1本追加しておき、全決済時にキャンセルされるか確認
    order = OrderRecord(request_id="r1", pair="xrp_jpy", side="sell", price=105.0, amount=8.0, state=OrderState.OPEN, exchange_order_id="777")
    store.save_order(order)

    cfg = HardStopLossConfig(total_capital_jpy=10_000.0, max_drawdown_ratio=0.15, partial_close_ratio=0.08, max_price_deviation_jpy=1000.0)
    manager = HardStopLossManager(cfg, base_price=100.0)

    # 含み損が閾値を超える価格まで暴落させる
    action = apply_hard_stop_loss(client, store, cfg, manager, "xrp_jpy", current_price=80.0, dry_run=False)

    assert action == Action.FULL_CLOSE
    client.cancel_order.assert_called_once()
    portfolio = store.get_portfolio_state()
    assert portfolio.net_inventory == pytest.approx(0.0)


def test_dynamodb_list_open_orders_skips_portfolio_state_sentinel():
    """
    DynamoDBStateStoreの同一テーブル設計に起因するバグの回帰テスト。
    __PORTFOLIO_STATE__レコード(stateキーを持たない)がscan結果に
    混在していてもlist_open_ordersがKeyErrorを起こさないことを確認する。
    """
    from unittest.mock import patch, MagicMock
    from src.state_store import DynamoDBStateStore

    fake_table = MagicMock()
    fake_table.scan.return_value = {
        "Items": [
            {"request_id": "__PORTFOLIO_STATE__", "cash_flow": "-123.45", "net_inventory": "8.0"},
            {
                "request_id": "r1", "pair": "xrp_jpy", "side": "buy",
                "price": "100.0", "amount": "8.0", "state": "OPEN", "exchange_order_id": "999",
            },
        ]
    }

    with patch("boto3.resource") as mock_resource:
        mock_resource.return_value.Table.return_value = fake_table
        store = DynamoDBStateStore()
        result = store.list_open_orders()

    assert len(result) == 1
    assert "r1" in result
    assert "__PORTFOLIO_STATE__" not in result


def test_reconcile_orders_skips_malformed_exchange_order_id_without_crashing():
    """
    dry-run由来の偽exchange_order_id等、int変換できない値が紛れ込んでいても
    reconcile_orders全体がクラッシュしないことを確認する回帰テスト。
    """
    client = make_mock_client()
    client.get_active_orders.return_value = {"orders": []}
    store = InMemoryStateStore()
    bad_record = OrderRecord(
        request_id="r1", pair="xrp_jpy", side="buy", price=100.0, amount=8.0,
        state=OrderState.OPEN, exchange_order_id="dryrun-ffaadeb5",
    )
    store.save_order(bad_record)

    result = reconcile_orders(client, store, "xrp_jpy")

    assert len(result.newly_filled) == 0
    # クラッシュせず、該当レコードはOPENのまま(スキップされた)であることを確認
    assert store.get_order("r1").state == OrderState.OPEN
    client.get_order_status.assert_not_called()


def test_place_grid_level_retries_on_rate_limit_and_succeeds():
    client = make_mock_client()
    rate_limit_error = requests.exceptions.HTTPError(response=MagicMock(status_code=429))
    client.create_order.side_effect = [rate_limit_error, rate_limit_error, {"order_id": 999, "status": "UNFILLED"}]
    store = InMemoryStateStore()
    level = GridLevel(side="buy", price=100.0, amount=8.0)

    request_id = place_grid_level(client, store, "xrp_jpy", level, dry_run=False, backoff_base_sec=0.01)

    assert request_id is not None
    assert client.create_order.call_count == 3
    assert store.get_order(request_id).state == OrderState.OPEN


def test_place_grid_level_gives_up_after_max_retries():
    client = make_mock_client()
    rate_limit_error = requests.exceptions.HTTPError(response=MagicMock(status_code=429))
    client.create_order.side_effect = rate_limit_error
    store = InMemoryStateStore()
    level = GridLevel(side="buy", price=100.0, amount=8.0)

    request_id = place_grid_level(client, store, "xrp_jpy", level, dry_run=False, max_retries=2, backoff_base_sec=0.01)

    assert request_id is None
    assert client.create_order.call_count == 3  # 初回 + リトライ2回
    open_orders = store.list_open_orders()
    assert len(open_orders) == 0  # FAILEDなのでOPEN/PENDINGには含まれない


def test_place_grid_level_does_not_retry_non_rate_limit_errors():
    client = make_mock_client()
    client.create_order.side_effect = RuntimeError("some other error")
    store = InMemoryStateStore()
    level = GridLevel(side="buy", price=100.0, amount=8.0)

    request_id = place_grid_level(client, store, "xrp_jpy", level, dry_run=False, max_retries=3, backoff_base_sec=0.01)

    assert request_id is None
    assert client.create_order.call_count == 1  # リトライされない


def test_apply_hard_stop_loss_feeds_ledger_and_notifies_on_forced_close():
    """
    今回のインシデントで発覚した不具合の回帰テスト:
    強制決済(EMERGENCY_STOP等)がPositionLedgerに反映されず、
    日次サマリー・往復益通知に実現損益が計上されない事故があった。
    """
    from src.position_ledger import PositionLedger

    client = make_mock_client()
    client.create_order.return_value = {"order_id": 1, "status": "FULLY_FILLED"}
    store = InMemoryStateStore()
    # 買いロットを1本仕込んでおき、強制決済(売り)とマッチさせる
    ledger = PositionLedger()
    ledger.process_fill("buy", price=160.0, amount=40.0)
    _apply_fill_to_portfolio(store, side="buy", price=160.0, amount=40.0)

    cfg = HardStopLossConfig(total_capital_jpy=10_000.0, max_drawdown_ratio=0.15, partial_close_ratio=0.08, max_price_deviation_jpy=1000.0)
    manager = HardStopLossManager(cfg, base_price=160.0)
    notifier = MagicMock()

    # 含み損が全決済閾値(15%)を明確に超える価格まで暴落させ、FULL_CLOSEを誘発
    action = apply_hard_stop_loss(
        client, store, cfg, manager, "xrp_jpy", current_price=100.0, dry_run=False,
        ledger=ledger, notifier=notifier,
    )

    assert action == Action.FULL_CLOSE
    notifier.notify_round_trip.assert_called_once()
    call_args = notifier.notify_round_trip.call_args[0]
    assert call_args[0] == 160.0  # buy_price
    assert call_args[1] == 100.0  # sell_price(強制決済の執行価格)

    portfolio = store.get_portfolio_state()
    assert portfolio.realized_profit_jpy == pytest.approx((100.0 - 160.0) * 40.0)  # -2400円の実現損
    assert portfolio.total_fill_count == 1


def test_apply_hard_stop_loss_works_without_ledger_backward_compatible():
    """ledger/notifierを渡さない既存の呼び出し方でも壊れないことを確認する。"""
    client = make_mock_client()
    client.create_order.return_value = {"order_id": 1, "status": "FULLY_FILLED"}
    store = InMemoryStateStore()
    _apply_fill_to_portfolio(store, side="buy", price=160.0, amount=40.0)

    cfg = HardStopLossConfig(total_capital_jpy=10_000.0, max_drawdown_ratio=0.15, max_price_deviation_jpy=1000.0)
    manager = HardStopLossManager(cfg, base_price=160.0)

    action = apply_hard_stop_loss(client, store, cfg, manager, "xrp_jpy", current_price=100.0, dry_run=False)

    assert action == Action.FULL_CLOSE
    # realized_profit_jpyはledger未指定なので更新されないままでよい(後方互換)
    assert store.get_portfolio_state().realized_profit_jpy == 0.0


def test_apply_hard_stop_loss_falls_back_to_limit_order_on_60002():
    """
    実運用で複数回発生した事故の回帰テスト: 成行注文が数量上限(60002)で
    拒否された場合、指値(post_only=False)へフォールバックして決済を完遂する。
    """
    from src.bitbank_client import BitbankAPIError

    client = make_mock_client()
    market_order_error = BitbankAPIError("bitbank API error: {'success': 0, 'data': {'code': 60002}}", code=60002)
    client.create_order.side_effect = [market_order_error, {"order_id": 1, "status": "FULLY_FILLED"}]
    store = InMemoryStateStore()
    _apply_fill_to_portfolio(store, side="buy", price=160.0, amount=40.0)

    cfg = HardStopLossConfig(total_capital_jpy=10_000.0, max_drawdown_ratio=0.15, max_price_deviation_jpy=1000.0)
    manager = HardStopLossManager(cfg, base_price=160.0)

    action = apply_hard_stop_loss(client, store, cfg, manager, "xrp_jpy", current_price=100.0, dry_run=False)

    assert action == Action.FULL_CLOSE
    assert client.create_order.call_count == 2
    # 1回目: 成行(price指定なし)
    first_call_kwargs = client.create_order.call_args_list[0].kwargs
    assert first_call_kwargs["order_type"] == "market"
    assert "price" not in first_call_kwargs
    # 2回目: 指値フォールバック(post_only=False、成行より不利な側に価格を振っている)
    second_call_kwargs = client.create_order.call_args_list[1].kwargs
    assert second_call_kwargs["order_type"] == "limit"
    assert second_call_kwargs["post_only"] is False
    # このテストはロング40XRPを下落局面で決済するシナリオ(close_side="sell")なので、
    # 確実に約定させるため現在価格より低い指値になっているはず
    assert second_call_kwargs["price"] < 100.0

    portfolio = store.get_portfolio_state()
    assert portfolio.net_inventory == pytest.approx(0.0)  # 決済が完遂している


def test_apply_hard_stop_loss_does_not_fallback_on_other_errors():
    """60002以外のエラーではフォールバックせず、そのまま失敗として扱われる。"""
    client = make_mock_client()
    client.create_order.side_effect = RuntimeError("network error")
    store = InMemoryStateStore()
    _apply_fill_to_portfolio(store, side="buy", price=160.0, amount=40.0)

    cfg = HardStopLossConfig(total_capital_jpy=10_000.0, max_drawdown_ratio=0.15, max_price_deviation_jpy=1000.0)
    manager = HardStopLossManager(cfg, base_price=160.0)

    action = apply_hard_stop_loss(client, store, cfg, manager, "xrp_jpy", current_price=100.0, dry_run=False)

    assert action == Action.FULL_CLOSE
    assert client.create_order.call_count == 1  # リトライされない
    # 決済が完遂していないため、ポジションはそのまま残っている
    assert store.get_portfolio_state().net_inventory == pytest.approx(40.0)


def test_apply_hard_stop_loss_logs_error_when_fallback_also_fails():
    from src.bitbank_client import BitbankAPIError

    client = make_mock_client()
    market_order_error = BitbankAPIError("bitbank API error: {'success': 0, 'data': {'code': 60002}}", code=60002)
    client.create_order.side_effect = [market_order_error, RuntimeError("fallback also failed")]
    store = InMemoryStateStore()
    _apply_fill_to_portfolio(store, side="buy", price=160.0, amount=40.0)

    cfg = HardStopLossConfig(total_capital_jpy=10_000.0, max_drawdown_ratio=0.15, max_price_deviation_jpy=1000.0)
    manager = HardStopLossManager(cfg, base_price=160.0)

    action = apply_hard_stop_loss(client, store, cfg, manager, "xrp_jpy", current_price=100.0, dry_run=False)

    assert action == Action.FULL_CLOSE
    assert client.create_order.call_count == 2
    # 両方失敗したのでポジションは未決済のまま
    assert store.get_portfolio_state().net_inventory == pytest.approx(40.0)


def test_apply_hard_stop_loss_skips_forced_close_for_negative_net_inventory():
    """
    今回のインシデントの回帰テスト: net_inventory<0(現物の在庫売り切り)は
    実在のショートポジションではないため、EMERGENCY_STOP(価格乖離)が発動しても
    買い戻しの強制決済は行わない。既存注文のキャンセルは引き続き行う。
    """
    client = make_mock_client()
    store = InMemoryStateStore()
    # 在庫を売り切った状態を再現(cash_flowは売却代金、net_inventoryはマイナス)
    _apply_fill_to_portfolio(store, side="sell", price=200.0, amount=40.0)
    assert store.get_portfolio_state().net_inventory == pytest.approx(-40.0)

    order = OrderRecord(
        request_id="r1", pair="xrp_jpy", side="buy", price=190.0, amount=8.0,
        state=OrderState.OPEN, exchange_order_id="777",
    )
    store.save_order(order)

    # 価格乖離でEMERGENCY_STOPを誘発(base_priceからの乖離が大きい)
    cfg = HardStopLossConfig(total_capital_jpy=30_000.0, max_price_deviation_jpy=8.0)
    manager = HardStopLossManager(cfg, base_price=200.0)

    action = apply_hard_stop_loss(client, store, cfg, manager, "xrp_jpy", current_price=215.0, dry_run=False)

    assert action == Action.EMERGENCY_STOP
    client.create_order.assert_not_called()  # 買い戻しの強制決済は行われない
    client.cancel_order.assert_called_once()  # 既存注文のキャンセルは引き続き行われる
    # portfolio(net_inventory)は変化していない
    assert store.get_portfolio_state().net_inventory == pytest.approx(-40.0)


def test_apply_hard_stop_loss_skips_forced_close_for_zero_net_inventory():
    client = make_mock_client()
    store = InMemoryStateStore()  # net_inventory=0のまま(初期状態)

    cfg = HardStopLossConfig(total_capital_jpy=30_000.0, max_price_deviation_jpy=8.0)
    manager = HardStopLossManager(cfg, base_price=200.0)

    action = apply_hard_stop_loss(client, store, cfg, manager, "xrp_jpy", current_price=215.0, dry_run=False)

    assert action == Action.EMERGENCY_STOP
    client.create_order.assert_not_called()
