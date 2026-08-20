import json
from unittest.mock import patch, MagicMock

import pytest

from src.bitbank_client import BitbankClient


@pytest.fixture
def client():
    return BitbankClient(api_key="dummy_key", api_secret="dummy_secret")


def _mock_response(json_data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status.return_value = None
    return resp


def test_create_order_limit_includes_price(client):
    with patch.object(client._session, "post") as mock_post:
        mock_post.return_value = _mock_response({"success": 1, "data": {"order_id": 1}})
        client.create_order(pair="xrp_jpy", price=159.61, amount=8.0, side="buy", order_type="limit")

    sent_body = json.loads(mock_post.call_args.kwargs["data"])
    assert sent_body["price"] == "159.61"
    assert sent_body["type"] == "limit"


def test_create_order_market_omits_price(client):
    """
    成行注文にはpriceを含めない。緊急停止時の強制決済(成行買い注文)が
    エラーコード60002で失敗した事故の再発防止テスト。
    """
    with patch.object(client._session, "post") as mock_post:
        mock_post.return_value = _mock_response({"success": 1, "data": {"order_id": 1}})
        client.create_order(pair="xrp_jpy", amount=120.0, side="buy", order_type="market", post_only=False)

    sent_body = json.loads(mock_post.call_args.kwargs["data"])
    assert "price" not in sent_body
    assert sent_body["type"] == "market"
    assert sent_body["amount"] == "120.0"
    assert "post_only" not in sent_body  # post_only=Falseの場合はキー自体を含めない


def test_create_order_limit_without_price_raises():
    client = BitbankClient(api_key="dummy_key", api_secret="dummy_secret")
    with pytest.raises(ValueError):
        client.create_order(pair="xrp_jpy", amount=8.0, side="buy", order_type="limit")


def test_create_order_post_only_true_included_in_body(client):
    with patch.object(client._session, "post") as mock_post:
        mock_post.return_value = _mock_response({"success": 1, "data": {"order_id": 1}})
        client.create_order(pair="xrp_jpy", price=159.61, amount=8.0, side="sell", post_only=True)

    sent_body = json.loads(mock_post.call_args.kwargs["data"])
    assert sent_body["post_only"] is True
