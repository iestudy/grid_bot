"""
在庫上限。XRPの評価額が総資産のcap(既定40%)以上なら、新規の買いを出さない。

背景: 2026-11、価格が約21%下落する間にグリッドが買い下がり続け、JPYがほぼ全額
XRPになって総資産が約16%減った。売りが約定しない一方向の下落では、買いの積み上がりを
止める規則が必要。売りは除かない(在庫を減らす側は常に出す)。
残高が取れないとき(Noneなど)は、買いを除かない(取得失敗だけで取引が止まらないように)。
"""
from typing import List, Optional

DEFAULT_CAP = 0.40


def inventory_ratio(xrp: Optional[float], jpy: Optional[float], price: Optional[float]) -> Optional[float]:
    """XRP評価額 / 総資産。計算できなければNone。"""
    if xrp is None or jpy is None or not price:
        return None
    total = jpy + xrp * price
    if total <= 0:
        return None
    return xrp * price / total


def exclude_buys_if_over_cap(levels: List, ratio: Optional[float], cap: float = DEFAULT_CAP) -> List:
    """比率がcap以上なら買いのレベルを除く。ratioがNoneなら、そのまま返す。"""
    if ratio is None or ratio < cap:
        return levels
    return [l for l in levels if l.side != "buy"]


DEFAULT_BUY_TOTAL_CAP = 0.25  # 出す買いの総額が総資産のこの割合を超えないようにする


def limit_buys_by_projected_ratio(levels, xrp, jpy, price, cap=DEFAULT_CAP, buy_total_cap=DEFAULT_BUY_TOTAL_CAP):
    '''買いを価格の高い順に採用し、全部約定した場合の想定在庫比率がcapを超える手前、
    または買いの総額が総資産のbuy_total_capを超える手前で止める。売りは除かない。
    残高や価格が不明(None)なら、そのまま返す(取得失敗で取引を止めない)。
    背景: 2026-11-24、起動直後は比率0%で13XRP x 5本が全部出て、全約定後にHardStopで約-800円。'''
    if xrp is None or jpy is None or not price:
        return levels
    total = jpy + xrp * price
    if total <= 0:
        return levels
    held_value = xrp * price
    buys = sorted([l for l in levels if l.side == 'buy'], key=lambda l: -l.price)
    keep = []
    buy_value = 0.0
    for l in buys:
        v = l.amount * l.price
        if (held_value + buy_value + v) / total > cap or (buy_value + v) / total > buy_total_cap:
            break
        keep.append(l)
        buy_value += v
    kept_ids = set(id(l) for l in keep)
    return [l for l in levels if l.side != 'buy' or id(l) in kept_ids]

