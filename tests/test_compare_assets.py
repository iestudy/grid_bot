import importlib.util
import json
from pathlib import Path


def _load():
    spec = importlib.util.spec_from_file_location(
        "compare_assets_mod", Path(__file__).resolve().parent.parent / "scripts" / "compare_assets.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _row(date, price, jpy, xrp):
    return {"date_jst": date, "price": price, "jpy": jpy, "xrp": xrp,
            "total_jpy": round(jpy + xrp * price)}


def test_baseline_row_has_zero_diff():
    m = _load()
    out = m.compare([_row("2026-10-15", 200.0, 1000.0, 10.0)])
    assert out[0]["diff"] == 0 and out[0]["hold"] == out[0]["actual"]


def test_diff_is_actual_minus_hold_using_baseline_holdings():
    m = _load()
    rows = [_row("2026-10-15", 200.0, 1000.0, 10.0),
            _row("2026-10-16", 190.0, 100.0, 20.0)]  # 実際: 100+20*190=3900 / 持ち続け: 1000+10*190=2900
    out = m.compare(rows)
    assert out[1]["hold"] == 2900 and out[1]["actual"] == 3900 and out[1]["diff"] == 1000


def test_load_skips_error_rows_and_broken_lines(tmp_path):
    m = _load()
    p = tmp_path / "a.jsonl"
    p.write_text(
        json.dumps(_row("2026-10-15", 200.0, 1000.0, 10.0)) + chr(10)
        + json.dumps({"date_jst": "2026-10-16", "error": "x", "total_jpy": None}) + chr(10)
        + "{broken" + chr(10))
    assert [r["date_jst"] for r in m.load(p)] == ["2026-10-15"]
