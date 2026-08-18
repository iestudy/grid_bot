import pytest

from src.hard_stop_loss import HardStopLossManager, Position, Action
from src.config import HardStopLossConfig


@pytest.fixture
def cfg():
    return HardStopLossConfig(
        max_drawdown_ratio=0.15,
        partial_close_ratio=0.08,
        partial_close_fraction=0.5,
        max_price_deviation_jpy=8.0,
        total_capital_jpy=30_000.0,
    )


@pytest.fixture
def manager(cfg):
    # base_price = 159.61 は過去のログに登場した実例に合わせる
    return HardStopLossManager(cfg, base_price=159.61)


@pytest.fixture
def manager_wide_deviation(cfg):
    """
    drawdown(含み損)判定のみを独立して検証するためのfixture。
    max_price_deviation_jpyを広く取り、EMERGENCY_STOPが先に発火しないようにする。
    実運用ではgrid幅・ストップロス幅の設計上、通常ここまで価格が乖離する前に
    EMERGENCY_STOPが先に発火するのが正しい挙動であり、これはあくまで
    drawdown_ratioの計算ロジック単体の正しさを検証するためのテストである。
    """
    wide_cfg = HardStopLossConfig(
        max_drawdown_ratio=cfg.max_drawdown_ratio,
        partial_close_ratio=cfg.partial_close_ratio,
        partial_close_fraction=cfg.partial_close_fraction,
        max_price_deviation_jpy=1000.0,
        total_capital_jpy=cfg.total_capital_jpy,
    )
    return HardStopLossManager(wide_cfg, base_price=159.61)


def test_no_positions_returns_none(manager):
    result = manager.evaluate(current_price=158.0, positions=[])
    assert result.action == Action.NONE
    assert result.unrealized_pnl_jpy == 0.0


def test_small_unrealized_loss_within_safe_range(manager):
    # 想定: 4本×20XRPで含み損 約82円程度（過去ログの実例規模） → 資金の0.27%程度、安全圏
    positions = [
        Position(side="buy", price=159.11, amount=20),
        Position(side="buy", price=158.61, amount=20),
        Position(side="buy", price=158.11, amount=20),
        Position(side="buy", price=157.61, amount=20),
    ]
    result = manager.evaluate(current_price=157.5, positions=positions)
    assert result.action == Action.NONE
    assert result.drawdown_ratio < 0.08


def test_partial_close_triggered_at_threshold(manager_wide_deviation, cfg):
    # 資金3万円の8%=2400円の損失となる価格を逆算
    positions = [Position(side="buy", price=160.0, amount=100)]
    # 含み損2400円 = (160 - current_price) * 100 → current_price = 160 - 24 = 136
    result = manager_wide_deviation.evaluate(current_price=136.0, positions=positions)
    assert result.action == Action.PARTIAL_CLOSE
    assert result.close_amount == pytest.approx(50.0)  # 半分クローズ
    assert result.drawdown_ratio == pytest.approx(0.08, rel=1e-3)


def test_full_close_triggered_at_threshold(manager_wide_deviation, cfg):
    # 資金3万円の15%=4500円の損失となる価格を逆算
    positions = [Position(side="buy", price=160.0, amount=100)]
    # (160 - current_price) * 100 = 4500 → current_price = 115
    result = manager_wide_deviation.evaluate(current_price=115.0, positions=positions)
    assert result.action == Action.FULL_CLOSE
    assert result.close_amount == pytest.approx(100.0)


def test_sell_position_pnl_direction(manager_wide_deviation):
    # 売り建玉は価格上昇で含み損になる
    positions = [Position(side="sell", price=155.0, amount=100)]
    # (price - current) * amount = (155-180)*100 = -2500 → loss 2500 → ratio 0.0833 → partial close
    result = manager_wide_deviation.evaluate(current_price=180.0, positions=positions)
    assert result.action == Action.PARTIAL_CLOSE


def test_unrealized_profit_does_not_trigger_stop(manager_wide_deviation):
    positions = [Position(side="buy", price=150.0, amount=100)]
    # 含み益になる方向（価格上昇）はストップロス対象外
    result = manager_wide_deviation.evaluate(current_price=200.0, positions=positions)
    assert result.action == Action.NONE
    assert result.unrealized_pnl_jpy > 0


def test_emergency_stop_on_grid_deviation(manager):
    # base_price=159.61、max_price_deviation_jpy=8.0 → 151.61以下でEMERGENCY_STOP
    positions = [Position(side="buy", price=155.0, amount=10)]
    result = manager.evaluate(current_price=151.0, positions=positions)
    assert result.action == Action.EMERGENCY_STOP


def test_emergency_stop_takes_priority_over_partial_close(manager):
    # 逸脱条件を満たす場合、drawdownがpartial close圏でもEMERGENCY_STOPが優先される
    positions = [Position(side="buy", price=160.0, amount=100)]
    result = manager.evaluate(current_price=140.0, positions=positions)  # 逸脱19.61円
    assert result.action == Action.EMERGENCY_STOP


def test_invalid_side_raises(manager):
    positions = [Position(side="hold", price=150.0, amount=10)]
    with pytest.raises(ValueError):
        manager.evaluate(current_price=150.0, positions=positions)
