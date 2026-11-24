from types import SimpleNamespace

from src.inventory_cap import exclude_buys_if_over_cap, inventory_ratio


def _levels():
    return [SimpleNamespace(side="buy"), SimpleNamespace(side="sell")]


def test_ratio_computation():
    assert round(inventory_ratio(57.35, 12133.0, 154.5), 3) == 0.422


def test_ratio_none_when_unknown():
    assert inventory_ratio(None, 100.0, 150.0) is None
    assert inventory_ratio(1.0, None, 150.0) is None
    assert inventory_ratio(1.0, 100.0, 0) is None
    assert inventory_ratio(0.0, 0.0, 150.0) is None


def test_buys_removed_at_or_over_cap_but_sells_kept():
    assert [l.side for l in exclude_buys_if_over_cap(_levels(), 0.41)] == ["sell"]
    assert [l.side for l in exclude_buys_if_over_cap(_levels(), 0.40)] == ["sell"]
    assert len(exclude_buys_if_over_cap(_levels(), 0.39)) == 2


def test_unknown_ratio_does_not_stop_trading():
    assert len(exclude_buys_if_over_cap(_levels(), None)) == 2


def _lv(side, price, amount):
    return SimpleNamespace(side=side, price=price, amount=amount)


def test_projected_limit_stops_buys_before_cap():
    from src.inventory_cap import limit_buys_by_projected_ratio
    levels = [_lv("buy", 157.0 - i, 13.0) for i in range(5)] + [_lv("sell", 159.0, 0.1)]
    out = limit_buys_by_projected_ratio(levels, xrp=2.35, jpy=20919.0, price=158.0)
    buys = [l for l in out if l.side == "buy"]
    assert 1 <= len(buys) < 5
    assert any(l.side == "sell" for l in out)


def test_projected_limit_keeps_all_when_small():
    from src.inventory_cap import limit_buys_by_projected_ratio
    levels = [_lv("buy", 149.0 - i, 4.0) for i in range(5)]
    assert len(limit_buys_by_projected_ratio(levels, xrp=10.0, jpy=19000.0, price=150.0)) == 5


def test_projected_limit_unknown_balance_is_unchanged():
    from src.inventory_cap import limit_buys_by_projected_ratio
    levels = [_lv("buy", 100.0, 5.0)]
    assert limit_buys_by_projected_ratio(levels, None, 1.0, 100.0) == levels


def test_projected_limit_drops_lowest_buy_at_boundary():
    from src.inventory_cap import limit_buys_by_projected_ratio
    levels = [_lv("buy", 149.0 - i, 4.0) for i in range(5)]
    out = limit_buys_by_projected_ratio(levels, xrp=35.0, jpy=15000.0, price=150.0)
    prices = sorted(l.price for l in out if l.side == "buy")
    assert len(prices) == 4 and min(prices) == 146.0  # 最も安い145.0が外れる
