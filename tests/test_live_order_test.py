import pytest

from src.live_order_test import compute_safe_price


def test_buy_price_is_one_tick_below_best_buy():
    price = compute_safe_price("buy", best_buy=159.642, best_sell=159.643)
    assert price == pytest.approx(159.641)


def test_sell_price_is_one_tick_above_best_sell():
    price = compute_safe_price("sell", best_buy=159.642, best_sell=159.643)
    assert price == pytest.approx(159.644)


def test_buy_price_never_crosses_best_sell():
    # 買い指値は必ず最良売り気配より低い（post_onlyが拒否されない条件）
    price = compute_safe_price("buy", best_buy=159.642, best_sell=159.643)
    assert price < 159.643


def test_sell_price_never_crosses_best_buy():
    price = compute_safe_price("sell", best_buy=159.642, best_sell=159.643)
    assert price > 159.642


def test_invalid_side_raises():
    with pytest.raises(ValueError):
        compute_safe_price("hold", best_buy=159.642, best_sell=159.643)
