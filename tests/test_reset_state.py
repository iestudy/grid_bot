from unittest.mock import MagicMock

import pytest

from src.reset_state import reset_state
from src.state_store import InMemoryStateStore, OrderRecord, OrderState, PortfolioState


def test_reset_state_cancels_orders_resets_portfolio_and_sets_base_price():
    client = MagicMock()
    client.get_active_orders.return_value = {"orders": [{"order_id": 1}, {"order_id": 2}]}
    client.cancel_order.return_value = {"status": "CANCELED_UNFILLED"}

    store = InMemoryStateStore()
    store.save_order(OrderRecord(
        request_id="r1", pair="xrp_jpy", side="buy", price=170.0, amount=8.0,
        state=OrderState.OPEN, exchange_order_id="1",
    ))
    store.save_order(OrderRecord(
        request_id="r2", pair="xrp_jpy", side="sell", price=180.0, amount=8.0,
        state=OrderState.OPEN, exchange_order_id="2",
    ))
    # リセット前は汚れた状態を再現
    store.save_portfolio_state(PortfolioState(cash_flow=19350.96, net_inventory=-120.0, realized_profit_jpy=50.0, total_fill_count=10))
    store.save_position_ledger_data({"buy_lots": [{"price": 100.0, "amount": 5.0}], "sell_lots": []})
    store.save_base_price(159.61)

    reset_state(client, store, "xrp_jpy", new_base_price=176.5, throttle_sec=0.0)

    # 1. 注文がキャンセルされ、ローカル状態も同期されている
    assert store.get_order("r1").state == OrderState.CANCELED
    assert store.get_order("r2").state == OrderState.CANCELED
    assert len(store.list_open_orders()) == 0

    # 2. ポートフォリオ・台帳がゼロにリセットされている
    portfolio = store.get_portfolio_state()
    assert portfolio.cash_flow == 0.0
    assert portfolio.net_inventory == 0.0
    assert portfolio.realized_profit_jpy == 0.0
    assert portfolio.total_fill_count == 0
    assert store.get_position_ledger_data() == {"buy_lots": [], "sell_lots": []}

    # 3. base_priceが新しい値に更新されている
    assert store.get_base_price() == 176.5


def test_reset_state_continues_even_if_some_cancels_fail():
    client = MagicMock()
    client.get_active_orders.return_value = {"orders": [{"order_id": 1}]}
    client.cancel_order.side_effect = RuntimeError("network error")

    store = InMemoryStateStore()

    # 例外を投げずに完走し、portfolio/base_priceのリセットまで到達することを確認
    reset_state(client, store, "xrp_jpy", new_base_price=176.5, throttle_sec=0.0)

    assert store.get_base_price() == 176.5
    assert store.get_portfolio_state().net_inventory == 0.0
