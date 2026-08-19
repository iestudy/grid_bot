import os
import tempfile

import pytest

from src.propose_params import (
    compute_width_candidates, update_config_file, write_pr_body, _current_pnl, _write_github_output,
)
from src.config import GridEnvelopeConfig


def test_compute_width_candidates_covers_envelope_range():
    cfg = GridEnvelopeConfig(grid_width_min_jpy=0.6, grid_width_max_jpy=1.0)
    candidates = compute_width_candidates(cfg)
    assert candidates[0] == pytest.approx(0.6)
    assert candidates[-1] == pytest.approx(1.0)
    assert 0.8 in candidates


def test_current_pnl_finds_matching_width():
    results = [{"width": 0.5, "total_pnl_jpy": 10.0}, {"width": 0.8, "total_pnl_jpy": 20.0}]
    assert _current_pnl(results, 0.8) == 20.0
    assert _current_pnl(results, 0.3) is None


def test_update_config_file_replaces_grid_width_default(tmp_path, monkeypatch):
    fake_config = tmp_path / "config.py"
    fake_config.write_text(
        "grid_width_min_jpy: float = 0.6\n"
        "grid_width_max_jpy: float = 1.0\n"
        "grid_width_default_jpy: float = 0.8\n"
    )
    monkeypatch.setattr("src.propose_params.CONFIG_PATH", str(fake_config))

    update_config_file(0.9)

    content = fake_config.read_text()
    assert "grid_width_default_jpy: float = 0.9" in content
    # 他の行は変更されていないこと
    assert "grid_width_min_jpy: float = 0.6" in content


def test_write_pr_body_creates_file_with_expected_content(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    results = [
        {"width": 0.8, "total_pnl_jpy": 100.0, "net_inventory_xrp": 0.0, "fills": 20},
        {"width": 1.0, "total_pnl_jpy": 150.0, "net_inventory_xrp": 0.0, "fills": 12},
    ]
    write_pr_body(current_width=0.8, current_pnl=100.0, best=results[1], all_results=results, trades_csv="data/x.csv")

    body = (tmp_path / "PR_BODY.md").read_text()
    assert "自動マージされません" in body
    assert "0.8" in body
    assert "1.0" in body
    assert "50.0%" in body  # (150/100 - 1) * 100


def test_write_pr_body_handles_zero_current_pnl_without_crashing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    results = [{"width": 0.8, "total_pnl_jpy": 0.0, "net_inventory_xrp": 0.0, "fills": 0}]
    write_pr_body(current_width=0.8, current_pnl=0.0, best=results[0], all_results=results, trades_csv="data/x.csv")
    body = (tmp_path / "PR_BODY.md").read_text()
    assert "算出不可" in body


def test_write_github_output_writes_to_file(tmp_path, monkeypatch):
    output_file = tmp_path / "gh_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    _write_github_output(True)

    content = output_file.read_text()
    assert "proposal_needed=true" in content


def test_main_proposes_when_improvement_exceeds_threshold(tmp_path, monkeypatch):
    import csv
    from src import propose_params

    # 適当な擬似トレードデータ(下落トレンド)を用意
    trades_csv = tmp_path / "trades.csv"
    with open(trades_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "side", "price", "amount"])
        writer.writeheader()
        price = 160.0
        for i in range(3000):
            side = "sell" if i % 3 != 0 else "buy"
            price -= 0.002
            writer.writerow({"timestamp": float(i), "side": side, "price": round(price, 3), "amount": 50.0})

    fake_config = tmp_path / "config.py"
    fake_config.write_text(
        "grid_width_min_jpy: float = 0.6\n"
        "grid_width_max_jpy: float = 1.0\n"
        "grid_width_default_jpy: float = 0.6\n"  # 意図的に不利そうな値からスタート
    )
    monkeypatch.setattr(propose_params, "CONFIG_PATH", str(fake_config))
    monkeypatch.chdir(tmp_path)

    output_file = tmp_path / "gh_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.setattr(
        "sys.argv",
        ["propose_params", "--trades-csv", str(trades_csv)],
    )

    propose_params.main()

    output_content = output_file.read_text()
    # 改善が見つかった場合は proposal_needed=true になり、config.pyとPR_BODY.mdが更新される
    if "proposal_needed=true" in output_content:
        assert (tmp_path / "PR_BODY.md").exists()
        updated_config = fake_config.read_text()
        assert "grid_width_default_jpy: float = 0.6" not in updated_config
    else:
        assert "proposal_needed=false" in output_content


def test_main_skips_proposal_for_empty_trades(tmp_path, monkeypatch):
    from src import propose_params

    empty_csv = tmp_path / "empty.csv"
    empty_csv.write_text("timestamp,side,price,amount\n")

    output_file = tmp_path / "gh_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.setattr("sys.argv", ["propose_params", "--trades-csv", str(empty_csv)])

    propose_params.main()

    assert "proposal_needed=false" in output_file.read_text()
