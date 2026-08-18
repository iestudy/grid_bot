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
    # 総資金（円）。Paper Trading/本番それぞれで実際の残高に合わせて更新する
    total_capital_jpy: float = 30_000.0
    # --- ここまで聖域 ---


@dataclass(frozen=True)
class GridEnvelopeConfig:
    """
    Tier1自動変更が許容される範囲（envelope）。
    この範囲内の変更のみ、機械的条件を満たせば人間承認なしでマージ・反映してよい。
    範囲外はTier2（人間承認待ち）にフォールバックする。
    """
    grid_width_min_jpy: float = 0.4
    grid_width_max_jpy: float = 0.6
    grid_width_default_jpy: float = 0.5

    max_buy_levels: int = 5
    max_sell_levels: int = 5

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
