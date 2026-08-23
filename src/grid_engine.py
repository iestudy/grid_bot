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
    amount: float = 0.0   # このレベルで発注する数量(XRP)


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
        levels.append(GridLevel(
            side="buy",
            price=round(base_price - width * i, 4),
            amount=cfg.amount_per_level_xrp,
        ))
    for i in range(1, cfg.max_sell_levels + 1):
        levels.append(GridLevel(
            side="sell",
            price=round(base_price + width * i, 4),
            amount=cfg.amount_per_level_xrp,
        ))
    return levels


def estimate_total_capital_jpy(assets: list, current_price: float) -> float:
    """
    bitbank get_assets() の 'assets' リストから総資金(円換算)を計算する。
    onhand_amount（locked含む）を使う。ロック中の資金も自分の資産であり、
    ストップロス判定の母数からは除外すべきではないため。

    使用例:
        data = client.get_assets()
        total = estimate_total_capital_jpy(data["assets"], current_price=159.6)
    """
    total = 0.0
    for a in assets:
        if a["asset"] == "jpy":
            total += float(a["onhand_amount"])
        elif a["asset"] == "xrp":
            total += float(a["onhand_amount"]) * current_price
    return total


def required_buy_side_jpy(cfg: GridEnvelopeConfig, base_price: float) -> float:
    """買いグリッド全レベルを約定させるために必要なJPY総額（発注時の目安）。"""
    width = cfg.grid_width_default_jpy
    total = 0.0
    for i in range(1, cfg.max_buy_levels + 1):
        price = base_price - width * i
        total += price * cfg.amount_per_level_xrp
    return total


def required_sell_side_xrp(cfg: GridEnvelopeConfig) -> float:
    """売りグリッド全レベル分に必要なXRP総量。"""
    return cfg.amount_per_level_xrp * cfg.max_sell_levels


def synthetic_position_from_portfolio(cash_flow: float, net_inventory: float, cost_side_hint: str = None):
    """
    state_store.PortfolioState(cash_flow, net_inventory)から、
    HardStopLossManagerがそのまま評価できる単一のPositionを合成する。

    数学的根拠: unrealized_pnl = (price - cost_price) * amount という
    HardStopLossManagerの計算式に対して、cost_price = -cash_flow / net_inventory
    と置くと、結果はバックテストのcompute_pnlが返す
    total_pnl(rebate除く) = cash_flow + net_inventory * current_price
    と完全に一致する。個々の約定ロットを追跡しなくても、累積した
    cash_flow/net_inventoryの2値だけで正確な含み損益判定ができる。

    net_inventory <= 0 の場合はポジションなし(None)として扱う。
    現物取引では実在の空売りは発生し得ないため、net_inventoryが負値になるのは
    「起動時点で保有していた在庫を売り切った」という会計上のラベルに過ぎず、
    買い戻しで解消すべき実在のリスクポジションではない。これを実在のショート
    ポジションとして扱うと、価格が上昇し続ける限り無限に「含み損」が拡大する
    見かけ上の計算になり、不要な強制決済(買い戻し)を誘発してしまう
    (実際に本番運用で、EMERGENCY_STOP時に不要な買い戻しを試みる事故があった)。
    """
    from .hard_stop_loss import Position

    if net_inventory <= 0:
        return None
    cost_price = -cash_flow / net_inventory
    side = "buy"
    return Position(side=side, price=cost_price, amount=abs(net_inventory))


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


def should_update_base_price_bidirectional(
    state: DriftState,
    market_price: float,
    now_timestamp: float,
    cfg: GridEnvelopeConfig,
) -> bool:
    """
    should_update_base_priceの双方向版。
    売りグリッド0本(下落トレンドで価格がbase_priceを下抜けたケース)だけでなく、
    買いグリッド0本(上昇トレンドで価格がbase_priceを上抜けたケース)でも
    同様にドリフト補正の対象とする。本番のrun_loopではこちらを使用する。
    """
    no_fill_minutes = (now_timestamp - state.last_fill_timestamp) / 60.0
    if no_fill_minutes < cfg.no_fill_minutes_threshold:
        return False
    drift = abs(market_price - state.base_price)
    if drift < cfg.drift_trigger_deviation_jpy:
        return False
    return state.open_sell_count == 0 or state.open_buy_count == 0


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


def detect_trend(reference_price: float, current_price: float, threshold_ratio: float) -> bool:
    """
    直近の基準価格(reference_price)から現在価格(current_price)までの変化率が
    threshold_ratio以上なら「トレンド」と判定する。

    重要: reference_priceは必ず「判定時点より過去」の価格を渡すこと。
    未来の価格を混ぜると先読みバイアスになり、バックテスト結果が無意味になる。

    これはClaude APIによるレジーム分類の代替として、決定論的・再現可能な
    バックテスト用に用意した簡易プロキシである。本番のレジーム判定ロジック
    そのものではない。
    """
    if reference_price <= 0:
        return False
    change_ratio = abs(current_price - reference_price) / reference_price
    return change_ratio >= threshold_ratio
