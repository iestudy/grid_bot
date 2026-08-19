import pytest

from src.position_ledger import PositionLedger


def test_simple_round_trip_buy_then_sell():
    ledger = PositionLedger()
    rt1 = ledger.process_fill("buy", price=100.0, amount=8.0)
    assert rt1 == []  # 買いだけではまだ往復は成立しない

    rt2 = ledger.process_fill("sell", price=102.0, amount=8.0)
    assert len(rt2) == 1
    assert rt2[0].buy_price == 100.0
    assert rt2[0].sell_price == 102.0
    assert rt2[0].amount == 8.0
    assert rt2[0].profit_jpy == pytest.approx(16.0)  # (102-100)*8


def test_round_trip_sell_then_buy_back_cheaper():
    """先に売ってから安く買い戻すケース(既存在庫を先に売る場合など)も正しく利益計算されること"""
    ledger = PositionLedger()
    rt1 = ledger.process_fill("sell", price=160.0, amount=8.0)
    assert rt1 == []

    rt2 = ledger.process_fill("buy", price=158.0, amount=8.0)
    assert len(rt2) == 1
    assert rt2[0].profit_jpy == pytest.approx(16.0)  # (160-158)*8


def test_partial_match_across_multiple_lots():
    ledger = PositionLedger()
    ledger.process_fill("buy", price=100.0, amount=5.0)
    ledger.process_fill("buy", price=101.0, amount=5.0)

    # 8XRP分の売り → 最初のロット(100円,5XRP)を使い切り、次のロット(101円)から3XRP消費
    round_trips = ledger.process_fill("sell", price=105.0, amount=8.0)

    assert len(round_trips) == 2
    assert round_trips[0].buy_price == 100.0
    assert round_trips[0].amount == pytest.approx(5.0)
    assert round_trips[1].buy_price == 101.0
    assert round_trips[1].amount == pytest.approx(3.0)

    # 残り2XRP分は買いロットとして残っているはず
    assert len(ledger.buy_lots) == 1
    assert ledger.buy_lots[0].amount == pytest.approx(2.0)


def test_loss_making_round_trip_reports_negative_profit():
    ledger = PositionLedger()
    ledger.process_fill("buy", price=160.0, amount=8.0)
    round_trips = ledger.process_fill("sell", price=155.0, amount=8.0)

    assert round_trips[0].profit_jpy == pytest.approx(-40.0)  # (155-160)*8


def test_to_dict_and_from_dict_round_trip_preserves_state():
    ledger = PositionLedger()
    ledger.process_fill("buy", price=100.0, amount=5.0)
    ledger.process_fill("sell", price=200.0, amount=2.0)  # 一部だけ売り、3XRP分の買いロットが残る

    data = ledger.to_dict()
    restored = PositionLedger.from_dict(data)

    assert len(restored.buy_lots) == 1
    assert restored.buy_lots[0].price == pytest.approx(100.0)
    assert restored.buy_lots[0].amount == pytest.approx(3.0)


def test_invalid_side_raises():
    ledger = PositionLedger()
    with pytest.raises(ValueError):
        ledger.process_fill("hold", price=100.0, amount=1.0)
