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
