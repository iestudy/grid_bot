import pytest

from src.resize_grid import compute_recommended_amount_per_level, update_config_file
from src.config import GridEnvelopeConfig


def test_compute_recommended_amount_scales_with_free_jpy():
    cfg = GridEnvelopeConfig(grid_width_default_jpy=0.8, max_buy_levels=5, max_sell_levels=5)

    # 自由JPYが少ないケース(今回のインシデントと同様の規模)
    small = compute_recommended_amount_per_level(free_jpy=850.0, base_price=234.0, cfg=cfg, budget_ratio=0.7)
    # 自由JPYが多いケース(当初の規模)
    large = compute_recommended_amount_per_level(free_jpy=8300.0, base_price=159.0, cfg=cfg, budget_ratio=0.7)

    assert small < large
    assert small > 0


def test_compute_recommended_amount_respects_budget_ratio():
    cfg = GridEnvelopeConfig(grid_width_default_jpy=0.8, max_buy_levels=5, max_sell_levels=5)

    conservative = compute_recommended_amount_per_level(free_jpy=1000.0, base_price=200.0, cfg=cfg, budget_ratio=0.5)
    aggressive = compute_recommended_amount_per_level(free_jpy=1000.0, base_price=200.0, cfg=cfg, budget_ratio=0.9)

    assert aggressive > conservative


def test_compute_recommended_amount_matches_actual_incident_numbers():
    """
    今回のインシデントで実際に発生した数値(自由JPY約850円、価格約234円)を使い、
    推奨値が「小さいが現実的な数量」になることを確認する。
    """
    cfg = GridEnvelopeConfig(grid_width_default_jpy=0.8, max_buy_levels=5, max_sell_levels=5)
    recommended = compute_recommended_amount_per_level(free_jpy=850.43, base_price=234.5, cfg=cfg, budget_ratio=0.7)

    # 8XRP(元の設定)よりは大幅に小さいはず
    assert recommended < 8.0
    assert recommended > 0.0


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
