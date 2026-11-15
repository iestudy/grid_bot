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
