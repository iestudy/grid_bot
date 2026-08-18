import time

import pytest

from src.ws_public_client import WebSocketPriceFeed


@pytest.fixture
def feed():
    return WebSocketPriceFeed(pair="xrp_jpy")


def test_get_latest_price_returns_none_before_any_message(feed):
    assert feed.get_latest_price() is None


def test_on_message_updates_latest_price_for_matching_room(feed):
    payload = {
        "room_name": "ticker_xrp_jpy",
        "message": {"data": {"last": "159.643", "buy": "159.642", "sell": "159.644"}},
    }
    feed._on_message(payload)
    assert feed.get_latest_price() == pytest.approx(159.643)


def test_on_message_ignores_other_rooms(feed):
    payload = {
        "room_name": "ticker_btc_jpy",  # 別ペアのルーム
        "message": {"data": {"last": "15000000"}},
    }
    feed._on_message(payload)
    assert feed.get_latest_price() is None


def test_on_message_handles_malformed_payload_without_raising(feed):
    feed._on_message("not a dict")
    feed._on_message({"room_name": "ticker_xrp_jpy"})  # messageキーが無い
    feed._on_message({"room_name": "ticker_xrp_jpy", "message": {"data": {}}})  # lastが無い
    assert feed.get_latest_price() is None  # クラッシュせず、価格も更新されない


def test_get_latest_price_returns_none_when_stale():
    feed = WebSocketPriceFeed(pair="xrp_jpy")
    payload = {"room_name": "ticker_xrp_jpy", "message": {"data": {"last": "159.643"}}}
    feed._on_message(payload)
    # 受信直後は取得できる
    assert feed.get_latest_price(max_age_sec=100) == pytest.approx(159.643)
    # 疑似的に古いタイムスタンプに書き換えて鮮度チェックを検証
    feed._latest_timestamp = time.time() - 60
    assert feed.get_latest_price(max_age_sec=30) is None


def test_connect_status_flags_toggle_on_connect_disconnect(feed):
    assert feed.is_connected() is False
    feed._sio.emit = lambda *a, **k: None  # emit呼び出しを無害化(実接続していないため)
    feed._on_connect()
    assert feed.is_connected() is True
    feed._on_disconnect()
    assert feed.is_connected() is False
