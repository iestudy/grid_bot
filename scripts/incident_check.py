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
        xrp_free = result["balances"].get("xrp", {}).get("free_amount")
        net_inventory = result["bot_state"].get("net_inventory")
        if xrp_free is not None and net_inventory is not None:
            consistency["xrp_free_vs_net_inventory_diff"] = round(xrp_free - net_inventory, 6)
    if result.get("active_orders") is not None:
        consistency["active_order_count"] = result["active_orders"]["count"]
    result["consistency"] = consistency

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
