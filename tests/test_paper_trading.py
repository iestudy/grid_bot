import pytest

from src.paper_trading import ConservativeFillSimulator, Trade, run_simulation, summarize
from src.grid_engine import GridLevel


@pytest.fixture
def simulator():
    return ConservativeFillSimulator(default_amount=8.0, strict_penetration=True)


def test_buy_level_fills_when_sell_trade_penetrates_below(simulator):
    level = GridLevel(side="buy", price=159.11, amount=8.0)
    trade = Trade(timestamp=1000.0, side="sell", price=159.10, amount=50.0)
    fill = simulator.check_fill(level, trade)
    assert fill is not None
    assert fill.side == "buy"
    assert fill.price == 159.11
    assert fill.amount == 8.0


def test_buy_level_does_not_fill_on_exact_touch(simulator):
    # strict_penetration=Trueなので、ちょうど同値でのタッチは約定とみなさない
    level = GridLevel(side="buy", price=159.11, amount=8.0)
    trade = Trade(timestamp=1000.0, side="sell", price=159.11, amount=50.0)
    fill = simulator.check_fill(level, trade)
    assert fill is None


def test_buy_level_does_not_fill_when_price_above(simulator):
    level = GridLevel(side="buy", price=159.11, amount=8.0)
    trade = Trade(timestamp=1000.0, side="sell", price=159.50, amount=50.0)
    fill = simulator.check_fill(level, trade)
    assert fill is None


def test_sell_level_fills_when_buy_trade_penetrates_above(simulator):
    level = GridLevel(side="sell", price=160.11, amount=8.0)
    trade = Trade(timestamp=1000.0, side="buy", price=160.12, amount=50.0)
    fill = simulator.check_fill(level, trade)
    assert fill is not None
    assert fill.side == "sell"


def test_non_strict_mode_allows_exact_touch():
    simulator = ConservativeFillSimulator(default_amount=8.0, strict_penetration=False)
    level = GridLevel(side="buy", price=159.11, amount=8.0)
    trade = Trade(timestamp=1000.0, side="sell", price=159.11, amount=50.0)
    fill = simulator.check_fill(level, trade)
    assert fill is not None


def test_wrong_taker_side_does_not_fill(simulator):
    # 買い指値は、taker側が"sell"の取引でしか約定しない
    level = GridLevel(side="buy", price=159.11, amount=8.0)
    trade = Trade(timestamp=1000.0, side="buy", price=158.0, amount=50.0)
    fill = simulator.check_fill(level, trade)
    assert fill is None


def test_run_simulation_removes_filled_level_from_grid():
    levels = [
        GridLevel(side="buy", price=159.11, amount=8.0),
        GridLevel(side="buy", price=158.61, amount=8.0),
    ]
    trades = [
        Trade(timestamp=1000.0, side="sell", price=158.50, amount=100.0),  # 両方のレベルを下抜け
    ]
    fills = run_simulation(levels, iter(trades))
    # 両レベルとも約定条件を満たすため、1回のtradeで2件約定する
    assert len(fills) == 2


def test_summarize_computes_rebate():
    fills = [
        __import__("src.paper_trading", fromlist=["SimulatedFill"]).SimulatedFill(
            timestamp=1.0, side="buy", price=159.11, amount=8.0
        ),
    ]
    result = summarize(fills, maker_rebate_rate=0.0002)
    # 159.11 * 8 * 0.0002 = 0.254576
    assert result["estimated_rebate_jpy"] == pytest.approx(0.25, abs=0.01)
    assert result["buy_count"] == 1
    assert result["sell_count"] == 0


def test_compute_pnl_captures_round_trip_spread():
    from src.paper_trading import SimulatedFill, compute_pnl
    # 買い100円→売り100.5円のラウンドトリップ、amount=10
    fills = [
        SimulatedFill(timestamp=1.0, side="buy", price=100.0, amount=10.0),
        SimulatedFill(timestamp=2.0, side="sell", price=100.5, amount=10.0),
    ]
    result = compute_pnl(fills, final_price=100.5, maker_rebate_rate=0.0)
    # cash_flow = -1000 + 1005 = 5円、在庫変化なし
    assert result["cash_flow_jpy"] == pytest.approx(5.0)
    assert result["net_inventory_xrp"] == pytest.approx(0.0)
    assert result["total_pnl_jpy"] == pytest.approx(5.0)


def test_compute_pnl_values_leftover_inventory_at_final_price():
    from src.paper_trading import SimulatedFill, compute_pnl
    fills = [
        SimulatedFill(timestamp=1.0, side="buy", price=100.0, amount=10.0),
    ]
    # 売らずに保有したまま終了、期末価格110円で評価
    result = compute_pnl(fills, final_price=110.0, maker_rebate_rate=0.0)
    assert result["net_inventory_xrp"] == pytest.approx(10.0)
    # cash_flow = -1000, inventory_value = 10*110=1100 → total 100円の含み益
    assert result["total_pnl_jpy"] == pytest.approx(100.0)


def test_regime_filter_stops_replenishment_during_trend():
    from src.paper_trading import run_simulation_with_regime_filter
    from src.config import GridEnvelopeConfig

    cfg = GridEnvelopeConfig(
        grid_width_default_jpy=0.5,
        max_buy_levels=2,
        max_sell_levels=2,
        amount_per_level_xrp=8.0,
    )
    # 最初にwindow分のフラットな価格履歴を用意し、その後急落させてトレンドを発生させる
    trades = []
    t = 0.0
    for _ in range(10):
        trades.append(__import__("src.paper_trading", fromlist=["Trade"]).Trade(
            timestamp=t, side="sell", price=100.0, amount=0.001,  # 微小約定でグリッドは動かさない基準値作り
        ))
        t += 1.0
    # 急落させるtrade（トレンド判定条件を満たす）
    trades.append(__import__("src.paper_trading", fromlist=["Trade"]).Trade(
        timestamp=t, side="sell", price=90.0, amount=0.001,
    ))

    fills, trend_pause_count = run_simulation_with_regime_filter(
        base_price=100.0, cfg=cfg, trades=iter(trades),
        trend_window=10, trend_threshold_ratio=0.05,
    )
    assert trend_pause_count >= 1


