from unittest.mock import MagicMock

import pytest

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


def test_run_loop_notifies_round_trip_on_matched_fill():
    """
    買い→売りの往復が成立した際にSlack通知が呼ばれ、
    portfolio.realized_profit_jpyが正しく積算されることを確認する。
    """
    from unittest.mock import patch, MagicMock
    from src.state_store import InMemoryStateStore
    from src.order_manager import ReconcileResult
    from src.state_store import OrderRecord, OrderState

    client = make_mock_client(last_price=159.61)
    store = InMemoryStateStore()

    # 1回目のreconcileで買い約定、2回目で売り約定を返すよう仕込む
    buy_fill = OrderRecord(
        request_id="r1", pair="xrp_jpy", side="buy", price=100.0, amount=8.0,
        state=OrderState.FILLED, exchange_order_id="1",
    )
    sell_fill = OrderRecord(
        request_id="r2", pair="xrp_jpy", side="sell", price=102.0, amount=8.0,
        state=OrderState.FILLED, exchange_order_id="2",
    )

    call_count = {"n": 0}

    def fake_reconcile(client_, store_, pair_):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return ReconcileResult(newly_filled=[buy_fill], still_open_count=0)
        elif call_count["n"] == 2:
            return ReconcileResult(newly_filled=[sell_fill], still_open_count=0)
        return ReconcileResult(newly_filled=[], still_open_count=0)

    mock_notifier = MagicMock()

    with patch("src.run_loop.reconcile_orders", side_effect=fake_reconcile), \
         patch("src.run_loop.SlackNotifier", return_value=mock_notifier):
        run_loop(
            client=client, store=store, pair="xrp_jpy", base_price=159.61,
            poll_interval_sec=0, max_iterations=2, dry_run=False,
        )

    mock_notifier.notify_round_trip.assert_called_once()
    call_args = mock_notifier.notify_round_trip.call_args[0]
    assert call_args[0] == 100.0  # buy_price
    assert call_args[1] == 102.0  # sell_price
    assert call_args[3] == pytest.approx(16.0)  # profit_jpy

    portfolio = store.get_portfolio_state()
    assert portfolio.realized_profit_jpy == pytest.approx(16.0)
    assert portfolio.total_fill_count == 2


def test_run_loop_notifies_emergency_on_stop():
    from unittest.mock import patch, MagicMock
    from src.state_store import InMemoryStateStore

    client = make_mock_client(last_price=50.0)  # base_priceから大きく乖離させて緊急停止を誘発
    store = InMemoryStateStore()
    mock_notifier = MagicMock()

    with patch("src.run_loop.SlackNotifier", return_value=mock_notifier):
        run_loop(
            client=client, store=store, pair="xrp_jpy", base_price=159.61,
            poll_interval_sec=0, max_iterations=5, dry_run=False,
        )

    mock_notifier.notify_emergency.assert_called_once()


def test_run_loop_persists_base_price_on_drift_update():
    """
    ドリフト補正でbase_priceが更新された際、store側にも永続化されることを確認する。
    これは再起動時に古いbase_priceで新たなグリッドを重複発注する事故を防ぐための機構。
    reconcile_orders/sync_grid_ordersはモックし、ドリフト永続化ロジックのみを検証する。
    """
    from unittest.mock import patch
    from src.state_store import InMemoryStateStore, OrderRecord, OrderState
    from src.order_manager import ReconcileResult

    client = make_mock_client(last_price=161.5)  # drift閾値は超えるが緊急停止閾値は超えない
    store = InMemoryStateStore()
    # 売り側が0本、買い側だけ4本OPENの状態を直接作る(ドリフト条件を満たす状況)
    for i in range(4):
        store.save_order(OrderRecord(
            request_id=f"buy{i}", pair="xrp_jpy", side="buy", price=155.0 + i,
            amount=8.0, state=OrderState.OPEN, exchange_order_id=str(i),
        ))

    import src.run_loop as run_loop_module
    real_time = run_loop_module.time.time
    real_sleep = run_loop_module.time.sleep
    fake_now = {"t": real_time()}

    def fake_time():
        return fake_now["t"]

    def fake_sleep(_):
        fake_now["t"] += 130 * 60

    run_loop_module.time.time = fake_time
    run_loop_module.time.sleep = fake_sleep

    try:
        with patch("src.run_loop.reconcile_orders") as mock_reconcile, \
             patch("src.run_loop.sync_grid_orders", return_value=0):
            mock_reconcile.return_value = ReconcileResult(newly_filled=[], still_open_count=4)
            run_loop(
                client=client, store=store, pair="xrp_jpy", base_price=159.61,
                poll_interval_sec=0, max_iterations=2, dry_run=False,
            )
    finally:
        run_loop_module.time.time = real_time
        run_loop_module.time.sleep = real_sleep

    persisted = store.get_base_price()
    assert persisted is not None
    assert persisted != 159.61  # 更新後の値に変わっているはず
