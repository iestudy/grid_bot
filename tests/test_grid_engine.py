import pytest

from src.grid_engine import (
    generate_grid, should_update_base_price, update_base_price,
    apply_envelope_clamp, DriftState, estimate_total_capital_jpy,
    required_buy_side_jpy, required_sell_side_xrp,
)
from src.config import GridEnvelopeConfig


def test_generate_grid_symmetric():
    cfg = GridEnvelopeConfig()
    levels = generate_grid(base_price=159.61, cfg=cfg)
    buys = [l for l in levels if l.side == "buy"]
    sells = [l for l in levels if l.side == "sell"]
    assert len(buys) == cfg.max_buy_levels
    assert len(sells) == cfg.max_sell_levels
    assert all(l.price < 159.61 for l in buys)
    assert all(l.price > 159.61 for l in sells)


def test_should_update_base_price_triggers_when_conditions_met():
    cfg = GridEnvelopeConfig()
    state = DriftState(base_price=159.61, last_fill_timestamp=0.0, open_sell_count=0, open_buy_count=4)
    now = cfg.no_fill_minutes_threshold * 60 + 1  # 閾値を1秒超過
    result = should_update_base_price(state, market_price=157.5, now_timestamp=now, cfg=cfg)
    assert result is True


def test_should_update_base_price_false_when_sell_grid_exists():
    cfg = GridEnvelopeConfig()
    state = DriftState(base_price=159.61, last_fill_timestamp=0.0, open_sell_count=2, open_buy_count=4)
    now = cfg.no_fill_minutes_threshold * 60 + 1
    result = should_update_base_price(state, market_price=157.5, now_timestamp=now, cfg=cfg)
    assert result is False


def test_update_base_price_moves_halfway_by_default():
    state = DriftState(base_price=159.61, last_fill_timestamp=0.0, open_sell_count=0, open_buy_count=4)
    new_price = update_base_price(state, market_price=157.61)
    # 乖離2.0円の半分だけ寄せる → 158.61
    assert new_price == 158.61


def test_envelope_clamp():
    cfg = GridEnvelopeConfig()
    assert apply_envelope_clamp(0.3, cfg) == cfg.grid_width_min_jpy
    assert apply_envelope_clamp(1.5, cfg) == cfg.grid_width_max_jpy
    assert apply_envelope_clamp(0.8, cfg) == 0.8


def test_generate_grid_includes_amount():
    cfg = GridEnvelopeConfig()
    levels = generate_grid(base_price=159.61, cfg=cfg)
    assert all(l.amount == cfg.amount_per_level_xrp for l in levels)


def test_estimate_total_capital_jpy_matches_actual_balance():
    # 実際のget_assets()結果を簡略化したテストデータ
    assets = [
        {"asset": "jpy", "onhand_amount": "8302.3598"},
        {"asset": "xrp", "onhand_amount": "120.000000"},
        {"asset": "btc", "onhand_amount": "0.00000000"},
    ]
    total = estimate_total_capital_jpy(assets, current_price=159.6)
    # 8302.36 + 120*159.6 = 8302.36 + 19152 = 27454.36
    assert total == pytest.approx(27454.36, rel=1e-3)


def test_required_buy_side_jpy_within_available_balance():
    cfg = GridEnvelopeConfig()
    required_jpy = required_buy_side_jpy(cfg, base_price=159.61)
    available_jpy = 8302.36  # キャンセル後の自由JPY残高
    assert required_jpy < available_jpy, (
        f"買いグリッドに必要な{required_jpy:.2f}円が自由JPY残高{available_jpy}円を超えています"
    )


def test_required_sell_side_xrp_within_holdings():
    cfg = GridEnvelopeConfig()
    required_xrp = required_sell_side_xrp(cfg)
    held_xrp = 120.0
    assert required_xrp <= held_xrp, (
        f"売りグリッドに必要な{required_xrp}XRPが保有量{held_xrp}XRPを超えています"
    )


def test_synthetic_position_matches_naive_pnl_calc():
    from src.grid_engine import synthetic_position_from_portfolio

    # 買い100円x10、売り105円x10のラウンドトリップ後を模擬
    cash_flow = -1000.0 + 1050.0  # -買い支払 + 売り受取 = 50円の実現益、在庫はゼロに戻る想定
    net_inventory = 10.0 - 10.0  # 0
    pos = synthetic_position_from_portfolio(cash_flow, net_inventory)
    assert pos is None  # 在庫ゼロならポジションなし


