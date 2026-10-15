from types import SimpleNamespace
from unittest.mock import MagicMock

from src.cleanup_orders import cancel_buy_orders


def _client(orders, fail_ids=()):
    c = MagicMock()
    c.get_active_orders.return_value = {"orders": orders}

    def cancel(pair, oid):
        if oid in fail_ids:
            raise RuntimeError("boom")
        return {"status": "CANCELED_UNFILLED"}
    c.cancel_order.side_effect = cancel
    return c


def _o(oid, side, price="200", amt="1"):
    return {"order_id": oid, "side": side, "price": price, "remaining_amount": amt}


def test_cancels_only_buy_orders_and_keeps_sells():
    client = _client([_o(1, "buy"), _o(2, "sell"), _o(3, "buy")])
    r = cancel_buy_orders(client, "xrp_jpy", throttle_sec=0)
    assert [x["order_id"] for x in r["succeeded"]] == [1, 3]
    cancelled = [c.args[1] for c in client.cancel_order.call_args_list]
    assert cancelled == [1, 3]  # 売り(2)は取り消さない


def test_reports_failed_orders_without_stopping():
    client = _client([_o(1, "buy"), _o(2, "buy")], fail_ids=(1,))
    r = cancel_buy_orders(client, "xrp_jpy", throttle_sec=0)
    assert [x["order_id"] for x in r["failed"]] == [1]
    assert [x["order_id"] for x in r["succeeded"]] == [2]


def test_syncs_local_state_for_cancelled_buys_only():
    client = _client([_o(10, "buy"), _o(11, "sell")])
    store = MagicMock()
    store.list_open_orders.return_value = {
        "req-buy": SimpleNamespace(exchange_order_id="10"),
        "req-sell": SimpleNamespace(exchange_order_id="11"),
    }
    cancel_buy_orders(client, "xrp_jpy", store=store, throttle_sec=0)
    assert store.update_state.call_count == 1
    assert store.update_state.call_args.args[0] == "req-buy"


def test_no_buy_orders_is_a_noop():
    client = _client([_o(2, "sell")])
    r = cancel_buy_orders(client, "xrp_jpy", throttle_sec=0)
    assert r == {"succeeded": [], "failed": []}
    client.cancel_order.assert_not_called()
