"""
HardStopLossManager — 資金防御の最終防衛線。

設計原則:
- 外部API（Claude API、取引所API）に一切依存しない、純粋な数値ロジックのみで判定する
- レジーム判定やAI分類の結果を待たない。入力は「現在価格」と「保有ポジション」だけ
- Tier1/Tier2いずれの自動パラメータ変更パイプラインの対象からも除外する（config.pyのHARD_STOP_LOSSを直接編集する場合のみ変更可）
"""

from dataclasses import dataclass
from enum import Enum
from typing import List

from .config import HardStopLossConfig


class Action(Enum):
    NONE = "NONE"
    PARTIAL_CLOSE = "PARTIAL_CLOSE"
    FULL_CLOSE = "FULL_CLOSE"
    EMERGENCY_STOP = "EMERGENCY_STOP"  # grid下限/上限を割った場合、新規発注も含め全停止


@dataclass
class Position:
    side: str          # "buy" または "sell"
    price: float        # 約定価格
    amount: float        # 数量


@dataclass
class Evaluation:
    action: Action
    unrealized_pnl_jpy: float
    drawdown_ratio: float
    reason: str
    close_amount: float = 0.0  # PARTIAL_CLOSE時に閉じるべき数量（正の値）


class HardStopLossManager:
    def __init__(self, cfg: HardStopLossConfig, base_price: float):
        self.cfg = cfg
        self.base_price = base_price

    @staticmethod
    def _unrealized_pnl(positions: List[Position], current_price: float) -> float:
        """買い建玉は (現在価格-約定価格)*数量、売り建玉は逆符号"""
        pnl = 0.0
        for p in positions:
            if p.side == "buy":
                pnl += (current_price - p.price) * p.amount
            elif p.side == "sell":
                pnl += (p.price - current_price) * p.amount
            else:
                raise ValueError(f"unknown side: {p.side}")
        return pnl

    def evaluate(self, current_price: float, positions: List[Position]) -> Evaluation:
        # 1. グリッド逸脱の緊急停止判定（最優先でチェック）
        deviation = abs(current_price - self.base_price)
        if deviation >= self.cfg.max_price_deviation_jpy:
            return Evaluation(
                action=Action.EMERGENCY_STOP,
                unrealized_pnl_jpy=self._unrealized_pnl(positions, current_price),
                drawdown_ratio=0.0,
                reason=(
                    f"price deviation {deviation:.2f} JPY exceeded "
                    f"max_price_deviation_jpy={self.cfg.max_price_deviation_jpy}"
                ),
            )

        if not positions:
            return Evaluation(
                action=Action.NONE,
                unrealized_pnl_jpy=0.0,
                drawdown_ratio=0.0,
                reason="no open positions",
            )

        pnl = self._unrealized_pnl(positions, current_price)
        # 損失のみを評価対象にする（含み益はストップロス判定に影響させない）
        loss = max(0.0, -pnl)
        drawdown_ratio = loss / self.cfg.total_capital_jpy

        if drawdown_ratio >= self.cfg.max_drawdown_ratio:
            return Evaluation(
                action=Action.FULL_CLOSE,
                unrealized_pnl_jpy=pnl,
                drawdown_ratio=drawdown_ratio,
                reason=(
                    f"drawdown_ratio {drawdown_ratio:.4f} >= "
                    f"max_drawdown_ratio {self.cfg.max_drawdown_ratio}"
                ),
                close_amount=sum(p.amount for p in positions),
            )

        if drawdown_ratio >= self.cfg.partial_close_ratio:
            total_amount = sum(p.amount for p in positions)
            return Evaluation(
                action=Action.PARTIAL_CLOSE,
                unrealized_pnl_jpy=pnl,
                drawdown_ratio=drawdown_ratio,
                reason=(
                    f"drawdown_ratio {drawdown_ratio:.4f} >= "
                    f"partial_close_ratio {self.cfg.partial_close_ratio}"
                ),
                close_amount=total_amount * self.cfg.partial_close_fraction,
            )

        return Evaluation(
            action=Action.NONE,
            unrealized_pnl_jpy=pnl,
            drawdown_ratio=drawdown_ratio,
            reason="within safe range",
        )
