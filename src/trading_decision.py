"""
Claudeによる取引モード判断の、純粋な部分(APIは呼ばない)。

Claudeは判断だけを返す。適用は決定的なコード(decide_trading_mode.py)が行う。
Claudeの出力は、ここで検証し、守る線に反するものは「現状維持」に倒す。
  - 不正な出力、理由が空、不明なモード -> 現状維持
  - 確信度lowのPAUSED -> 採用しない(ACTIVE)
  - 前回のモード変更から MIN_CHANGE_INTERVAL 未満 -> 変更を受け付けない
  - PAUSEDが MAX_PAUSE_HOURS 続いたら、次の FORCED_ACTIVE_HOURS は ACTIVE を強制する
  - PAUSEDの期限は、最大 MAX_PAUSE_HOURS
"""
import json
from datetime import datetime, timedelta
from typing import List, Optional

VALID_MODES = ("ACTIVE", "CAUTIOUS", "PAUSED")
MIN_CHANGE_INTERVAL = timedelta(hours=2)
MAX_PAUSE_HOURS = 24
FORCED_ACTIVE_HOURS = 6
MIN_REVIEW_MIN, MAX_REVIEW_MIN = 60, 240

DECISION_TOOL = {
    "name": "report_trading_mode",
    "description": "取引モードの判断を報告する",
    "input_schema": {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": list(VALID_MODES),
                     "description": "ACTIVE=通常取引 / CAUTIOUS=慎重(現時点ではACTIVEと同じ動き) / PAUSED=買いを止め、売りは維持"},
            "reason": {"type": "string", "description": "判断の理由。指標の数値を引用して、日本語で2〜4文"},
            "market_view": {"type": "string", "description": "市場の見立て。1〜2文"},
            "resume_condition": {"type": "string", "description": "PAUSEDの場合、再開の目安。それ以外は空でよい"},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "next_review_minutes": {"type": "integer", "description": f"次に見直すまでの分数({MIN_REVIEW_MIN}〜{MAX_REVIEW_MIN})"},
        },
        "required": ["mode", "reason", "market_view", "confidence", "next_review_minutes"],
    },
}

SYSTEM_PROMPT = """あなたはXRP/JPYグリッド取引botの「取引モード」を判断します。
注文の量や価格は決めません。決めるのは、今は取引を続けるか、買いを止めるかだけです。

【戦略の性質】
- グリッドは、レンジ相場で往復の利幅を取り、一方向のトレンドでは在庫が偏って損をします。
- 下落が続くと買いが次々に約定して在庫が増え、含み損が膨らみます。上昇が続くと売りが先に約定し、在庫が減ります。

【モード】
- ACTIVE: 通常の取引。
- CAUTIOUS: 慎重。現時点ではACTIVEと同じ動きです。
- PAUSED: 買い注文を取り消し、新規の買いを出しません。売りは維持するので、保有XRPは反発時に売れます。
  PAUSEDの間、下落を拾えず、反発後の買い戻しも出ません。止めることにも費用があります。

【指標の読み方】
- position: 0が期間内の安値圏、1が高値圏。
- avg_range_vs_grid_width: 1時間の平均値幅がグリッド幅の何倍か。1を大きく超えるほど、価格が段を一気に飛ばす動きで、
  グリッドにとっては不利です(約定が片側に偏り、在庫が積み上がりやすい)。「値動きが活発=有利」とは読まないでください。
- positionが0に近く、騰落率が下向きなら、買いが連続して約定しやすい位置です。買い注文の最低値に価格が近いほど、
  買いの全段が約定する可能性が高まります。
- worst_case_if_all_buys_fill: 買いが全段約定した場合の追加在庫と、その後さらに下落した場合の含み損の概算です。
  理由には、この数値を引用して、下落が続いた場合の損失の大きさを必ず述べてください。
- sma_gap: 負なら短期が長期を下回る(下向き)。
- null は不明です。推測で補わないでください。
- BTCも同じ向きに動いていれば、XRP固有でなく市場全体の動きです。

【制約】
- PAUSEDは最長24時間で、自動的にACTIVEへ戻ります。判断は約2時間おきに見直されます。
- 判断に迷うときや、材料が不足するときは、ACTIVEを選んでください。止めすぎると取引の機会を失います。
- 過去の判断と、その後の価格の動きが渡されます。自分の過去の判断が外れていないかを、振り返ってください。
- 理由には、渡された数値を引用してください。渡されていない情報(ニュースなど)を、根拠にしないでください。
"""


def build_user_prompt(facts: dict) -> str:
    return (
        "以下が現在の事実です(コードが計算した値です)。\n\n```json\n"
        + json.dumps(facts, indent=2, ensure_ascii=False)
        + "\n```\n\nreport_trading_modeツールで判断を報告してください。"
    )


