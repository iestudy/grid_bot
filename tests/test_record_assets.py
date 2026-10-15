import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

NOW = datetime(2026, 10, 15, 14, 50, tzinfo=timezone.utc)  # JST 23:50


def _load():
    spec = importlib.util.spec_from_file_location(
        "record_assets_mod", Path(__file__).resolve().parent.parent / "scripts" / "record_assets.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _data():
    return {"balances": {"jpy": {"onhand_amount": 19874.5}, "xrp": {"onhand_amount": 26.9}},
            "current_price": 208.65, "bot_state": {"net_inventory": -7.3}, "errors": []}


def test_build_record_computes_total():
    r = _load().build_record(_data(), NOW)
    assert r["total_jpy"] == round(19874.5 + 26.9 * 208.65) and r["date_jst"] == "2026-10-15"


def test_missing_values_are_none_not_zero():
    r = _load().build_record({"errors": ["boom"]}, NOW)
    assert r["total_jpy"] is None and r["jpy"] is None and "boom" in r["error"]


def test_already_recorded_ignores_error_rows(tmp_path):
    m = _load()
    p = tmp_path / "a.jsonl"
    p.write_text(json.dumps({"date_jst": "2026-10-15", "error": "x"}) + chr(10))
    assert m.already_recorded("2026-10-15", p) is False  # 失敗行があっても、再記録できる
    p.write_text(p.read_text() + json.dumps({"date_jst": "2026-10-15", "total_jpy": 1}) + chr(10))
    assert m.already_recorded("2026-10-15", p) is True
