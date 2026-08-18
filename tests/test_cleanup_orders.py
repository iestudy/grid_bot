from unittest.mock import MagicMock

import pytest
import requests

from src.cleanup_orders import cancel_order_with_retry, cancel_all_orders


def make_mock_client():
    return MagicMock()


def test_cancel_order_with_retry_succeeds_after_rate_limit():
    client = make_mock_client()
    rate_limit_error = requests.exceptions.HTTPError(response=MagicMock(status_code=429))
    client.cancel_order.side_effect = [rate_limit_error, {"status": "CANCELED_UNFILLED"}]

    result = cancel_order_with_retry(client, "xrp_jpy", 123, backoff_base_sec=0.01)

    assert result is True
    assert client.cancel_order.call_count == 2


def test_cancel_order_with_retry_gives_up_after_max_retries():
    client = make_mock_client()
    rate_limit_error = requests.exceptions.HTTPError(response=MagicMock(status_code=429))
    client.cancel_order.side_effect = rate_limit_error

    result = cancel_order_with_retry(client, "xrp_jpy", 123, max_retries=2, backoff_base_sec=0.01)

    assert result is False
    assert client.cancel_order.call_count == 3  # 初回 + リトライ2回


def test_cancel_all_orders_processes_every_order_despite_rate_limits():
    client = make_mock_client()
    client.get_active_orders.return_value = {
        "orders": [{"order_id": 1}, {"order_id": 2}, {"order_id": 3}]
    }
    rate_limit_error = requests.exceptions.HTTPError(response=MagicMock(status_code=429))
    # 2番目の注文だけ1回レート制限に当たってからリトライで成功する想定
    client.cancel_order.side_effect = [
        {"status": "CANCELED_UNFILLED"},   # order 1: 成功
        rate_limit_error,                   # order 2: 1回目失敗
        {"status": "CANCELED_UNFILLED"},   # order 2: リトライ成功
        {"status": "CANCELED_UNFILLED"},   # order 3: 成功
    ]

    result = cancel_all_orders(client, "xrp_jpy", throttle_sec=0.0)

    assert result["succeeded"] == [1, 2, 3]
    assert result["failed"] == []


def test_cancel_all_orders_reports_failures_without_stopping():
    client = make_mock_client()
    client.get_active_orders.return_value = {
        "orders": [{"order_id": 1}, {"order_id": 2}]
    }
    other_error = RuntimeError("network down")
    client.cancel_order.side_effect = [
        other_error,                        # order 1: 非レート制限エラー(リトライしない)
        {"status": "CANCELED_UNFILLED"},   # order 2: 成功
    ]

    result = cancel_all_orders(client, "xrp_jpy", throttle_sec=0.0)

    assert result["succeeded"] == [2]
    assert result["failed"] == [1]


def test_cancel_all_orders_syncs_local_state_store_when_provided():
    from src.state_store import InMemoryStateStore, OrderRecord, OrderState
    from src.cleanup_orders import cancel_all_orders

    client = make_mock_client()
    client.get_active_orders.return_value = {"orders": [{"order_id": 111}]}
    client.cancel_order.return_value = {"status": "CANCELED_UNFILLED"}

    store = InMemoryStateStore()
    record = OrderRecord(
        request_id="r1", pair="xrp_jpy", side="buy", price=100.0, amount=8.0,
        state=OrderState.OPEN, exchange_order_id="111",
    )
    store.save_order(record)

    result = cancel_all_orders(client, "xrp_jpy", store=store, throttle_sec=0.0)

    assert result["succeeded"] == [111]
    assert store.get_order("r1").state == OrderState.CANCELED


def test_cancel_all_orders_without_store_does_not_crash():
    from src.cleanup_orders import cancel_all_orders

    client = make_mock_client()
    client.get_active_orders.return_value = {"orders": [{"order_id": 111}]}
    client.cancel_order.return_value = {"status": "CANCELED_UNFILLED"}

    result = cancel_all_orders(client, "xrp_jpy", store=None, throttle_sec=0.0)

    assert result["succeeded"] == [111]


def test_cancel_order_with_retry_retries_on_bitbank_transient_error_code():
    from src.bitbank_client import BitbankAPIError
    from src.cleanup_orders import cancel_order_with_retry

    client = make_mock_client()
    transient_error = BitbankAPIError("bitbank API error: {'success': 0, 'data': {'code': 70019}}", code=70019)
    client.cancel_order.side_effect = [transient_error, {"status": "CANCELED_UNFILLED"}]

    result = cancel_order_with_retry(client, "xrp_jpy", 123, backoff_base_sec=0.01)

    assert result is True
    assert client.cancel_order.call_count == 2


def test_cancel_order_with_retry_does_not_retry_non_transient_bitbank_error():
    from src.bitbank_client import BitbankAPIError
    from src.cleanup_orders import cancel_order_with_retry

    client = make_mock_client()
    # 70001はRETRYABLE_BITBANK_CODESに含まれない(システムエラー、リトライしても
    # 解決しない可能性が高いもの)想定
    fatal_error = BitbankAPIError("bitbank API error: {'success': 0, 'data': {'code': 70001}}", code=70001)
    client.cancel_order.side_effect = fatal_error

    result = cancel_order_with_retry(client, "xrp_jpy", 123, backoff_base_sec=0.01)

    assert result is False
    assert client.cancel_order.call_count == 1
