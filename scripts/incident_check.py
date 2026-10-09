"""
インシデント対応(EMERGENCY_STOP後の自動復旧)のための状況収集スクリプト。

実残高・アクティブ注文・bot内部の帳簿(base_price/PortfolioState)を
まとめて取得し、JSON形式で標準出力に書き出す。Claude Codeがこの出力を
読み取り、reset_state.py/resize_grid.py/bot再起動の要否を判断する
入力として使うことを想定している。

使い方:
    venv/bin/python3 scripts/incident_check.py

このスクリプト自体は状態を変更しない(読み取り専用)。
"""
import json
import sys
from pathlib import Path


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
        # .envの値がクォートで囲まれている場合(例: KEY="value")は剥がす
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        env[key.strip()] = value
    return env


def main():
    project_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(project_root))

    env_vars = load_env(project_root / ".env")
    result = {"errors": []}

    # --- 実残高 ---
    try:
        from src.bitbank_client import BitbankClient

        client = BitbankClient(
            api_key=env_vars.get("BITBANK_API_KEY"),
            api_secret=env_vars.get("BITBANK_API_SECRET"),
        )
        assets = client.get_assets()["assets"]
        balances = {}
        for a in assets:
            if a["asset"] in ("jpy", "xrp"):
                balances[a["asset"]] = {
                    "onhand_amount": float(a["onhand_amount"]),
                    "locked_amount": float(a["locked_amount"]),
                    "free_amount": float(a["free_amount"]),
                }
        result["balances"] = balances
    except Exception as e:
        result["errors"].append(f"balances取得失敗: {e}")
        result["balances"] = None

    # --- アクティブ注文 ---
    try:
        orders = client.get_active_orders("xrp_jpy")["orders"]
        result["active_orders"] = {
            "count": len(orders),
            "orders": [
                {
                    "side": o["side"],
                    "price": o["price"],
                    "start_amount": o.get("start_amount"),
                    "remaining_amount": o.get("remaining_amount"),
                    "executed_amount": o.get("executed_amount"),
                    "status": o["status"],
                }
                for o in orders
            ],
        }
    except Exception as e:
        result["errors"].append(f"active_orders取得失敗: {e}")
        result["active_orders"] = None

    # --- bot内部の帳簿 ---
    try:
        from src.state_store import DynamoDBStateStore

        store = DynamoDBStateStore()
        base_price = store.get_base_price()
        portfolio = store.get_portfolio_state()
        result["bot_state"] = {
            "base_price": base_price,
            "cash_flow": portfolio.cash_flow,
            "net_inventory": portfolio.net_inventory,
        }
    except Exception as e:
        result["errors"].append(f"bot_state取得失敗: {e}")
        result["bot_state"] = None

    # --- 簡易整合性チェック(判断材料として付与するだけ。ここでは何もアクションしない) ---
    consistency = {}
    if result.get("balances") and result.get("bot_state"):
        xrp_balance = result["balances"].get("xrp", {})
        xrp_free = xrp_balance.get("free_amount")
        xrp_onhand = xrp_balance.get("onhand_amount")
        net_inventory = result["bot_state"].get("net_inventory")
        if xrp_free is not None and net_inventory is not None:
            consistency["xrp_free_vs_net_inventory_diff"] = round(xrp_free - net_inventory, 6)
        # net_inventoryが負の値の場合、実際にXRPをどれだけ保有しているか
        # (onhand_amount、拘束中の分も含む)が「正当な売り越し」か
        # 「帳簿バグ」かの判定材料になる。ランブックのエスカレーション
        # 条件2を参照。
        if xrp_onhand is not None:
            consistency["xrp_onhand_amount"] = xrp_onhand
    if result.get("active_orders") is not None:
        consistency["active_order_count"] = result["active_orders"]["count"]
    result["consistency"] = consistency

    # --- config の現在の amount(判断側が推測せずに済むようにする) ---
    try:
        from src.config import GRID_ENVELOPE
        result["config"] = {
            "buy_amount_per_level_xrp": GRID_ENVELOPE.buy_amount_per_level_xrp,
            "sell_amount_per_level_xrp": GRID_ENVELOPE.sell_amount_per_level_xrp,
            "grid_width_default_jpy": GRID_ENVELOPE.grid_width_default_jpy,
            "new_order_halt_deviation_jpy": GRID_ENVELOPE.new_order_halt_deviation_jpy,
        }
    except Exception as e:
        result["errors"].append(f"config取得失敗: {e}")
        result["config"] = None

    # --- 在庫の整合性判定(net_inventory/cash_flowと実残高を、コードで決定的に判定) ---
    # 判定が「consistent」なら、incident_decide.pyはClaudeのエスカレーション条件2の
    # 判断を採用しない。「inconsistent」「unknown」の場合は、従来通りClaudeに渡す。
    try:
        import json as _json
        from pathlib import Path as _Path
        from src.inventory_consistency import check_inventory_consistency

        current_price = None
        try:
            current_price = float(client.get_ticker("xrp_jpy")["data"]["last"])
        except Exception as e:
            result["errors"].append(f"現在価格の取得失敗(整合性判定に影響): {e}")

        baseline_xrp = None
        baseline_path = _Path(__file__).resolve().parent.parent / "run" / "reset_baseline.json"
        if baseline_path.exists():
            try:
                _raw = _json.loads(baseline_path.read_text()).get("xrp_onhand")
                # xrp_onhandが無い、または数値でない場合は、基準値なしとして扱う
                # (エラーにしない。errorsが空でないと、自動対応が条件1でエスカレーションするため)。
                baseline_xrp = float(_raw) if isinstance(_raw, (int, float)) else None
            except Exception as e:
                result["errors"].append(f"reset_baseline.jsonの読み込み失敗: {e}")

        if result.get("balances") and result.get("bot_state"):
            result["current_price"] = current_price
            result["inventory_consistency"] = check_inventory_consistency(
                net_inventory=result["bot_state"]["net_inventory"],
                cash_flow=result["bot_state"]["cash_flow"],
                xrp_onhand=result["balances"]["xrp"]["onhand_amount"],
                current_price=current_price,
                baseline_xrp=baseline_xrp,
            )
        else:
            result["inventory_consistency"] = {
                "status": "unknown",
                "reasons": ["残高または帳簿を取得できなかったため判定できない"],
            }
    except Exception as e:
        result["errors"].append(f"在庫整合性判定の失敗: {e}")
        result["inventory_consistency"] = {"status": "unknown", "reasons": [f"判定中に例外: {e}"]}

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