def validate_decision(raw: dict) -> Optional[dict]:
    """Claudeの出力を検証し、正規化して返す。不正ならNone(現状維持)。"""
    if not isinstance(raw, dict):
        return None
    mode = raw.get("mode")
    reason = (raw.get("reason") or "").strip()
    if mode not in VALID_MODES or not reason:
        return None
    confidence = raw.get("confidence")
    if confidence not in ("low", "medium", "high"):
        confidence = "low"
    try:
        review = int(raw.get("next_review_minutes", 120))
    except (TypeError, ValueError):
        review = 120
    review = max(MIN_REVIEW_MIN, min(MAX_REVIEW_MIN, review))
    return {
        "mode": mode, "reason": reason,
        "market_view": (raw.get("market_view") or "").strip(),
        "resume_condition": (raw.get("resume_condition") or "").strip(),
        "confidence": confidence, "next_review_minutes": review,
    }


def apply_guardrails(decision: dict, current_mode: str, last_change_at: Optional[datetime],
                     pause_started_at: Optional[datetime], last_pause_ended_at: Optional[datetime],
                     now: datetime) -> dict:
    """守る線を適用し、最終的なモードと、その理由(補正の有無)を返す。

    戻り値: {"mode": 最終モード, "overridden": bool, "note": str}
    """
    wanted = decision["mode"]
    note = ""

    # 確信度lowのPAUSEDは採用しない
    if wanted == "PAUSED" and decision["confidence"] == "low":
        return {"mode": "ACTIVE" if current_mode != "PAUSED" else current_mode, "overridden": True,
                "note": "確信度lowのPAUSEDは採用しません。"}

    # PAUSEDが長く続いたら、一定時間ACTIVEを強制する(取引の機会を確保する)
    if wanted == "PAUSED" and last_pause_ended_at is not None and \
            now - last_pause_ended_at < timedelta(hours=FORCED_ACTIVE_HOURS):
        return {"mode": "ACTIVE", "overridden": True,
                "note": f"PAUSEDが上限に達した後の{FORCED_ACTIVE_HOURS}時間は、ACTIVEを維持します。"}

    # 頻繁な切り替えを防ぐ(PAUSEDの解除は、買いが止まり続けるのを避けるため常に許可する)
    if wanted != current_mode and last_change_at is not None and \
            now - last_change_at < MIN_CHANGE_INTERVAL and not (current_mode == "PAUSED" and wanted == "ACTIVE"):
        return {"mode": current_mode, "overridden": True,
                "note": "前回のモード変更から2時間以内のため、変更を受け付けません。"}

    return {"mode": wanted, "overridden": False, "note": note}


def pause_expiry(now: datetime, pause_started_at: Optional[datetime]) -> datetime:
    """PAUSEDの期限。開始から最長MAX_PAUSE_HOURS。継続中は、開始時刻を基準にする。"""
    start = pause_started_at or now
    return start + timedelta(hours=MAX_PAUSE_HOURS)


def restore_state(history: List[dict], now: datetime) -> dict:
    """判断履歴(古い順)から、守る線の判定に必要な状態を復元する。

    履歴の各行: {"at": ISO8601, "mode": 適用後のモード, "pause_end_reason": "limit"|None, ...}
      - last_change_at: 直近でモードが変わった時刻
      - pause_started_at: 現在PAUSEDが続いているなら、その開始時刻
      - last_pause_ended_at: PAUSEDが「24時間の上限に達して」終わった時刻のみ。
        手動の解除や通常の再開では設定しない(6時間のACTIVE強制は、上限到達のときだけ)。
    """
    last_change_at = None
    pause_started_at = None
    last_pause_ended_at = None
    prev_mode = None
    for rec in history:
        at = datetime.fromisoformat(rec["at"])
        mode = rec["mode"]
        if prev_mode is not None and mode != prev_mode:
            last_change_at = at
        if mode == "PAUSED" and prev_mode != "PAUSED":
            pause_started_at = at
        if prev_mode == "PAUSED" and mode != "PAUSED":
            if rec.get("pause_end_reason") == "limit":
                last_pause_ended_at = at
            pause_started_at = None
        prev_mode = mode
    return {
        "current_mode": prev_mode or "ACTIVE",
        "last_change_at": last_change_at,
        "pause_started_at": pause_started_at,
        "last_pause_ended_at": last_pause_ended_at,
    }


def pause_hit_limit(pause_started_at: Optional[datetime], now: datetime) -> bool:
    """PAUSEDが、24時間の上限に達しているか。"""
    return pause_started_at is not None and now - pause_started_at >= timedelta(hours=MAX_PAUSE_HOURS)
