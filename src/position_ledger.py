"""
FIFO方式のポジション追跡・往復損益計算。

grid botの全ての約定はpost_only(メイカー)の指値注文のみであり、
成行の滑り(スリッページ)が発生しない設計になっている(post_only=Trueは
テイカーになる価格では拒否される)。そのため、各約定記録のpriceは
そのまま正確な約定価格として扱える。

このモジュールは「買いロット」「売りロット」のFIFOキューを保持し、
新しい約定が入るたびに反対側のロットと相殺(マッチング)する。
マッチが成立した部分だけが「往復(ラウンドトリップ)」として損益確定する。

例: 100円で買い→102円で売り、を8XRP分行った場合
    profit_jpy = (102 - 100) * 8 = 16円 の往復益として計上される。
"""

from dataclasses import dataclass
from typing import List


@dataclass
class Lot:
    price: float
    amount: float


@dataclass
class RoundTrip:
    buy_price: float
    sell_price: float
    amount: float
    profit_jpy: float


class PositionLedger:
    def __init__(self, buy_lots: List[Lot] = None, sell_lots: List[Lot] = None):
        self.buy_lots: List[Lot] = buy_lots if buy_lots is not None else []
        self.sell_lots: List[Lot] = sell_lots if sell_lots is not None else []

    def process_fill(self, side: str, price: float, amount: float) -> List[RoundTrip]:
        """
        新しい約定をFIFOロットに反映し、成立した往復のリストを返す。
        買い→既存の売りロット(ショート)があれば先に相殺、余りは買いロットとして積む。
        売り→既存の買いロットがあれば先に相殺、余りは売りロットとして積む。
        """
        round_trips: List[RoundTrip] = []
        remaining = amount

        if side == "buy":
            while remaining > 1e-9 and self.sell_lots:
                lot = self.sell_lots[0]
                matched = min(remaining, lot.amount)
                profit = (lot.price - price) * matched
                round_trips.append(RoundTrip(buy_price=price, sell_price=lot.price, amount=matched, profit_jpy=profit))
                lot.amount -= matched
                remaining -= matched
                if lot.amount <= 1e-9:
                    self.sell_lots.pop(0)
            if remaining > 1e-9:
                self.buy_lots.append(Lot(price=price, amount=remaining))

        elif side == "sell":
            while remaining > 1e-9 and self.buy_lots:
                lot = self.buy_lots[0]
                matched = min(remaining, lot.amount)
                profit = (price - lot.price) * matched
                round_trips.append(RoundTrip(buy_price=lot.price, sell_price=price, amount=matched, profit_jpy=profit))
                lot.amount -= matched
                remaining -= matched
                if lot.amount <= 1e-9:
                    self.buy_lots.pop(0)
            if remaining > 1e-9:
                self.sell_lots.append(Lot(price=price, amount=remaining))
        else:
            raise ValueError(f"unknown side: {side}")

        return round_trips

    def to_dict(self) -> dict:
        return {
            "buy_lots": [{"price": l.price, "amount": l.amount} for l in self.buy_lots],
            "sell_lots": [{"price": l.price, "amount": l.amount} for l in self.sell_lots],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PositionLedger":
        buy_lots = [Lot(price=d["price"], amount=d["amount"]) for d in data.get("buy_lots", [])]
        sell_lots = [Lot(price=d["price"], amount=d["amount"]) for d in data.get("sell_lots", [])]
        return cls(buy_lots=buy_lots, sell_lots=sell_lots)
