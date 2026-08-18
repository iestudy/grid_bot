"""
最小ロットでの実発注テスト。

安全設計:
- 数量は必ず明示指定させ、SAFETY_MAX_AMOUNT_XRPを超える値は拒否する
- post_only=True を必ず使用し、成行化を防ぐ
- 発注直前に最新の板情報を取得し、確実にメイカー側になる価格を計算する
  （買い: 最良買い気配より1tick下 / 売り: 最良売り気配より1tick上）
- 発注前に必ず人間の確認(y/N)を挟む。--auto-cancel以外では自動実行しない
- 発注後は get_active_orders で状態を確認できる

使い方（買いテスト、1XRP、確認プロンプトあり）:
    python3 -m src.live_order_test --side buy --amount 1.0

使い方（売りテスト、1XRP、発注後に自動キャンセルまでラウンドトリップ確認）:
    python3 -m src.live_order_test --side sell --amount 1.0 --auto-cancel
"""

import argparse
import os
import sys

from .bitbank_client import BitbankClient

TICK_SIZE_JPY = 0.001  # XRP/JPYの呼び値。本番投入前にbitbank公式ドキュメントで最新仕様を再確認すること
SAFETY_MAX_AMOUNT_XRP = 5.0  # これを超える数量はテストとして不適切なため拒否する


def compute_safe_price(side: str, best_buy: float, best_sell: float, tick_size: float = TICK_SIZE_JPY) -> float:
    """
    post_onlyが確実にメイカーとして通る価格を計算する。
    買いは最良買い気配より1tick下、売りは最良売り気配より1tick上に置くことで、
    相場急変時でも成行扱いにならず、拒否・即時キャンセルを避ける。
    """
    if side == "buy":
        return round(best_buy - tick_size, 3)
    elif side == "sell":
        return round(best_sell + tick_size, 3)
    else:
        raise ValueError(f"unknown side: {side}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", default="xrp_jpy")
    parser.add_argument("--side", choices=["buy", "sell"], required=True)
    parser.add_argument("--amount", type=float, required=True,
                         help=f"テスト数量(XRP)。安全のため{SAFETY_MAX_AMOUNT_XRP}XRP以下のみ許可")
    parser.add_argument("--auto-cancel", action="store_true",
                         help="発注後、確認を挟まず即座にキャンセルする(ラウンドトリップ確認に推奨)")
    args = parser.parse_args()

    if args.amount <= 0:
        print("エラー: 数量は正の値にしてください。")
        sys.exit(1)
    if args.amount > SAFETY_MAX_AMOUNT_XRP:
        print(f"エラー: 数量が安全上限({SAFETY_MAX_AMOUNT_XRP}XRP)を超えています。テストなので少量にしてください。")
        sys.exit(1)

    from dotenv import load_dotenv
    load_dotenv()
    client = BitbankClient(
        api_key=os.getenv("BITBANK_API_KEY"),
        api_secret=os.getenv("BITBANK_API_SECRET"),
    )

    ticker = client.get_ticker(args.pair)["data"]
    best_buy = float(ticker["buy"])
    best_sell = float(ticker["sell"])
    print(f"現在の板: buy={best_buy} / sell={best_sell}")

    price = compute_safe_price(args.side, best_buy, best_sell)
    notional = price * args.amount
    print(f"発注予定: side={args.side} price={price} amount={args.amount} (想定金額 約{notional:.1f}円)")

    confirm = input("この内容で発注しますか？ 実際に資金が動きます [y/N]: ")
    if confirm.strip().lower() != "y":
        print("中止しました。発注は行われていません。")
        return

    result = client.create_order(
        pair=args.pair, price=price, amount=args.amount, side=args.side,
        order_type="limit", post_only=True,
    )
    print("発注結果:", result)
    order_id = result.get("order_id")

    active = client.get_active_orders(args.pair)
    print("現在の未約定注文一覧:", active)

    if args.auto_cancel:
        cancel_result = client.cancel_order(args.pair, order_id)
        print("キャンセル結果:", cancel_result)
        print("ラウンドトリップテスト完了（発注→確認→キャンセルまで正常に動作）。")
    else:
        print(f"注文ID {order_id} は未約定のまま残しています。")
        print("手動でキャンセルする場合、以下を実行してください:")
        print(
            f'  python3 -c "from dotenv import load_dotenv; import os; load_dotenv(); '
            f'from src.bitbank_client import BitbankClient; '
            f'c = BitbankClient(api_key=os.getenv(\'BITBANK_API_KEY\'), api_secret=os.getenv(\'BITBANK_API_SECRET\')); '
            f'print(c.cancel_order(\'{args.pair}\', {order_id}))"'
        )


if __name__ == "__main__":
    main()
