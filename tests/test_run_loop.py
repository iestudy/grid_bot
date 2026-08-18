from unittest.mock import MagicMock

from src.run_loop import run_loop
from src.state_store import InMemoryStateStore


def make_mock_client(last_price=159.61):
    client = MagicMock()
    client.get_ticker.return_value = {"data": {"last": str(last_price), "buy": str(last_price - 0.001), "sell": str(last_price + 0.001)}}
    return client


def test_run_loop_dry_run_does_not_call_trading_apis():
    client = make_mock_client()
    store = InMemoryStateStore()

    run_loop(
        client=client, store=store, pair="xrp_jpy", base_price=159.61,
        poll_interval_sec=0, max_iterations=2, dry_run=True,
    )

    client.create_order.assert_not_called()
    client.cancel_order.assert_not_called()
    # dry_runではreconcile_ordersも呼ばれない(発注していないので照合対象がない)設計
    client.get_active_orders.assert_not_called()


def test_run_loop_respects_max_iterations():
    client = make_mock_client()
    store = InMemoryStateStore()

    # get_tickerの呼び出し回数でイテレーション数を確認
    run_loop(
        client=client, store=store, pair="xrp_jpy", base_price=159.61,
        poll_interval_sec=0, max_iterations=3, dry_run=True,
    )

    assert client.get_ticker.call_count == 3


def test_run_loop_stops_on_emergency_stop():
    from src.state_store import PortfolioState

    client = make_mock_client(last_price=100.0)
    client.get_active_orders.return_value = {"orders": []}
    client.create_order.return_value = {"order_id": 1, "status": "FULLY_FILLED"}
    store = InMemoryStateStore()
    # base_price=100だが、後続イテレーションでの現在価格をbase_priceから
    # 大きく乖離させてEMERGENCY_STOPを誘発する
    store.save_portfolio_state(PortfolioState(cash_flow=0.0, net_inventory=0.0))

    client.get_ticker.return_value = {"data": {"last": "50.0", "buy": "49.999", "sell": "50.001"}}

    run_loop(
        client=client, store=store, pair="xrp_jpy", base_price=100.0,
        poll_interval_sec=0, max_iterations=10, dry_run=False,
    )

    # EMERGENCY_STOPで即座にbreakするため、10回未満で終わっているはず
    assert client.get_ticker.call_count < 10


def test_run_loop_triggers_base_price_drift_when_grid_empty_on_one_side():
    """
    dry-runでは常にOPEN状態を記録するため、初回発注後は買い5本・売り5本が
    揃った状態になる。ここでは意図的に、乖離条件を満たすほど現在価格を
    base_priceから離しておき、実運用で「売りグリッドが枯渇したまま」に
    ならないことを確認する意味で、120分以上ノーフィルが継続した想定で
    ドリフト補正が発動することを確認する。
    """
    from src.state_store import InMemoryStateStore

    client = make_mock_client(last_price=161.5)  # drift閾値(1.5円)は超えるが緊急停止閾値(8円)は超えない
    store = InMemoryStateStore()

    # poll_interval=0で回すため、実時間ではno_fill_minutes_thresholdを
    # 自然に超えられない。time.timeをモックして時間経過を模擬する。
    import src.run_loop as run_loop_module

    real_time = run_loop_module.time.time
    fake_now = {"t": real_time()}

    def fake_time():
        return fake_now["t"]

    original_sleep = run_loop_module.time.sleep

    def fake_sleep(_):
        fake_now["t"] += 130 * 60  # 130分進める(閾値120分を超える)

    run_loop_module.time.time = fake_time
    run_loop_module.time.sleep = fake_sleep
    try:
        run_loop(
            client=client, store=store, pair="xrp_jpy", base_price=159.61,
            poll_interval_sec=0, max_iterations=2, dry_run=True,
        )
    finally:
        run_loop_module.time.time = real_time
        run_loop_module.time.sleep = original_sleep

    # 2周期目でbase_priceが現在価格側に寄っているはず(初期値159.61のままではない)
    open_orders = store.list_open_orders()
    prices = [rec.price for rec in open_orders.values()]
    # 161.5円方向にドリフトしていれば、新規に追加されたレベルの中に
    # 元のbase_price(159.61)近辺のレンジより高い価格が含まれるはず
    assert max(prices) > 161.0


def test_fetch_current_price_prefers_websocket_when_available():
    from src.run_loop import _fetch_current_price
    from unittest.mock import MagicMock

    client = make_mock_client(last_price=159.61)
    price_feed = MagicMock()
    price_feed.get_latest_price.return_value = 160.0

    price, source = _fetch_current_price(client, "xrp_jpy", price_feed)

    assert price == 160.0
    assert source == "websocket"
    client.get_ticker.assert_not_called()


def test_fetch_current_price_falls_back_to_rest_when_websocket_stale():
    from src.run_loop import _fetch_current_price
    from unittest.mock import MagicMock

    client = make_mock_client(last_price=159.61)
    price_feed = MagicMock()
    price_feed.get_latest_price.return_value = None  # 陳腐化・未接続を模擬

    price, source = _fetch_current_price(client, "xrp_jpy", price_feed)

    assert price == 159.61
    assert source == "rest"
    client.get_ticker.assert_called_once()


def test_fetch_current_price_uses_rest_when_no_feed_provided():
    from src.run_loop import _fetch_current_price

    client = make_mock_client(last_price=159.61)

    price, source = _fetch_current_price(client, "xrp_jpy", price_feed=None)

    assert price == 159.61
    assert source == "rest"


def test_run_loop_falls_back_gracefully_when_websocket_connect_fails():
    """
    WebSocket接続に失敗しても、ループ全体がクラッシュせずRESTのみで継続することを確認する。
    """
    from unittest.mock import patch, MagicMock

    client = make_mock_client(last_price=159.61)
    store = InMemoryStateStore()

    mock_feed_instance = MagicMock()
    mock_feed_instance.connect.side_effect = RuntimeError("connection refused")

    with patch("src.ws_public_client.WebSocketPriceFeed", return_value=mock_feed_instance):
        run_loop(
            client=client, store=store, pair="xrp_jpy", base_price=159.61,
            poll_interval_sec=0, max_iterations=2, dry_run=True, use_websocket=True,
        )

    # RESTフォールバックでget_tickerが呼ばれているはず
    assert client.get_ticker.call_count == 2
    # disconnectは呼ばれない(connectに失敗しているためfeedはNoneに戻っている)
    mock_feed_instance.disconnect.assert_not_called()


def test_run_loop_disconnects_websocket_feed_on_normal_completion():
    from unittest.mock import patch, MagicMock

    client = make_mock_client(last_price=159.61)
    store = InMemoryStateStore()

    mock_feed_instance = MagicMock()
    mock_feed_instance.get_latest_price.return_value = 159.61

    with patch("src.ws_public_client.WebSocketPriceFeed", return_value=mock_feed_instance):
        run_loop(
            client=client, store=store, pair="xrp_jpy", base_price=159.61,
            poll_interval_sec=0, max_iterations=2, dry_run=True, use_websocket=True,
        )

    mock_feed_instance.connect.assert_called_once()
    mock_feed_instance.disconnect.assert_called_once()
    # WebSocket価格が使われているため、get_tickerは呼ばれないはず
    client.get_ticker.assert_not_called()
