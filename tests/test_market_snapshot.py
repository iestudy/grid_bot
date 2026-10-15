from src.market_snapshot import (
    HOUR_MS, avg_range, build_snapshot, closed_only, pct_change, range_position, sma_gap_pct,
)


def _candles(closes, start_t=0, spread=1.0):
    return [{"open": c, "high": c + spread / 2, "low": c - spread / 2, "close": c,
             "volume": 1.0, "t": start_t + i * HOUR_MS} for i, c in enumerate(closes)]


def test_closed_only_excludes_in_progress_candle():
    cs = _candles([100, 101, 102])
    now_ms = cs[2]["t"] + HOUR_MS // 2  # 3本目は進行中
    assert len(closed_only(cs, now_ms)) == 2
    assert len(closed_only(cs, cs[2]["t"] + HOUR_MS)) == 3  # ちょうど確定した時刻


def test_pct_change_and_insufficient_data():
    cs = _candles([100, 110])
    assert pct_change(cs, 1) == 10.0
    assert pct_change(cs, 5) is None  # 足りなければNone(0で埋めない)


def test_range_position_low_and_high():
    low = _candles([110, 105, 100], spread=0)
    assert range_position(low, 3)["position"] == 0.0
    high = _candles([100, 105, 110], spread=0)
    assert range_position(high, 3)["position"] == 1.0


def test_range_position_flat_is_none():
    assert range_position(_candles([100, 100, 100], spread=0), 3)["position"] is None


def test_avg_range():
    assert avg_range(_candles([100] * 4, spread=2.0), 4) == 2.0
    assert avg_range(_candles([100] * 2), 4) is None


def test_sma_gap_sign_follows_trend():
    down = _candles(list(range(200, 100, -1)))  # 下落
    up = _candles(list(range(100, 200)))        # 上昇
    assert sma_gap_pct(down, 5, 50) < 0
    assert sma_gap_pct(up, 5, 50) > 0


def test_build_snapshot_returns_none_for_missing_not_zero():
    snap = build_snapshot([], [], 1.0, 10**12)
    assert snap["xrp_price"] is None
    assert snap["xrp_change_pct"]["24h"] is None
    assert snap["avg_range_vs_grid_width"] is None
