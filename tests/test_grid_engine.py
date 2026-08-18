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
    assert apply_envelope_clamp(0.9, cfg) == cfg.grid_width_max_jpy
    assert apply_envelope_clamp(0.5, cfg) == 0.5


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