def test_synthetic_position_reproduces_backtest_pnl_formula():
    from src.grid_engine import synthetic_position_from_portfolio
    from src.hard_stop_loss import HardStopLossManager
    from src.config import HardStopLossConfig

    # 買い100円x10のみ、まだ売っていない状態
    cash_flow = -1000.0
    net_inventory = 10.0
    current_price = 95.0  # 含み損が出ている状況

    pos = synthetic_position_from_portfolio(cash_flow, net_inventory)
    assert pos is not None
    assert pos.side == "buy"
    assert pos.price == pytest.approx(100.0)  # cost_price = -(-1000)/10 = 100
    assert pos.amount == pytest.approx(10.0)

    # HardStopLossManager経由の評価値が、素朴な計算(cash_flow + net_inventory*current_price)と一致するか
    cfg = HardStopLossConfig(total_capital_jpy=10_000.0, max_price_deviation_jpy=1000.0)
    manager = HardStopLossManager(cfg, base_price=100.0)
    result = manager.evaluate(current_price=current_price, positions=[pos])
    expected_pnl = cash_flow + net_inventory * current_price  # -1000 + 10*95 = -50
    assert result.unrealized_pnl_jpy == pytest.approx(expected_pnl)


def test_should_update_base_price_bidirectional_triggers_on_buy_grid_empty():
    from src.grid_engine import should_update_base_price_bidirectional

    cfg = GridEnvelopeConfig()
    # 買いグリッドが0本(上昇トレンドで価格がbase_priceを上抜けたケース)
    state = DriftState(base_price=159.61, last_fill_timestamp=0.0, open_sell_count=4, open_buy_count=0)
    now = cfg.no_fill_minutes_threshold * 60 + 1
    result = should_update_base_price_bidirectional(state, market_price=162.5, now_timestamp=now, cfg=cfg)
    assert result is True


def test_should_update_base_price_bidirectional_triggers_on_sell_grid_empty():
    from src.grid_engine import should_update_base_price_bidirectional

    cfg = GridEnvelopeConfig()
    state = DriftState(base_price=159.61, last_fill_timestamp=0.0, open_sell_count=0, open_buy_count=4)
    now = cfg.no_fill_minutes_threshold * 60 + 1
    result = should_update_base_price_bidirectional(state, market_price=157.5, now_timestamp=now, cfg=cfg)
    assert result is True


def test_should_update_base_price_bidirectional_false_when_both_sides_have_orders():
    from src.grid_engine import should_update_base_price_bidirectional

    cfg = GridEnvelopeConfig()
    state = DriftState(base_price=159.61, last_fill_timestamp=0.0, open_sell_count=3, open_buy_count=3)
    now = cfg.no_fill_minutes_threshold * 60 + 1
    result = should_update_base_price_bidirectional(state, market_price=162.5, now_timestamp=now, cfg=cfg)
    assert result is False


def test_should_update_base_price_bidirectional_false_when_no_fill_time_not_elapsed():
    from src.grid_engine import should_update_base_price_bidirectional

    cfg = GridEnvelopeConfig()
    state = DriftState(base_price=159.61, last_fill_timestamp=0.0, open_sell_count=0, open_buy_count=4)
    now = 60  # まだ1分しか経っていない
    result = should_update_base_price_bidirectional(state, market_price=157.5, now_timestamp=now, cfg=cfg)
    assert result is False


def test_synthetic_position_returns_none_for_negative_net_inventory():
    """
    現物取引では実在の空売りが発生し得ないため、net_inventory<0
    (起動時保有在庫の売り切り)はポジションなしとして扱う。
    """
    from src.grid_engine import synthetic_position_from_portfolio

    pos = synthetic_position_from_portfolio(cash_flow=8610.60, net_inventory=-40.0)
    assert pos is None


def test_synthetic_position_returns_none_for_zero_net_inventory():
    from src.grid_engine import synthetic_position_from_portfolio

    pos = synthetic_position_from_portfolio(cash_flow=0.0, net_inventory=0.0)
    assert pos is None


def test_synthetic_position_still_works_for_positive_net_inventory():
    from src.grid_engine import synthetic_position_from_portfolio

    pos = synthetic_position_from_portfolio(cash_flow=-1000.0, net_inventory=10.0)
    assert pos is not None
    assert pos.side == "buy"
    assert pos.amount == pytest.approx(10.0)


def test_should_halt_new_orders_true_when_deviation_exceeds_threshold():
    from src.grid_engine import should_halt_new_orders
    assert should_halt_new_orders(current_price=164.0, base_price=159.61, halt_deviation_jpy=4.0) is True


def test_should_halt_new_orders_false_when_within_threshold():
    from src.grid_engine import should_halt_new_orders
    assert should_halt_new_orders(current_price=162.0, base_price=159.61, halt_deviation_jpy=4.0) is False


def test_should_halt_new_orders_symmetric_for_downside():
    from src.grid_engine import should_halt_new_orders
    assert should_halt_new_orders(current_price=155.0, base_price=159.61, halt_deviation_jpy=4.0) is True
