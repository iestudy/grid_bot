"""
実際の口座残高(自由JPY・自由XRP)に基づいて buy_amount_per_level_xrp /
sell_amount_per_level_xrp を再計算し、config.py を更新する。

買い側・売り側を別々に計算する理由:
  2026-09まではamount_per_level_xrpという単一の値を買い・売り共通で
  使っていたが、JPYとXRPの残高が偏った際(例: 買いが連続約定して
  XRPが貯まりJPYが枯渇)、枯渇した側に合わせてamountを下げざるを得ず、
  潤沢な側まで小さい注文しか出せなくなる問題が繰り返し発生していた。
  買い側はJPY残高のみ、売り側はXRP残高のみを基準に、それぞれ独立に
  計算することでこの問題を解消する。

安全マージンの考え方:
  自由JPY残高・自由XRP残高それぞれの一部(デフォルト70%)だけを
  グリッドの予算として使い、残りは価格変動・手数料等のバッファとして
  温存する。100%を使い切る設計にすると、わずかな価格変動で
  再び枯渇するリスクがある。

使い方:
    python3 -m src.resize_grid --pair xrp_jpy --apply
  --applyを付けない場合は計算結果を表示するのみで、config.pyは変更しない。
"""

import argparse
import logging
import os
import re
import dataclasses

from dotenv import load_dotenv

from .bitbank_client import BitbankClient
from .config import GRID_ENVELOPE
from .grid_engine import required_buy_side_jpy, required_sell_side_xrp

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.py")
BUDGET_UTILIZATION_RATIO = 0.70  # 自由JPY・自由XRP残高の何割をグリッド予算として使うか


def compute_recommended_buy_amount_per_level(
    free_jpy: float,
    base_price: float,
    cfg=GRID_ENVELOPE,
    budget_ratio: float = BUDGET_UTILIZATION_RATIO,
) -> float:
    """
    required_buy_side_jpy(amount=1XRP換算での必要額)を基準に、
    budget_ratio * free_jpy に収まる buy_amount_per_level_xrp を逆算する。
    """
    unit_cfg = dataclasses.replace(cfg, buy_amount_per_level_xrp=1.0)
    cost_per_unit = required_buy_side_jpy(unit_cfg, base_price)
    if cost_per_unit <= 0:
        raise ValueError("cost_per_unitが0以下です。base_price/grid_widthの設定を確認してください。")

    budget = free_jpy * budget_ratio
    recommended = budget / cost_per_unit
    return round(recommended, 1)


def compute_recommended_sell_amount_per_level(
    free_xrp: float,
    cfg=GRID_ENVELOPE,
    budget_ratio: float = BUDGET_UTILIZATION_RATIO,
) -> float:
    """
    required_sell_side_xrp(amount=1XRP換算での必要量)を基準に、
    budget_ratio * free_xrp に収まる sell_amount_per_level_xrp を逆算する。
    """
    unit_cfg = dataclasses.replace(cfg, sell_amount_per_level_xrp=1.0)
    required_per_unit = required_sell_side_xrp(unit_cfg)
    if required_per_unit <= 0:
        raise ValueError("required_per_unitが0以下です。max_sell_levelsの設定を確認してください。")

    budget = free_xrp * budget_ratio
    recommended = budget / required_per_unit
    return round(recommended, 1)


def update_config_file(new_buy_amount: float, new_sell_amount: float) -> None:
    with open(CONFIG_PATH) as f:
        content = f.read()

    buy_pattern = r"(buy_amount_per_level_xrp:\s*float\s*=\s*)[\d.]+"
    content, buy_count = re.subn(buy_pattern, rf"\g<1>{new_buy_amount}", content)
    if buy_count != 1:
        raise RuntimeError(f"buy_amount_per_level_xrpの置換に失敗しました(マッチ数={buy_count})。")

    sell_pattern = r"(sell_amount_per_level_xrp:\s*float\s*=\s*)[\d.]+"
    content, sell_count = re.subn(sell_pattern, rf"\g<1>{new_sell_amount}", content)
    if sell_count != 1:
        raise RuntimeError(f"sell_amount_per_level_xrpの置換に失敗しました(マッチ数={sell_count})。")

    with open(CONFIG_PATH, "w") as f:
        f.write(content)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", default="xrp_jpy")
    parser.add_argument("--apply", action="store_true", help="指定しない限り計算結果の表示のみ")
    parser.add_argument("--budget-ratio", type=float, default=BUDGET_UTILIZATION_RATIO)
    args = parser.parse_args()

    load_dotenv()
    client = BitbankClient(
        api_key=os.getenv("BITBANK_API_KEY"),
        api_secret=os.getenv("BITBANK_API_SECRET"),
    )

    ticker = client.get_ticker(args.pair)["data"]
    current_price = float(ticker["last"])

    assets = client.get_assets()["assets"]
    free_jpy = next(float(a["free_amount"]) for a in assets if a["asset"] == "jpy")
    free_xrp = next(float(a["free_amount"]) for a in assets if a["asset"] == "xrp")

    current_buy_amount = GRID_ENVELOPE.buy_amount_per_level_xrp
    current_sell_amount = GRID_ENVELOPE.sell_amount_per_level_xrp

    recommended_buy = compute_recommended_buy_amount_per_level(free_jpy, current_price, budget_ratio=args.budget_ratio)
    recommended_sell = compute_recommended_sell_amount_per_level(free_xrp, budget_ratio=args.budget_ratio)

    print(f"現在価格: {current_price}円")
    print(f"自由JPY残高: {free_jpy}円 / 自由XRP残高: {free_xrp}枚")
    print(f"現在のbuy_amount_per_level_xrp: {current_buy_amount} / sell_amount_per_level_xrp: {current_sell_amount}")
    print(f"推奨buy_amount_per_level_xrp: {recommended_buy} (JPY予算の{args.budget_ratio*100:.0f}%を買いグリッドに割り当てた場合)")
    print(f"推奨sell_amount_per_level_xrp: {recommended_sell} (XRP予算の{args.budget_ratio*100:.0f}%を売りグリッドに割り当てた場合)")

    required_jpy_at_recommended = required_buy_side_jpy(
        dataclasses.replace(GRID_ENVELOPE, buy_amount_per_level_xrp=recommended_buy), current_price,
    )
    print(f"→ 買いグリッド必要額: 約{required_jpy_at_recommended:.0f}円 (自由JPY残高の{required_jpy_at_recommended/free_jpy*100:.0f}%)" if free_jpy > 0 else f"→ 買いグリッド必要額: 約{required_jpy_at_recommended:.0f}円 (自由JPY残高は0円)")

    required_xrp_at_recommended = recommended_sell * GRID_ENVELOPE.max_sell_levels
    print(f"→ 売りグリッド必要量: {required_xrp_at_recommended}XRP (自由XRP残高の{required_xrp_at_recommended/free_xrp*100:.0f}%)" if free_xrp > 0 else f"→ 売りグリッド必要量: {required_xrp_at_recommended}XRP (自由XRP残高は0枚)")

    if args.apply:
        update_config_file(recommended_buy, recommended_sell)
        print(f"\nconfig.pyのbuy_amount_per_level_xrpを{recommended_buy}に、sell_amount_per_level_xrpを{recommended_sell}に更新しました。")
    else:
        print("\n--applyを付けていないため、config.pyは変更していません。")


if __name__ == "__main__":
    main()
