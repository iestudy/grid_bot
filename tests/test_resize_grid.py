import pytest

from src.resize_grid import (
    compute_recommended_amount_per_level,
    compute_recommended_amount_per_level_jpy,
    compute_recommended_amount_per_level_xrp,
    update_config_file,
)
from src.config import GridEnvelopeConfig


def test_compute_recommended_amount_scales_with_free_jpy():
    cfg = GridEnvelopeConfig(grid_width_default_jpy=0.8, max_buy_levels=5, max_sell_levels=5)

    # 自由JPYが少ないケース(今回のインシデントと同様の規模)。
    # XRPは両ケースとも潤沢にしてJPY側が制約になるようにする。
    small = compute_recommended_amount_per_level(free_jpy=850.0, free_xrp=1000.0, base_price=234.0, cfg=cfg, budget_ratio=0.7)
    # 自由JPYが多いケース(当初の規模)
    large = compute_recommended_amount_per_level(free_jpy=8300.0, free_xrp=1000.0, base_price=159.0, cfg=cfg, budget_ratio=0.7)

    assert small < large
    assert small > 0


def test_compute_recommended_amount_respects_budget_ratio():
    cfg = GridEnvelopeConfig(grid_width_default_jpy=0.8, max_buy_levels=5, max_sell_levels=5)

    conservative = compute_recommended_amount_per_level(free_jpy=1000.0, free_xrp=1000.0, base_price=200.0, cfg=cfg, budget_ratio=0.5)
    aggressive = compute_recommended_amount_per_level(free_jpy=1000.0, free_xrp=1000.0, base_price=200.0, cfg=cfg, budget_ratio=0.9)

    assert aggressive > conservative


def test_compute_recommended_amount_matches_actual_incident_numbers():
    """
    今回のインシデントで実際に発生した数値(自由JPY約850円、価格約234円)を使い、
    推奨値が「小さいが現実的な数量」になることを確認する。XRPは潤沢とする。
    """
    cfg = GridEnvelopeConfig(grid_width_default_jpy=0.8, max_buy_levels=5, max_sell_levels=5)
    recommended = compute_recommended_amount_per_level(free_jpy=850.43, free_xrp=1000.0, base_price=234.5, cfg=cfg, budget_ratio=0.7)

    # 8XRP(元の設定)よりは大幅に小さいはず
    assert recommended < 8.0
    assert recommended > 0.0


def test_compute_recommended_amount_uses_xrp_constraint_when_binding():
    """
    XRPが枯渇していてJPYが潤沢な場合、XRP側の推奨値が採用されることを確認する
    (2026-09-14/09-20に実際に発生した「JPY基準のみで計算し、売りグリッドが
    自由XRP残高の数百万%を要求する」問題の回帰テスト)。
    """
    cfg = GridEnvelopeConfig(grid_width_default_jpy=0.8, max_buy_levels=5, max_sell_levels=5)

    jpy_based = compute_recommended_amount_per_level_jpy(free_jpy=30000.0, base_price=220.0, cfg=cfg, budget_ratio=0.7)
    xrp_based = compute_recommended_amount_per_level_xrp(free_xrp=5.0, cfg=cfg, budget_ratio=0.7)
    recommended = compute_recommended_amount_per_level(free_jpy=30000.0, free_xrp=5.0, base_price=220.0, cfg=cfg, budget_ratio=0.7)

    assert xrp_based < jpy_based, "このテストの前提(XRP側が制約になる)が崩れている"
    assert recommended == xrp_based

    # 採用された推奨値で売りグリッドを組んでも、自由XRP残高の70%以内に収まることを確認
    required_xrp = recommended * cfg.max_sell_levels
    assert required_xrp <= 5.0 * 0.7 + 1e-6


def test_compute_recommended_amount_uses_jpy_constraint_when_binding():
    """XRPが潤沢でJPYが枯渇している場合、JPY側の推奨値が採用されることを確認する。"""
    cfg = GridEnvelopeConfig(grid_width_default_jpy=0.8, max_buy_levels=5, max_sell_levels=5)

    jpy_based = compute_recommended_amount_per_level_jpy(free_jpy=500.0, base_price=220.0, cfg=cfg, budget_ratio=0.7)
    xrp_based = compute_recommended_amount_per_level_xrp(free_xrp=200.0, cfg=cfg, budget_ratio=0.7)
    recommended = compute_recommended_amount_per_level(free_jpy=500.0, free_xrp=200.0, base_price=220.0, cfg=cfg, budget_ratio=0.7)

    assert jpy_based < xrp_based, "このテストの前提(JPY側が制約になる)が崩れている"
    assert recommended == jpy_based


def test_update_config_file_replaces_amount_per_level(tmp_path, monkeypatch):
    fake_config = tmp_path / "config.py"
    fake_config.write_text(
        "amount_per_level_xrp: float = 8.0\n"
        "grid_width_default_jpy: float = 0.8\n"
    )
    monkeypatch.setattr("src.resize_grid.CONFIG_PATH", str(fake_config))

    update_config_file(0.6)

    content = fake_config.read_text()
    assert "amount_per_level_xrp: float = 0.6" in content
    assert "grid_width_default_jpy: float = 0.8" in content  # 他の行は無傷
