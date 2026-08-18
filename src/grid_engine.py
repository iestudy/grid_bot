"""
グリッドエンジン本体。

- グリッド生成: base_priceを中心に、grid_width間隔で買い/売りレベルを生成
- base_price自動ドリフト補正: 約定ゼロが続き、売り（または買い）グリッドが枯渇し、
  市場価格がbase_priceから一定以上乖離した場合に、段階的にbase_priceを更新する

既存ポジションの利確目標（売り目標）はドリフト補正後も更新前の値を維持する方針
（含み損ポジションを塩漬けにしたまま新グリッドだけ動く二重構造を避けるため）。
"""

from dataclasses import dataclass
from typing import List, Optional

from .config import GridEnvelopeConfig


@dataclass
class GridLevel:
    side: str        # "buy" or "sell"
    price: float


@dataclass
class DriftState:
    base_price: float
    last_fill_timestamp: float           # unix time（秒）
    open_sell_count: int
    open_buy_count: int


def generate_grid(base_price: float, cfg: GridEnvelopeConfig) -> List[GridLevel]:
    levels: List[GridLevel] = []
    width = cfg.grid_width_default_jpy
    for i in range(1, cfg.max_buy_levels + 1):
        levels.append(GridLevel(side="buy", price=round(base_price - width * i, 4)))
    for i in range(1, cfg.max_sell_levels + 1):
        levels.append(GridLevel(side="sell", price=round(base_price + width * i, 4)))
    return levels


def should_update_base_price(
    state: DriftState,
    market_price: float,
    now_timestamp: float,
    cfg: GridEnvelopeConfig,
) -> bool:
    """
    以下を全て満たす場合にTrueを返す:
    1. 約定ゼロが no_fill_minutes_threshold 分以上継続
    2. 売りグリッドが0本 (下方向にドリフトしたケースを想定。買い0本のケースは
       呼び出し側で市場方向を見て逆方向にも同様のロジックを適用する)
    3. 市場価格がbase_priceから drift_trigger_deviation_jpy 円以上乖離
    """
    no_fill_minutes = (now_timestamp - state.last_fill_timestamp) / 60.0
    drift = abs(market_price - state.base_price)
    return (
        no_fill_minutes >= cfg.no_fill_minutes_threshold
        and state.open_sell_count == 0
        and drift >= cfg.drift_trigger_deviation_jpy
    )


def update_base_price(
    state: DriftState,
    market_price: float,
    step_ratio: float = 0.5,
) -> float:
    """
    急激な再配置を避けるため、乖離分の step_ratio (デフォルト50%) だけ market_price に寄せる。
    既存ポジションの利確目標は呼び出し側（発注管理層）で従来のbase_price基準のまま
    維持すること。この関数はbase_priceの新しい値を返すだけで、既存注文には触れない。
    """
    new_base_price = state.base_price + (market_price - state.base_price) * step_ratio
    return round(new_base_price, 4)


def apply_envelope_clamp(
    proposed_width: float,
    cfg: GridEnvelopeConfig,
) -> float:
    """Tier1自動変更で提案されたgrid幅をenvelope範囲内にクランプする。"""
    return max(cfg.grid_width_min_jpy, min(cfg.grid_width_max_jpy, proposed_width))
