"""
週次バックテスト結果からgrid_widthの変更を提案する。GitHub Actionsから呼ばれる。

Tier1候補としてPRを自動作成するが、自動マージは行わない
(実運用実績がまだ十分に蓄積していないため、意図的に無効化している)。

対象パラメータはgrid_widthのみに限定している。理由:
Walk-Forward検証の結果、レジームフィルタ(trend_window/threshold)は
過学習と判明し不採用となった一方、grid_widthの調整だけは訓練・検証
両期間で一貫して効果が確認された。信頼性が実証されていないパラメータを
自動化対象に含めるべきではない。

探索範囲は config.py の envelope(grid_width_min_jpy 〜 grid_width_max_jpy)
に自動的に収まる(このスクリプト自身がenvelope外の値を提案することはない)。

改善とみなす閾値: 現在値に対して total_pnl_jpy が MIN_IMPROVEMENT_RATIO 以上
改善している場合のみ提案する。わずかな差(ノイズ)での提案を防ぐため。
"""

import argparse
import dataclasses
import logging
import os
import re

from .paper_trading import sweep_grid_width_only, _load_trades_from_csv
from .config import GRID_ENVELOPE

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

MIN_IMPROVEMENT_RATIO = 0.10  # 現在値より10%以上PnLが改善しない限り提案しない
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.py")


def compute_width_candidates(cfg) -> list:
    """envelope範囲内を0.1円刻みで候補として生成する。"""
    widths = []
    w = cfg.grid_width_min_jpy
    while w <= cfg.grid_width_max_jpy + 1e-9:
        widths.append(round(w, 2))
        w += 0.1
    return widths


def update_config_file(new_width: float) -> None:
    with open(CONFIG_PATH) as f:
        content = f.read()

    pattern = r"(grid_width_default_jpy:\s*float\s*=\s*)[\d.]+"
    new_content, count = re.subn(pattern, rf"\g<1>{new_width}", content)
    if count != 1:
        raise RuntimeError(f"grid_width_default_jpyの置換に失敗しました(マッチ数={count})。config.pyの形式が変わっていないか確認してください。")

    with open(CONFIG_PATH, "w") as f:
        f.write(new_content)


def write_pr_body(current_width: float, current_pnl, best: dict, all_results: list, trades_csv: str) -> None:
    if current_pnl is not None and abs(current_pnl) > 1e-9:
        improvement_line = f"- 改善率: {(best['total_pnl_jpy'] / abs(current_pnl) - 1) * 100:.1f}%"
    else:
        improvement_line = "- 改善率: 算出不可(現在値の損益がゼロまたはスイープ対象外)"

    lines = [
        "## 週次バックテストによるgrid_width調整提案(Tier1候補)",
        "",
        "**このPRは自動マージされません。内容を確認の上、手動でマージしてください。**",
        "",
        f"- 現在値: `{current_width}` 円",
        f"- 提案値: `{best['width']}` 円",
        improvement_line,
        "",
        "### 全候補の結果",
        "",
        "| grid_width | 損益(円) | 期末純在庫(XRP) | 約定数 |",
        "|---|---|---|---|",
    ]
    for r in all_results:
        marker = " ← 提案" if r["width"] == best["width"] else (" (現在値)" if r["width"] == current_width else "")
        lines.append(f"| {r['width']} | {r['total_pnl_jpy']:.2f} | {r['net_inventory_xrp']:.2f} | {r['fills']}{marker} |")

    lines += [
        "",
        f"### 使用データ",
        f"- {trades_csv}",
        "",
        "### 注意",
        "- この提案は直近データへの過学習の可能性があります。マージ前に自身でも数値を確認してください。",
        "- HardStopLossManagerの閾値はこの自動化の対象外です(常に人間の変更が必要です)。",
    ]

    with open("PR_BODY.md", "w") as f:
        f.write("\n".join(lines))


def _current_pnl(results: list, current_width: float):
    for r in results:
        if r["width"] == current_width:
            return r["total_pnl_jpy"]
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trades-csv", required=True)
    parser.add_argument("--base-price", type=float, default=None, help="省略時はCSV末尾行(直近)の価格を使用")
    args = parser.parse_args()

    trades = list(_load_trades_from_csv(args.trades_csv))
    if not trades:
        logger.warning("約定履歴が空のため、提案をスキップします。")
        _write_github_output(False)
        return

    base_price = args.base_price if args.base_price is not None else trades[-1].price
    current_width = GRID_ENVELOPE.grid_width_default_jpy

    candidates = compute_width_candidates(GRID_ENVELOPE)
    results = sweep_grid_width_only(base_price, GRID_ENVELOPE, trades, candidates)

    best = results[0]
    current_pnl = _current_pnl(results, current_width)

    logger.info(f"現在値: {current_width}円 (PnL={current_pnl})")
    logger.info(f"最良候補: {best['width']}円 (PnL={best['total_pnl_jpy']})")

    should_propose = False
    if best["width"] != current_width and current_pnl is not None:
        if current_pnl <= 0:
            # 現在値が赤字の場合、改善率(割合)では判定できないため絶対額で判定する
            should_propose = best["total_pnl_jpy"] > current_pnl + abs(current_pnl) * MIN_IMPROVEMENT_RATIO
        else:
            should_propose = best["total_pnl_jpy"] >= current_pnl * (1 + MIN_IMPROVEMENT_RATIO)

    if should_propose:
        logger.info(f"改善が見込めるため提案します: {current_width} -> {best['width']}")
        update_config_file(best["width"])
        write_pr_body(current_width, current_pnl, best, results, args.trades_csv)
        _write_github_output(True)
    else:
        logger.info("有意な改善が見られないため、今回は提案しません。")
        _write_github_output(False)


def _write_github_output(proposal_needed: bool) -> None:
    gh_output = os.environ.get("GITHUB_OUTPUT")
    value = "true" if proposal_needed else "false"
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"proposal_needed={value}\n")
    else:
        logger.info(f"(GITHUB_OUTPUT未設定のためログのみ) proposal_needed={value}")


if __name__ == "__main__":
    main()