def test_detect_trend_uses_only_past_reference():
    from src.grid_engine import detect_trend
    # 変化率5% → 閾値1%を超えるのでトレンド判定
    assert detect_trend(reference_price=100.0, current_price=95.0, threshold_ratio=0.01) is True
    # 変化率0.5% → 閾値1%未満なのでトレンドでない
    assert detect_trend(reference_price=100.0, current_price=99.5, threshold_ratio=0.01) is False
    # reference_price=0は不正値として常にFalse
    assert detect_trend(reference_price=0.0, current_price=100.0, threshold_ratio=0.01) is False


def test_stop_loss_triggers_full_close_and_halts():
    from src.paper_trading import run_simulation_with_stop_loss, Trade
    from src.config import GridEnvelopeConfig, HardStopLossConfig

    cfg = GridEnvelopeConfig(
        grid_width_default_jpy=0.5, max_buy_levels=2, max_sell_levels=2, amount_per_level_xrp=100.0,
    )
    hard_stop_cfg = HardStopLossConfig(
        max_drawdown_ratio=0.15, partial_close_ratio=0.08, partial_close_fraction=0.5,
        max_price_deviation_jpy=1000.0, total_capital_jpy=10_000.0,
    )
    # 買いが約定してから価格が暴落 → 含み損が閾値を超えてFULL_CLOSEが発動するはず
    trades = [
        Trade(timestamp=1.0, side="sell", price=99.0, amount=200.0),   # 買いレベル100.0を約定させる
        Trade(timestamp=2.0, side="sell", price=80.0, amount=200.0),   # 暴落 → drawdown急拡大
    ]
    fills, stop_events, halted = run_simulation_with_stop_loss(
        base_price=100.0, cfg=cfg, hard_stop_cfg=hard_stop_cfg, trades=iter(trades),
        trend_window=1, trend_threshold_ratio=0.5,  # トレンド判定はほぼ無効化して損切り判定に集中
    )
    assert halted is True
    assert any(ev["type"] == "FULL_CLOSE" for ev in stop_events)


def test_stop_loss_does_not_trigger_when_pnl_healthy():
    from src.paper_trading import run_simulation_with_stop_loss, Trade
    from src.config import GridEnvelopeConfig, HardStopLossConfig

    cfg = GridEnvelopeConfig(
        grid_width_default_jpy=0.5, max_buy_levels=2, max_sell_levels=2, amount_per_level_xrp=8.0,
    )
    hard_stop_cfg = HardStopLossConfig(
        max_drawdown_ratio=0.15, partial_close_ratio=0.08, partial_close_fraction=0.5,
        max_price_deviation_jpy=1000.0, total_capital_jpy=30_000.0,
    )
    trades = [
        Trade(timestamp=1.0, side="sell", price=99.0, amount=200.0),
        Trade(timestamp=2.0, side="buy", price=99.6, amount=200.0),
    ]
    fills, stop_events, halted = run_simulation_with_stop_loss(
        base_price=100.0, cfg=cfg, hard_stop_cfg=hard_stop_cfg, trades=iter(trades),
        trend_window=1, trend_threshold_ratio=0.5,
    )
    assert halted is False
    assert len(stop_events) == 0


def test_parameter_sweep_returns_sorted_results():
    from src.paper_trading import run_parameter_sweep, Trade
    from src.config import GridEnvelopeConfig, HardStopLossConfig

    cfg = GridEnvelopeConfig(max_buy_levels=2, max_sell_levels=2, amount_per_level_xrp=8.0)
    hard_stop_cfg = HardStopLossConfig(total_capital_jpy=30_000.0)

    trades = [
        Trade(timestamp=float(i), side="sell" if i % 2 == 0 else "buy", price=100.0 - i * 0.05, amount=50.0)
        for i in range(200)
    ]

    results = run_parameter_sweep(
        base_price=100.0, base_cfg=cfg, hard_stop_cfg=hard_stop_cfg, trades_list=trades,
        trend_windows=[50, 100], trend_thresholds=[0.01, 0.02], grid_widths=[0.3, 0.5],
    )
    assert len(results) == 2 * 2 * 2  # 2 widths * 2 windows * 2 thresholds
    # 降順ソートされていることを確認
    pnls = [r["total_pnl_jpy"] for r in results]
    assert pnls == sorted(pnls, reverse=True)


def test_sweep_grid_width_only_returns_sorted_results():
    from src.paper_trading import sweep_grid_width_only, Trade
    from src.config import GridEnvelopeConfig

    cfg = GridEnvelopeConfig(max_buy_levels=2, max_sell_levels=2, amount_per_level_xrp=8.0)
    trades = [
        Trade(timestamp=float(i), side="sell" if i % 2 == 0 else "buy", price=100.0 - i * 0.05, amount=50.0)
        for i in range(200)
    ]

    results = sweep_grid_width_only(base_price=100.0, base_cfg=cfg, trades_list=trades, widths=[0.3, 0.5, 0.8])

    assert len(results) == 3
    pnls = [r["total_pnl_jpy"] for r in results]
    assert pnls == sorted(pnls, reverse=True)
    assert {r["width"] for r in results} == {0.3, 0.5, 0.8}
