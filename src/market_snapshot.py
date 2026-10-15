"""
Claudeの取引モード判断に渡す、市場の事実(指標)を作る。

方針: 計算はコードが行い、Claudeには計算済みの数値だけを渡す。生データは渡さない。
(2026-10の誤判定で、Claudeは渡されていない値を推測して判断した。判断に必要な事実は
 こちらで決定的に計算して渡す。)

データ源: bitbankの公開API(認証不要)。1時間足は1日分ずつ返る。
ohlcvの並び: [始値, 高値, 安値, 終値, 出来高, 時刻ms]。価格は文字列。
"""
import datetime
from typing import List, Optional

import requests

PUBLIC_BASE = "https://public.bitbank.cc"
HOUR_MS = 3600 * 1000


def fetch_hourly(pair: str, days_back: int = 8, now: Optional[datetime.datetime] = None,
                 timeout: float = 10.0) -> List[dict]:
    """1時間足を、古い順に返す。取得できない日は飛ばす(全体は止めない)。"""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    candles: List[dict] = []
    for back in range(days_back - 1, -1, -1):
        day = (now - datetime.timedelta(days=back)).strftime("%Y%m%d")
        try:
            r = requests.get(f"{PUBLIC_BASE}/{pair}/candlestick/1hour/{day}", timeout=timeout).json()
            rows = r["data"]["candlestick"][0]["ohlcv"] if r.get("success") else []
        except Exception:
            rows = []
        for o, h, l, c, v, t in rows:
            candles.append({"open": float(o), "high": float(h), "low": float(l),
                            "close": float(c), "volume": float(v), "t": int(t)})
    candles.sort(key=lambda x: x["t"])
    return candles


def closed_only(candles: List[dict], now_ms: int) -> List[dict]:
    """確定した足だけを返す(開始時刻 + 1時間 <= 現在時刻)。進行中の足は除く。"""
    return [c for c in candles if c["t"] + HOUR_MS <= now_ms]


def pct_change(candles: List[dict], hours: int) -> Optional[float]:
    """直近hours時間の騰落率(%)。足が足りなければNone。"""
    if len(candles) < hours + 1:
        return None
    start = candles[-(hours + 1)]["close"]
    end = candles[-1]["close"]
    return round((end - start) / start * 100, 3) if start else None


def range_position(candles: List[dict], hours: int) -> Optional[dict]:
    """直近hours時間の高値・安値と、現在値がそのレンジ内のどこにあるか(0=安値, 1=高値)。"""
    if len(candles) < hours:
        return None
    window = candles[-hours:]
    hi = max(c["high"] for c in window)
    lo = min(c["low"] for c in window)
    cur = candles[-1]["close"]
    pos = (cur - lo) / (hi - lo) if hi > lo else None
    return {"high": hi, "low": lo, "position": round(pos, 3) if pos is not None else None}


def avg_range(candles: List[dict], hours: int) -> Optional[float]:
    """直近hours時間の、1時間足の平均値幅(高値-安値)。"""
    if len(candles) < hours:
        return None
    window = candles[-hours:]
    return round(sum(c["high"] - c["low"] for c in window) / len(window), 4)


def sma_gap_pct(candles: List[dict], short: int, long: int) -> Optional[float]:
    """短期移動平均が長期移動平均から何%離れているか。正=上向き、負=下向き。"""
    if len(candles) < long:
        return None
    s = sum(c["close"] for c in candles[-short:]) / short
    l = sum(c["close"] for c in candles[-long:]) / long
    return round((s - l) / l * 100, 3) if l else None


def build_snapshot(xrp: List[dict], btc: List[dict], grid_width: float, now_ms: int) -> dict:
    """指標をまとめる。取得できなかった指標はNone。"""
    x = closed_only(xrp, now_ms)
    b = closed_only(btc, now_ms)
    ar = avg_range(x, 24)
    return {
        "xrp_price": x[-1]["close"] if x else None,
        "xrp_change_pct": {
            "1h": pct_change(x, 1), "4h": pct_change(x, 4),
            "24h": pct_change(x, 24), "7d": pct_change(x, 168),
        },
        "xrp_range_24h": range_position(x, 24),
        "xrp_range_7d": range_position(x, 168),
        "xrp_avg_hourly_range_24h": ar,
        "avg_range_vs_grid_width": round(ar / grid_width, 3) if ar and grid_width else None,
        "xrp_sma_gap_pct_24h_vs_168h": sma_gap_pct(x, 24, 168),
        "btc_change_pct_24h": pct_change(b, 24),
        "candles_used": {"xrp": len(x), "btc": len(b)},
    }
