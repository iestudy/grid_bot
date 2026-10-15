"""
Claudeに取引モードを判断させる。現在は試行専用で、モードも注文も変更しない。

事実の収集(コード) -> Claudeの判断 -> 守る線の適用(コード) -> 表示・記録・Slack通知。
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.market_snapshot import fetch_hourly, build_snapshot  # noqa: E402
from src.trading_mode import read_trading_mode  # noqa: E402
from src.trading_apply import (  # noqa: E402
    load_history, append_history, write_mode_file, cancel_buys_and_report,
)
from src.trading_decision import (  # noqa: E402
    DECISION_TOOL, SYSTEM_PROMPT, build_user_prompt, validate_decision, apply_guardrails,
    restore_state, pause_hit_limit,
)

MODEL = "claude-sonnet-4-5-20250929"  # incident_decide.pyで動作実績のあるモデル
JST = timezone(timedelta(hours=9))
DRYRUN_LOG = PROJECT_ROOT / "run" / "decision_dryrun.jsonl"


def load_env(env_path: Path) -> dict:
    env = {}
    if not env_path.exists():
        return env
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        env[key.strip()] = value
    return env


def summarize_orders(orders: list) -> dict:
    buys = [o for o in orders if o["side"] == "buy"]
    sells = [o for o in orders if o["side"] == "sell"]
    return {
        "buy_count": len(buys), "sell_count": len(sells),
        "buy_amount_total_xrp": round(sum(float(o["remaining_amount"]) for o in buys), 4),
        "sell_amount_total_xrp": round(sum(float(o["remaining_amount"]) for o in sells), 4),
        "lowest_buy_price": min((float(o["price"]) for o in buys), default=None),
        "highest_sell_price": max((float(o["price"]) for o in sells), default=None),
    }


def worst_case(price, orders: list) -> dict:
    """買いが全段約定した場合の追加在庫と、そこからさらに下落した場合の含み損の概算(コードで計算)。"""
    buys = [(float(o["price"]), float(o["remaining_amount"])) for o in orders if o["side"] == "buy"]
    if not buys or not price:
        return {"additional_xrp": 0.0, "avg_buy_price": None, "unrealized_loss_if_drop_5pct_jpy": 0.0}
    xrp = sum(a for _, a in buys)
    avg = sum(p * a for p, a in buys) / xrp
    lowest = min(p for p, _ in buys)
    drop_price = lowest * 0.95
    return {
        "additional_xrp": round(xrp, 2),
        "avg_buy_price": round(avg, 3),
        "lowest_buy_price": lowest,
        "unrealized_loss_if_drop_5pct_below_lowest_buy_jpy": round((avg - drop_price) * xrp),
    }


def gather_bot_state() -> dict:
    r = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "incident_check.py")],
        capture_output=True, text=True, timeout=120, cwd=str(PROJECT_ROOT),
    )
    return json.loads(r.stdout)


def build_facts(data: dict, snapshot: dict, current_mode: str, now: datetime) -> dict:
    b = data.get("balances") or {}
    jpy, xrp = b.get("jpy") or {}, b.get("xrp") or {}
    price = data.get("current_price")
    state = data.get("bot_state") or {}
    total = None
    if price and jpy and xrp:
        total = round(jpy["onhand_amount"] + xrp["onhand_amount"] * price)
    base = state.get("base_price")
    return {
        "now_jst": now.astimezone(JST).strftime("%Y-%m-%d %H:%M JST"),
        "current_trading_mode": current_mode,
        "my_state": {
            "price": price,
            "base_price": base,
            "deviation_from_base_jpy": round(price - base, 3) if price and base else None,
            "net_inventory_since_reset_xrp": state.get("net_inventory"),
            "cash_flow_since_reset_jpy": state.get("cash_flow"),
            "jpy_total": jpy.get("onhand_amount"), "jpy_free": jpy.get("free_amount"),
            "xrp_total": xrp.get("onhand_amount"), "xrp_free": xrp.get("free_amount"),
            "total_assets_jpy": total,
            "orders": summarize_orders((data.get("active_orders") or {}).get("orders") or []),
            "worst_case_if_all_buys_fill": worst_case(price, (data.get("active_orders") or {}).get("orders") or []),
            "config": data.get("config"),
        },
        "market": snapshot,
        "notes": "総資産の履歴と過去の判断の履歴は、まだ記録されていません(不明として扱ってください)。",
    }


def ask_claude(api_key: str, facts: dict) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=api_key, timeout=60.0)
    msg = client.messages.create(
        model=MODEL, max_tokens=1500, system=SYSTEM_PROMPT, tools=[DECISION_TOOL],
        tool_choice={"type": "tool", "name": DECISION_TOOL["name"]},
        messages=[{"role": "user", "content": build_user_prompt(facts)}],
    )
    for block in msg.content:
        if block.type == "tool_use" and block.name == DECISION_TOOL["name"]:
            return dict(block.input)
    return {}


def fmt(v, suffix=""):
    return "不明" if v is None else f"{v}{suffix}"


def format_slack(decision: dict, final: dict, facts: dict, applied: bool = False) -> str:
    m = facts["market"]
    ch = m.get("xrp_change_pct") or {}
    r7 = m.get("xrp_range_7d") or {}
    lines = [
        f"取引モード判断({'適用' if applied else '試行・未適用'}): Claudeの提案={decision['mode']} / 守る線の適用後={final['mode']}",
        facts["now_jst"],
        f"理由: {decision['reason']}",
        f"市場の見立て: {decision['market_view']}",
    ]
    if decision["resume_condition"]:
        lines.append(f"再開の目安: {decision['resume_condition']}")
    lines.append(f"確信度: {decision['confidence']} / 次の見直し: {decision['next_review_minutes']}分後")
    if final["overridden"]:
        lines.append(f"守る線による補正: {final['note']}")
    lines.append(
        "参照した指標: 価格 " + fmt(m.get("xrp_price"), "円")
        + " / 24h " + fmt(ch.get("24h"), "%") + " / 7d " + fmt(ch.get("7d"), "%")
        + " / 7日レンジ内の位置 " + fmt(r7.get("position"))
        + " / BTC24h " + fmt(m.get("btc_change_pct_24h"), "%")
    )
    if not applied:
        lines.append("※試行のため、取引モードは変更していません。")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-slack", action="store_true", help="Slackに送らない")
    parser.add_argument("--apply", action="store_true", help="モードの書き込みと買い注文の取り消しを行う(既定は試行のみ)")
    args = parser.parse_args()

    status = subprocess.run(["systemctl", "is-active", "grid_bot"], capture_output=True, text=True)
    if status.stdout.strip() != "active":
        print(f"grid_botが稼働中ではありません({status.stdout.strip()})。判断しません。")
        return

    env = load_env(PROJECT_ROOT / ".env")
    api_key = env.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEYが.envにありません。", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(timezone.utc)
    now_ms = int(time.time() * 1000)
    data = gather_bot_state()
    grid_width = (data.get("config") or {}).get("grid_width_default_jpy")
    snapshot = build_snapshot(fetch_hourly("xrp_jpy", 8), fetch_hourly("btc_jpy", 8), grid_width, now_ms)
    current_mode = read_trading_mode()
    facts = build_facts(data, snapshot, current_mode, now)

    try:
        raw = ask_claude(api_key, facts)
    except Exception as e:
        print(f"Claude APIの呼び出しに失敗しました(現状維持): {e}", file=sys.stderr)
        sys.exit(1)

    decision = validate_decision(raw)
    if decision is None:
        print(f"Claudeの出力が不正でした(現状維持): {raw}", file=sys.stderr)
        sys.exit(1)

    history = load_history()
    state = restore_state(history, now)
    current_mode = read_trading_mode()  # 履歴ではなく、実際のモードファイルを優先する
    hit_limit = state['current_mode'] == 'PAUSED' and pause_hit_limit(state['pause_started_at'], now)
    final = apply_guardrails(
        decision, current_mode, state['last_change_at'], state['pause_started_at'],
        state['last_pause_ended_at'], now,
    )
    pause_end_reason = None
    if hit_limit:
        # PAUSEDが24時間の上限に達した。判断にかかわらずACTIVEへ戻す。
        final = {'mode': 'ACTIVE', 'overridden': True,
                 'note': 'PAUSEDが上限(24時間)に達したため、ACTIVEへ戻します。'}
        pause_end_reason = 'limit'

    text = format_slack(decision, final, facts, applied=args.apply)
    print(text)

    record = {'at': now.isoformat(), 'mode': final['mode'], 'claude_mode': decision['mode'],
              'price': facts['my_state']['price'], 'decision': decision, 'final': final, 'facts': facts}
    if pause_end_reason:
        record['pause_end_reason'] = pause_end_reason

    if not args.apply:
        # 試行。モードも注文も変更しない。試行の記録は、状態の復元には使わない。
        DRYRUN_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(DRYRUN_LOG, 'a') as f:
            f.write(json.dumps(record, ensure_ascii=False) + chr(10))
        if not args.no_slack:
            from src.notifications import SlackNotifier
            SlackNotifier(webhook_url=env.get('SLACK_WEBHOOK_URL'))._send(text)
        return

    # --- 適用 ---
    append_history(record)
    # モードファイルを先に書く(買い注文の取り消しが失敗しても、新規の買いは止まる)
    write_mode_file(final['mode'], decision['reason'], state, now)
    cancel_text = ''
    if final['mode'] == 'PAUSED' and current_mode != 'PAUSED':
        cancel_text = cancel_buys_and_report(env)
    if not args.no_slack:
        from src.notifications import SlackNotifier
        SlackNotifier(webhook_url=env.get('SLACK_WEBHOOK_URL'))._send(text + cancel_text)


if __name__ == "__main__":
    main()
