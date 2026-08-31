"""
実際の口座残高(bitbank)を確認するスクリプト。
.envから認証情報を読み込み、JPY/XRPの残高を表示する。

使い方:
    cd /home/ec2-user/grid_bot
    venv/bin/python3 scripts/check_balance.py
"""
import os
import json
import sys
from pathlib import Path


def load_env(env_path: Path) -> dict:
    env = {}
    if not env_path.exists():
        print(f"エラー: {env_path} が見つかりません", file=sys.stderr)
        sys.exit(1)
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def main():
    project_root = Path(__file__).resolve().parent.parent
    env_vars = load_env(project_root / ".env")

    sys.path.insert(0, str(project_root))
    from src.bitbank_client import BitbankClient

    api_key = env_vars.get("BITBANK_API_KEY")
    api_secret = env_vars.get("BITBANK_API_SECRET")

    if not api_key or not api_secret:
        print("エラー: .envにBITBANK_API_KEY/BITBANK_API_SECRETがありません", file=sys.stderr)
        sys.exit(1)

    client = BitbankClient(api_key=api_key, api_secret=api_secret)
    assets = client.get_assets()

    # まず生の構造をデバッグ表示
    print("--- raw response ---", file=sys.stderr)
    print(json.dumps(assets, indent=2, ensure_ascii=False)[:3000], file=sys.stderr)
    print("--- end raw ---", file=sys.stderr)

    # data配下かどうか両対応
    asset_list = assets.get("data", {}).get("assets") if "data" in assets else assets.get("assets")

    if asset_list is None:
        print("assetsのキー構造が想定外です。上のraw responseを確認してください。", file=sys.stderr)
        sys.exit(1)

    for a in asset_list:
        if a["asset"] in ("jpy", "xrp"):
            print(json.dumps(a, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
