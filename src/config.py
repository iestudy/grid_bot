"""
全パラメータの一元管理。
Tier1自動変更の対象はここで定義する envelope の範囲内に限定される。
HARD_STOP_LOSS 関連の値は Tier1/Tier2 いずれの自動変更パイプラインからも
書き換えを禁止する（変更する場合は必ず人間が直接このファイルをレビュー・コミットする）。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class HardStopLossConfig:
    # --- ここから下は自動変更対象外（聖域） ---
    # 累積含み損がこの割合を超えたら即座に全決済トリガー
    max_drawdown_ratio: float = 0.15          # 資金の15%
    # 累積含み損がこの割合を超えたら部分決済トリガー（早期警戒ライン）
    partial_close_ratio: float = 0.08         # 資金の8%
    # 部分決済時に閉じる建玉の割合
    partial_close_fraction: float = 0.5
    # base_priceからの乖離がこの絶対値(円)を超えたら緊急停止（グリッド下限/上限の逸脱）
    max_price_deviation_jpy: float = 8.0
    # 総資金（円）。Paper Trading/本番それぞれで実際の残高に合わせて更新する。
    # 静的な設定値は簡易版であり、本来は毎サイクルget_assets()を呼んで
    # estimate_total_capital_jpy()（grid_engine.py）で動的に計算するのが望ましい。
    # 以下はキャンセル後の実残高（JPY自由8,300円 + XRP保有120枚×約159.6円）に基づく初期値。
    total_capital_jpy: float = 27_450.0
    # --- ここまで聖域 ---


@dataclass(frozen=True)
class GridEnvelopeConfig:
    """
    Tier1自動変更が許容される範囲（envelope）。
    この範囲内の変更のみ、機械的条件を満たせば人間承認なしでマージ・反映してよい。
    範囲外はTier2（人間承認待ち）にフォールバックする。
    """
    grid_width_min_jpy: float = 0.6
    grid_width_max_jpy: float = 1.0
    grid_width_default_jpy: float = 0.8

    max_buy_levels: int = 5
    max_sell_levels: int = 5

    # 1レベルあたりの数量(XRP)。実際の保有資産(JPY自由残高・XRP保有量)から逆算した初期値。
    # 口座残高が変わったら、estimate_total_capital_jpy()の結果を見ながら調整すること。
    amount_per_level_xrp: float = 8.0

    # base_priceからの片道乖離がこの値以上になったら、新規発注(sync_grid_orders)を
    # 一時停止する(既存注文はそのまま、HardStopLossManagerの判定・キャンセルも
    # 別途独立して機能する)。EMERGENCY_STOP閾値(HardStopLossConfig.max_price_deviation_jpy、
    # デフォルト8円)より内側で発動する「早期警戒ライン」として機能させる。
    # 以前否決したレジームフィルタ(trend_window/threshold)とは異なり、
    # 既に信頼しているbase_price乖離という単一指標のみを使う設計。
    new_order_halt_deviation_min_jpy: float = 2.0
    new_order_halt_deviation_max_jpy: float = 6.0
    new_order_halt_deviation_jpy: float = 4.0

    # 24時間の累積ドリフト上限（初期値からの乖離率）。これを超えたら強制的にTier2へ
    cumulative_drift_limit_ratio: float = 0.30

    # base_price自動更新の発動条件
    no_fill_minutes_threshold: int = 120
    drift_trigger_deviation_jpy: float = 1.5


@dataclass(frozen=True)
class CircuitBreakerConfig:
    # 直近のTier1自動rollbackがこの回数連続したら、Tier1自動展開を一時停止
    max_consecutive_rollbacks: int = 3
    # 一時停止後、この分数は新規発注も停止する
    cooldown_minutes: int = 60


HARD_STOP_LOSS = HardStopLossConfig()
GRID_ENVELOPE = GridEnvelopeConfig()
CIRCUIT_BREAKER = CircuitBreakerConfig()
