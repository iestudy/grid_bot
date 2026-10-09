import json

import pytest

from src.inventory_consistency import (
    apply_consistency_override,
    check_inventory_consistency as chk,
)


# --- 整合性判定: 実際に誤判定された2件のデータ ---

def test_oct9_false_escalation_is_consistent_with_baseline():
    """2026-10-09、正当な売り越しが誤ってエスカレーションされた実データ。"""
    r = chk(net_inventory=-114.4, cash_flow=24647.07, xrp_onhand=3.9001,
            current_price=220.89, baseline_xrp=118.3)
    assert r["status"] == "consistent"
    assert r["avg_fill_price"] == pytest.approx(215.4464, rel=1e-4)


def test_oct5_false_escalation_is_consistent_with_baseline():
    """2026-10-05の実データ。"""
    r = chk(net_inventory=-115.5, cash_flow=27221.04, xrp_onhand=2.1001,
            current_price=239.68, baseline_xrp=117.6)
    assert r["status"] == "consistent"


def test_without_baseline_is_unknown_not_consistent():
    """基準値が無い場合、関係1だけでは整合と断定せず、判断を覆さない。"""
    r = chk(net_inventory=-114.4, cash_flow=24647.07, xrp_onhand=3.9001,
            current_price=220.89, baseline_xrp=None)
    assert r["status"] == "unknown"


# --- 整合性判定: 異常を見逃さないこと ---

def test_detects_remaining_xrp_that_should_have_been_sold():
    """売ったはずのXRPが実残高に多く残っている(帳簿バグの典型)。"""
    r = chk(net_inventory=-114.4, cash_flow=24647.07, xrp_onhand=60.0,
            current_price=220.89, baseline_xrp=118.3)
    assert r["status"] == "inconsistent"


def test_detects_implausible_average_price():
    """売却額が小さすぎる(平均約定価格が現在価格から大きく乖離)。"""
    r = chk(net_inventory=-114.4, cash_flow=1144.0, xrp_onhand=3.9001,
            current_price=220.89, baseline_xrp=118.3)
    assert r["status"] == "inconsistent"
    assert r["avg_fill_price"] == pytest.approx(10.0)


def test_detects_same_sign_cash_flow_and_inventory():
    """cash_flowとnet_inventoryが同符号は、売買の向きと矛盾する。"""
    r = chk(net_inventory=-114.4, cash_flow=-24647.07, xrp_onhand=3.9,
            current_price=220.89, baseline_xrp=118.3)
    assert r["status"] == "inconsistent"


def test_unknown_when_current_price_missing():
    r = chk(net_inventory=-114.4, cash_flow=24647.07, xrp_onhand=3.9,
            current_price=None, baseline_xrp=118.3)
    assert r["status"] == "unknown"


def test_long_position_normal_case_is_consistent():
    """買い越し(net_inventory>0)でも、同じ式で整合を判定できる。"""
    r = chk(net_inventory=50.0, cash_flow=-11000.0, xrp_onhand=170.0,
            current_price=221.0, baseline_xrp=120.0)
    assert r["status"] == "consistent"
    assert r["avg_fill_price"] == pytest.approx(220.0)


def test_negligible_inventory_is_consistent():
    """net_inventoryがほぼゼロ(リセット直後など)は、検証対象外。"""
    r = chk(net_inventory=0.0, cash_flow=0.0, xrp_onhand=2.1,
            current_price=220.0, baseline_xrp=None)
    assert r["status"] == "consistent"


# --- 後処理: Claudeの判断の上書き ---

ESC2 = {"action": "escalate", "matched_conditions": ["条件2: net_inventoryが異常な値"], "reasoning": "Claudeの理由"}


def _data(status):
    return {"inventory_consistency": {"status": status, "reasons": ["テスト理由"]}}


def test_override_when_consistent_and_only_condition2():
    r = apply_consistency_override(ESC2, _data("consistent"))
    assert r["action"] == "auto_recover"
    assert r["matched_conditions"] == []
    assert "Claudeの理由" in r["reasoning"]  # 元の判断が経緯として残る
    assert "コードによる在庫整合性判定" in r["reasoning"]


