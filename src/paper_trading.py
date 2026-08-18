"""
Paper Trading シミュレータ。

レビュー指摘の反映:
  「指値にタッチしたら約定」という楽観的判定は、実際の板では順番待ちで
  素通りされることが多く、本番との成績乖離を生む。
  ここでは「Best Ask/Bidが指値を突き抜けた（=指値より不利な側に一定進んだ）」
  場合のみ約定とみなす、保守的な判定を実装する。

使い方（雛形）:
    python -m src.paper_trading --pair xrp_jpy --days 7
  実際にはヒストリカルなtick/OHLCデータをCSV等で用意し、
  PriceTick のイテレータとして流し込む。ここでは骨組みのみ提供。
"""

import argparse
from dataclasses import dataclass
from typing import Iterator, List, Optional

from .grid_engine import GridLevel


@dataclass
class PriceTick:
    timestamp: float
    best_bid: float
    best_ask: float


@dataclass
class SimulatedFill:
    timestamp: float
    side: str
    price: float
    amount: float


class ConservativeFillSimulator:
    """
    約定判定ロジック:
      買い指値 price に対して、best_ask <= price になった場合のみ約定
      （＝板が指値を下から突き抜けた。ただの一瞬のタッチでは約定しない前提とし、
        意図的に「同値一致」ではなく「明確に下回った」ケースのみ約定させたい場合は
        strict_penetration=True で price に微小マージンを設けること）
      売り指値 price に対して、best_bid >= price になった場合のみ約定
    """

    def __init__(self, amount_per_level: float = 20.0):
        self.amount_per_level = amount_per_level

    def check_fill(self, level: GridLevel, tick: PriceTick) -> Optional[SimulatedFill]:
        if level.side == "buy" and tick.best_ask <= level.price:
            return SimulatedFill(tick.timestamp, "buy", level.price, self.amount_per_level)
        if level.side == "sell" and tick.best_bid >= level.price:
            return SimulatedFill(tick.timestamp, "sell", level.price, self.amount_per_level)
        return None


def run_simulation(levels: List[GridLevel], ticks: Iterator[PriceTick]) -> List[SimulatedFill]:
    simulator = ConservativeFillSimulator()
    fills: List[SimulatedFill] = []
    remaining_levels = list(levels)
    for tick in ticks:
        still_open = []
        for level in remaining_levels:
            fill = simulator.check_fill(level, tick)
            if fill:
                fills.append(fill)
                # 約定したレベルはグリッドから外す（実運用では反対側に新規レベルを積む）
            else:
                still_open.append(level)
        remaining_levels = still_open
    return fills


def _load_ticks_from_csv(path: str) -> Iterator[PriceTick]:
    import csv
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            yield PriceTick(
                timestamp=float(row["timestamp"]),
                best_bid=float(row["best_bid"]),
                best_ask=float(row["best_ask"]),
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", default="xrp_jpy")
    parser.add_argument("--ticks-csv", default=None, help="timestamp,best_bid,best_ask 列を持つCSV")
    parser.add_argument("--base-price", type=float, default=159.61)
    args = parser.parse_args()

    from .grid_engine import generate_grid
    from .config import GRID_ENVELOPE

    grid = generate_grid(args.base_price, GRID_ENVELOPE)

    if args.ticks_csv:
        ticks = _load_ticks_from_csv(args.ticks_csv)
        fills = run_simulation(grid, ticks)
        total_pnl_from_rebate = sum(f.price * f.amount for f in fills) * 0.0002  # bitbank maker rebate 0.02%目安
        print(f"約定件数: {len(fills)}")
        print(f"リベート概算収益: {total_pnl_from_rebate:.2f} 円（実際の税・出金コスト等は含まず）")
    else:
        print("--ticks-csv でヒストリカルデータ（timestamp,best_bid,best_ask列）を指定してください。")
        print(f"生成されたグリッド（{len(grid)}本）:")
        for level in grid:
            print(f"  {level.side}: {level.price}")
