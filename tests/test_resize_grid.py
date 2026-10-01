import pytest

from src.resize_grid import (
    compute_recommended_buy_amount_per_level,
    compute_recommended_sell_amount_per_level,
    update_config_file,
)
from src.config import GridEnvelopeConfig


def test_compute_recommended_buy_amount_scales_with_free_jpy():
    cfg = GridEnvelopeConfig(grid_width_default_jpy=0.8, max_buy_levels=5, max_sell_levels=5)

    # 自由JPYが少ないケース(今回のインシデントと同様の規模)
    small = compute_recommended_buy_amount_per_level(free_jpy=850.0, base_price=234.0, cfg=cfg, budget_ratio=0.7)
    # 自由JPYが多いケース(当初の規模)
    large = compute_recommended_buy_amount_per_level(free_jpy=8300.0, base_price=159.0, cfg=cfg, budget_ratio=0.7)

    assert small < large
    assert small > 0


def test_compute_recommended_sell_amount_scales_with_free_xrp():
    cfg = GridEnvelopeConfig(grid_width_default_jpy=0.8, max_buy_levels=5, max_sell_levels=5)

    small = compute_recommended_sell_amount_per_level(free_xrp=5.0, cfg=cfg, budget_ratio=0.7)
    large = compute_recommended_sell_amount_per_level(free_xrp=500.0, cfg=cfg, budget_ratio=0.7)

    assert small < large
    assert small > 0


def test_compute_recommended_amount_respects_budget_ratio():
    cfg = GridEnvelopeConfig(grid_width_default_jpy=0.8, max_buy_levels=5, max_sell_levels=5)

    conservative = compute_recommended_buy_amount_per_level(free_jpy=1000.0, base_price=200.0, cfg=cfg, budget_ratio=0.5)
    aggressive = compute_recommended_buy_amount_per_level(free_jpy=1000.0, base_price=200.0, cfg=cfg, budget_ratio=0.9)

    assert aggressive > conservative


def test_compute_recommended_amount_matches_actual_incident_numbers():
    """
    今回のインシデントで実際に発生した数値(自由JPY約850円、価格約234円)を使い、
    推奨値が「小さいが現実的な数量」になることを確認する。
    """
    cfg = GridEnvelopeConfig(grid_width_default_jpy=0.8, max_buy_levels=5, max_sell_levels=5)
    recommended = compute_recommended_buy_amount_per_level(free_jpy=850.43, base_price=234.5, cfg=cfg, budget_ratio=0.7)

    # 8XRP(元の設定)よりは大幅に小さいはず
    assert recommended < 8.0
    assert recommended > 0.0


def test_buy_and_sell_amounts_are_computed_independently():
    """
    買い側(JPY基準)・売り側(XRP基準)が互いに影響しないことを確認する
    (2026-09まで単一のamount_per_level_xrpだった際、JPY/XRPどちらかが
    枯渇すると、潤沢な側まで引きずられて小さくなる/0になる問題が
    繰り返し発生していたため、買い・売りを分離した経緯の回帰テスト)。
    """
    cfg = GridEnvelopeConfig(grid_width_default_jpy=0.8, max_buy_levels=5, max_sell_levels=5)

    # JPY潤沢・XRP枯渇のケース: 買い側は大きい値のまま、売り側だけ小さくなるべき
    buy_amount = compute_recommended_buy_amount_per_level(free_jpy=30000.0, base_price=220.0, cfg=cfg, budget_ratio=0.7)
    sell_amount = compute_recommended_sell_amount_per_level(free_xrp=5.0, cfg=cfg, budget_ratio=0.7)

    assert buy_amount > 10.0, "JPYが潤沢なら買い側は大きい値になるべき(XRP枯渇に引きずられてはいけない)"
    assert sell_amount < 1.0, "XRPが枯渇しているので売り側は小さい値になるべき"

    # 売りグリッドを組んでも自由XRP残高の70%以内に収まることを確認
    required_xrp = sell_amount * cfg.max_sell_levels
    assert required_xrp <= 5.0 * 0.7 + 1e-6


def test_update_config_file_replaces_both_amounts(tmp_path, monkeypatch):
    fake_config = tmp_path / "config.py"
    fake_config.write_text(
        "buy_amount_per_level_xrp: float = 8.0\n"
        "sell_amount_per_level_xrp: float = 8.0\n"
        "grid_width_default_jpy: float = 0.8\n"
    )
    monkeypatch.setattr("src.resize_grid.CONFIG_PATH", str(fake_config))

    update_config_file(0.6, 1.2)

    content = fake_config.read_text()
    assert "buy_amount_per_level_xrp: float = 0.6" in content
    assert "sell_amount_per_level_xrp: float = 1.2" in content
    assert "grid_width_default_jpy: float = 0.8" in content  # 他の行は無傷
