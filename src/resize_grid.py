"""
実際の口座残高(自由JPY)に基づいて amount_per_level_xrp を再計算し、
config.py を更新する。

当初の8XRP/レベルという値は「JPY自由残高8,300円、価格159円」という
開始時点の資金配分を前提に決めたものであり、その後の資産構成の変化
(JPYが大きく減りXRP保有が増える等)によって前提が崩れた場合、
買い注文が恒常的に残高不足(60001)で失敗し続ける事態になる。
このツールは、その時点の実際の残高から妥当な値を再計算する。

安全マージンの考え方:
  自由JPY残高の一部(デフォルト70%)だけを買いグリッドの予算として使い、
  残りは価格変動・手数料等のバッファとして温存する。
  100%を使い切る設計にすると、わずかな価格上昇で再び全滅するリスクがある。

使い方:
    python3 -m src.resize_grid --pair xrp_jpy --apply
  --applyを付けない場合は計算結果を表示するのみで、config.pyは変更しない。
"""

import argparse
import logging
import os
import re

from dotenv import load_dotenv

from .bitbank_client import BitbankClient
from .config import GRID_ENVELOPE
from .grid_engine import required_buy_side_jpy

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.py")
BUDGET_UTILIZATION_RATIO = 0.70  # 自由JPY残高の何割を買いグリッド予算として使うか


def compute_recommended_amount_per_level(
    free_jpy: float,
    base_price: float,
    cfg=GRID_ENVELOPE,
    budget_ratio: float = BUDGET_UTILIZATION_RATIO,
) -> float:
    """
    required_buy_side_jpy(amount=1XRP換算での必要額)を基準に、
    budget_ratio * free_jpy に収まる amount_per_level_xrp を逆算する。
    """
    import dataclasses

    unit_cfg = dataclasses.replace(cfg, amount_per_level_xrp=1.0)
    cost_per_unit = required_buy_side_jpy(unit_cfg, base_price)
    if cost_per_unit <= 0:
        raise ValueError("cost_per_unitが0以下です。base_price/grid_widthの設定を確認してください。")

    budget = free_jpy * budget_ratio
    recommended = budget / cost_per_unit
    return round(recommended, 1)


def update_config_file(new_amount: float) -> None:
    with open(CONFIG_PATH) as f:
        content = f.read()

    pattern = r"(amount_per_level_xrp:\s*float\s*=\s*)[\d.]+"
    new_content, count = re.subn(pattern, rf"\g<1>{new_amount}", content)
    if count != 1:
        raise RuntimeError(f"amount_per_level_xrpの置換に失敗しました(マッチ数={count})。")

    with open(CONFIG_PATH, "w") as f:
        f.write(new_content)


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

    current_amount = GRID_ENVELOPE.amount_per_level_xrp
    recommended = compute_recommended_amount_per_level(free_jpy, current_price, budget_ratio=args.budget_ratio)

    print(f"現在価格: {current_price}円")
    print(f"自由JPY残高: {free_jpy}円 / 自由XRP残高: {free_xrp}枚")
    print(f"現在のamount_per_level_xrp: {current_amount}")
    print(f"推奨amount_per_level_xrp: {recommended} (JPY予算の{args.budget_ratio*100:.0f}%を買いグリッドに割り当てた場合)")

    required_jpy_at_recommended = required_buy_side_jpy(
        __import__("dataclasses").replace(GRID_ENVELOPE, amount_per_level_xrp=recommended), current_price,
    )
    print(f"→ 買いグリッド必要額: 約{required_jpy_at_recommended:.0f}円 (自由JPY残高の{required_jpy_at_recommended/free_jpy*100:.0f}%)")

    required_xrp_at_recommended = recommended * GRID_ENVELOPE.max_sell_levels
    print(f"→ 売りグリッド必要量: {required_xrp_at_recommended}XRP (自由XRP残高の{required_xrp_at_recommended/free_xrp*100:.0f}%)" if free_xrp > 0 else "")

    if args.apply:
        update_config_file(recommended)
        print(f"\nconfig.pyのamount_per_level_xrpを{recommended}に更新しました。")
    else:
        print("\n--applyを付けていないため、config.pyは変更していません。")


if __name__ == "__main__":
    main()
