"""
取引モード(ACTIVE / CAUTIOUS / PAUSED)の読み取り。

モードは run/trading_mode.json に、判断スクリプトが書く。bot本体は、各iterationで
これを読むだけで、書かない。失敗時は、通常運転(ACTIVE)に倒す。取引が止まったまま
になることを避けるため。

  {"mode": "PAUSED", "set_at": "...ISO8601", "expires_at": "...ISO8601", "reason": "..."}

- expires_atを過ぎたら、ACTIVEとして扱う(止め続けない)。
- 不明なモード名、壊れたJSON、ファイルなしは、ACTIVEとして扱う。
- CAUTIOUSは、現時点ではACTIVEと同じ動き(動作は後から足す)。
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

TRADING_MODE_PATH = Path(__file__).resolve().parent.parent / "run" / "trading_mode.json"
VALID_MODES = ("ACTIVE", "CAUTIOUS", "PAUSED")


def read_trading_mode(path: Optional[Path] = None, now: Optional[datetime] = None) -> str:
    """現在有効なモード名を返す。読めない・期限切れ・不明な値は"ACTIVE"。"""
    path = path or TRADING_MODE_PATH
    now = now or datetime.now(timezone.utc)
    try:
        if not path.exists():
            return "ACTIVE"
        data = json.loads(path.read_text())
        mode = data.get("mode")
        if mode not in VALID_MODES:
            logger.warning(f"不明な取引モード{mode!r}のため、ACTIVEとして動作します")
            return "ACTIVE"
        expires_at = data.get("expires_at")
        if expires_at:
            if now >= datetime.fromisoformat(expires_at):
                return "ACTIVE"
        elif mode != "ACTIVE":
            # 期限のないACTIVE以外のモードは、止め続けるおそれがあるため無効にする。
            logger.warning("expires_atのない取引モードは無効です。ACTIVEとして動作します")
            return "ACTIVE"
        return mode
    except Exception as e:
        logger.warning(f"取引モードの読み取りに失敗したため、ACTIVEとして動作します: {e}")
        return "ACTIVE"


def exclude_buys_if_paused(levels: list, mode: str) -> list:
    """PAUSEDのとき、買いのレベルを除く。それ以外は、そのまま返す。"""
    if mode != "PAUSED":
        return levels
    return [l for l in levels if l.side != "buy"]
