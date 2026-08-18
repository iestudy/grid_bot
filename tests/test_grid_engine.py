from src.grid_engine import (
    generate_grid, should_update_base_price, update_base_price,
    apply_envelope_clamp, DriftState,
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