def test_no_override_when_other_conditions_present():
    d = {"action": "escalate",
         "matched_conditions": ["条件2: 異常", "条件1: errorsが空でない"], "reasoning": "x"}
    r = apply_consistency_override(d, _data("consistent"))
    assert r["action"] == "escalate"


@pytest.mark.parametrize("status", ["inconsistent", "unknown"])
def test_no_override_unless_consistent(status):
    assert apply_consistency_override(ESC2, _data(status))["action"] == "escalate"


def test_no_override_without_consistency_data():
    assert apply_consistency_override(ESC2, {})["action"] == "escalate"
    assert apply_consistency_override(ESC2, None)["action"] == "escalate"


def test_auto_recover_decision_is_left_alone():
    d = {"action": "auto_recover", "matched_conditions": [], "reasoning": "x"}
    assert apply_consistency_override(d, _data("consistent")) == d


def test_override_does_not_mutate_input():
    before = json.loads(json.dumps(ESC2))
    apply_consistency_override(ESC2, _data("consistent"))
    assert ESC2 == before


def test_condition2_matching_excludes_other_numbers():
    """「条件21」のような別番号を、条件2として扱わない。"""
    d = {"action": "escalate", "matched_conditions": ["条件21: 別の条件"], "reasoning": "x"}
    assert apply_consistency_override(d, _data("consistent"))["action"] == "escalate"


# --- reset_state.py: 基準値の保存 ---

def test_reset_saves_baseline_onhand_balances(tmp_path, monkeypatch):
    import src.reset_state as rs
    from unittest.mock import MagicMock

    monkeypatch.setattr(rs, "RESET_BASELINE_PATH", tmp_path / "run" / "reset_baseline.json")
    client = MagicMock()
    client.get_assets.return_value = {"assets": [
        {"asset": "jpy", "onhand_amount": "25103.636", "free_amount": "100"},
        {"asset": "xrp", "onhand_amount": "117.6001", "free_amount": "0"},
        {"asset": "btc", "onhand_amount": "9", "free_amount": "9"},
    ]}
    rs._save_reset_baseline(client)

    saved = json.loads((tmp_path / "run" / "reset_baseline.json").read_text())
    assert saved["xrp_onhand"] == pytest.approx(117.6001)  # freeではなくonhand
    assert saved["jpy_onhand"] == pytest.approx(25103.636)
    assert "btc_onhand" not in saved


def test_reset_baseline_failure_does_not_raise(tmp_path, monkeypatch):
    import src.reset_state as rs
    from unittest.mock import MagicMock

    monkeypatch.setattr(rs, "RESET_BASELINE_PATH", tmp_path / "run" / "reset_baseline.json")
    client = MagicMock()
    client.get_assets.side_effect = RuntimeError("API障害")
    rs._save_reset_baseline(client)  # 例外を投げない
    assert not (tmp_path / "run" / "reset_baseline.json").exists()


def test_reset_does_not_save_baseline_without_balances(tmp_path, monkeypatch):
    """残高が取れないとき、残高の無い基準値を書かない(検証側が壊れた基準値を読むため)。"""
    import src.reset_state as rs
    from unittest.mock import MagicMock

    path = tmp_path / "run" / "reset_baseline.json"
    monkeypatch.setattr(rs, "RESET_BASELINE_PATH", path)
    client = MagicMock()
    client.get_assets.return_value = {"assets": []}
    rs._save_reset_baseline(client)
    assert not path.exists()


def test_real_run_dir_is_not_polluted_by_tests():
    """conftestの隔離により、テストが本物のrun/reset_baseline.jsonを書かないこと。"""
    import src.reset_state as rs
    from pathlib import Path
    real = Path(__file__).resolve().parent.parent / "run" / "reset_baseline.json"
    assert rs.RESET_BASELINE_PATH != real
