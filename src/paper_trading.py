"""
Paper Trading シミュレータ（実約定履歴ベース）。

設計思想:
  当初はbest_bid/best_askの「板タッチ」で約定判定していたが、これは
  bitbank Public APIで過去の板情報を取得できないため実現不可能だった。
  代わりに実際の約定履歴(transactions)を使い、以下のルールで判定する:

    自分の買い指値(price=P)が約定したとみなす条件:
      taker側が"sell"の約定が発生し、その約定価格が P より低い
      （厳密に下抜けた場合のみ。ちょうどPで止まった=タッチのみは約定とみなさない）

    自分の売り指値(price=P)が約定したとみなす条件:
      taker側が"buy"の約定が発生し、その約定価格が P より高い

  この「厳密な突き抜け」判定により、指値にタッチしただけで約定したとみなす
  楽観的シミュレーションを避ける（レビュー指摘の反映）。

  ただし、これでも以下の点で楽観的な近似であることは明記しておく:
  - 同一価格に自分より先に並んでいた他の指値注文（キューの先頭）の存在は考慮していない
  - 約定数量が自分の注文数量以上あるかは概ね妥当な前提だが、極端な薄商いでは崩れる
  実際の運用では、この楽観性を織り込んだ上でPaper Trading結果を「上限の見積もり」
  として扱い、本番の実約定率と比較検証すること。
"""

import argparse
from dataclasses import dataclass
from typing import Iterator, List, Optional

from .grid_engine import GridLevel


@dataclass
class Trade:
    """bitbank transactions APIの1レコードに対応"""
    timestamp: float
    side: str     # "buy" または "sell"（taker側の方向）
    price: float
    amount: float


@dataclass
class SimulatedFill:
    timestamp: float
    side: str
    price: float
    amount: float


class ConservativeFillSimulator:
    def __init__(self, default_amount: float = 8.0, strict_penetration: bool = True):
        self.default_amount = default_amount
        self.strict_penetration = strict_penetration

    def check_fill(self, level: GridLevel, trade: Trade) -> Optional[SimulatedFill]:
        amount = level.amount if level.amount > 0 else self.default_amount

        if level.side == "buy" and trade.side == "sell":
            filled = trade.price < level.price if self.strict_penetration else trade.price <= level.price
            if filled:
                return SimulatedFill(trade.timestamp, "buy", level.price, amount)

        if level.side == "sell" and trade.side == "buy":
            filled = trade.price > level.price if self.strict_penetration else trade.price >= level.price
            if filled:
                return SimulatedFill(trade.timestamp, "sell", level.price, amount)

        return None


def run_simulation(levels: List[GridLevel], trades: Iterator[Trade]) -> List[SimulatedFill]:
    """
    簡易版: 約定したレベルは再投入されない「使い捨てグリッド」のシミュレーション。
    実際のgrid botの挙動を大幅に過小評価するため、参考値としてのみ使うこと。
    現実的な見積もりには run_simulation_with_replenishment を使う。
    """
    simulator = ConservativeFillSimulator()
    fills: List[SimulatedFill] = []
    remaining_levels = list(levels)
    for trade in trades:
        still_open = []
        for level in remaining_levels:
            fill = simulator.check_fill(level, trade)
            if fill:
                fills.append(fill)
            else:
                still_open.append(level)
        remaining_levels = still_open
    return fills


def run_simulation_with_replenishment(
    base_price: float,
    cfg,  # GridEnvelopeConfig
    trades: Iterator[Trade],
    max_active_levels: int = None,
) -> List[SimulatedFill]:
    """
    現実的なgrid botの挙動を模擬する: あるレベルが約定したら、
    その価格からgrid_width分だけ反対側に新しいレベルを再投入する。
      - 買いが約定 → その価格+width で売りを再投入（ラウンドトリップの利確側）
      - 売りが約定 → その価格-width で買いを再投入（下がったところで買い直す）

    この「約定価格を起点に反対側へ再投入する」設計は、固定base_priceを
    前提にしないため、結果的にbase_price自動ドリフト補正と同種の効果
    （価格のトレンドに追従する）を持つ。ただし総アクティブレベル数の
    上限(max_active_levels)を超えては再投入しない（資金制約のシミュレーション）。
    """
    from .grid_engine import generate_grid, GridLevel

    if max_active_levels is None:
        max_active_levels = cfg.max_buy_levels + cfg.max_sell_levels

    levels = generate_grid(base_price, cfg)
    simulator = ConservativeFillSimulator(default_amount=cfg.amount_per_level_xrp)
    fills: List[SimulatedFill] = []
    width = cfg.grid_width_default_jpy

    for trade in trades:
        still_open = []
        newly_added = []
        for level in levels:
            fill = simulator.check_fill(level, trade)
            if fill:
                fills.append(fill)
                if len(levels) - 1 + len(newly_added) < max_active_levels:
                    if level.side == "buy":
                        newly_added.append(GridLevel(
                            side="sell", price=round(level.price + width, 4), amount=level.amount,
                        ))
                    else:
                        newly_added.append(GridLevel(
                            side="buy", price=round(level.price - width, 4), amount=level.amount,
                        ))
            else:
                still_open.append(level)
        levels = still_open + newly_added

    return fills


def run_simulation_with_stop_loss(
    base_price: float,
    cfg,  # GridEnvelopeConfig
    hard_stop_cfg,  # HardStopLossConfig
    trades: Iterator[Trade],
    trend_window: int = 500,
    trend_threshold_ratio: float = 0.01,
    taker_fee_rate: float = 0.0012,
    max_active_levels: int = None,
):
    """
    run_simulation_with_regime_filter に、HardStopLossManagerと同種の
    「含み損に応じた段階的損切り」ロジックを統合した版。

    強制決済(損切り)は成行相当とみなし、メイカーリベートではなく
    テイカー手数料(taker_fee_rate、デフォルト0.12%)を課す。指値が自然に
    約定するオーガニックなgrid取引とは区別して会計する。

    重要な設計判断: 全決済(FULL_CLOSE)がトリガーされたら、以降の取引は
    一切再開しない。これは本番設計（緊急停止後は人間のレビューを待つまで
    自動再開しない）とバックテスト上の挙動を一致させるためであり、
    単純化ではなく意図的な安全側の模擬である。

    戻り値: (fills, stop_events, halted)
      fills: オーガニックな約定 + 強制決済の両方を含む
      stop_events: 発生した損切りイベントのログ（デバッグ・検証用）
      halted: 全決済で停止したかどうか
    """
    from collections import deque
    from .grid_engine import generate_grid, GridLevel, detect_trend

    if max_active_levels is None:
        max_active_levels = cfg.max_buy_levels + cfg.max_sell_levels

    levels = generate_grid(base_price, cfg)
    simulator = ConservativeFillSimulator(default_amount=cfg.amount_per_level_xrp)
    fills: List[SimulatedFill] = []
    stop_events: List[dict] = []
    width = cfg.grid_width_default_jpy
    price_history: "deque[float]" = deque(maxlen=trend_window)

    cash_flow = 0.0
    net_inventory = 0.0
    stop_state = "NONE"  # NONE / PARTIAL / FULL(=halted)
    halted = False

    for trade in trades:
        if halted:
            break

        is_trend = False
        if len(price_history) >= trend_window:
            reference_price = price_history[0]
            is_trend = detect_trend(reference_price, trade.price, trend_threshold_ratio)

        # --- オーガニックなgrid約定処理 ---
        still_open = []
        newly_added = []
        for level in levels:
            fill = simulator.check_fill(level, trade)
            if fill:
                fills.append(fill)
                if fill.side == "buy":
                    cash_flow -= fill.price * fill.amount
                    net_inventory += fill.amount
                else:
                    cash_flow += fill.price * fill.amount
                    net_inventory -= fill.amount

                if not is_trend and len(levels) - 1 + len(newly_added) < max_active_levels:
                    if level.side == "buy":
                        newly_added.append(GridLevel(
                            side="sell", price=round(level.price + width, 4), amount=level.amount,
                        ))
                    else:
                        newly_added.append(GridLevel(
                            side="buy", price=round(level.price - width, 4), amount=level.amount,
                        ))
            else:
                still_open.append(level)
        levels = still_open + newly_added

        # --- 含み損評価とストップロス判定 ---
        running_pnl = cash_flow + net_inventory * trade.price
        loss = max(0.0, -running_pnl)
        drawdown_ratio = loss / hard_stop_cfg.total_capital_jpy

        if drawdown_ratio >= hard_stop_cfg.max_drawdown_ratio and stop_state != "FULL":
            close_amount = net_inventory
            if close_amount != 0:
                fee = abs(close_amount) * trade.price * taker_fee_rate
                if close_amount > 0:
                    cash_flow += close_amount * trade.price - fee
                    fills.append(SimulatedFill(trade.timestamp, "sell", trade.price, close_amount))
                else:
                    cash_flow += close_amount * trade.price - fee  # close_amount負なので買い戻し
                    fills.append(SimulatedFill(trade.timestamp, "buy", trade.price, abs(close_amount)))
                net_inventory = 0.0
            levels = []  # 全ての未約定注文もキャンセル(緊急停止)
            stop_state = "FULL"
            halted = True
            stop_events.append({
                "type": "FULL_CLOSE", "timestamp": trade.timestamp,
                "price": trade.price, "drawdown_ratio": drawdown_ratio,
            })

        elif drawdown_ratio >= hard_stop_cfg.partial_close_ratio and stop_state == "NONE":
            close_amount = net_inventory * hard_stop_cfg.partial_close_fraction
            if close_amount != 0:
                fee = abs(close_amount) * trade.price * taker_fee_rate
                if close_amount > 0:
                    cash_flow += close_amount * trade.price - fee
                    fills.append(SimulatedFill(trade.timestamp, "sell", trade.price, close_amount))
                else:
                    cash_flow += close_amount * trade.price - fee
                    fills.append(SimulatedFill(trade.timestamp, "buy", trade.price, abs(close_amount)))
                net_inventory -= close_amount
            stop_state = "PARTIAL"
            stop_events.append({
                "type": "PARTIAL_CLOSE", "timestamp": trade.timestamp,
                "price": trade.price, "drawdown_ratio": drawdown_ratio,
            })

        elif drawdown_ratio < hard_stop_cfg.partial_close_ratio * 0.5 and stop_state == "PARTIAL":
            stop_state = "NONE"  # 回復したら再度部分決済を発動できる状態に戻す

        price_history.append(trade.price)

    return fills, stop_events, halted


def run_parameter_sweep(
    base_price: float,
    base_cfg,  # GridEnvelopeConfig
    hard_stop_cfg,  # HardStopLossConfig
    trades_list: List[Trade],
    trend_windows: List[int],
    trend_thresholds: List[float],
    grid_widths: List[float],
) -> List[dict]:
    """
    grid_width / trend_window / trend_threshold の組み合わせを総当たりし、
    run_simulation_with_stop_loss（最も現実的な統合版）で評価する。

    注意: これは同一データセットへの過学習リスクを伴う探索である。
    ここで見つけた「最良の組み合わせ」は、必ず別期間のデータ（out-of-sample）
    で再検証してから採用すること。このスイープ結果だけで本番パラメータを
    決定してはならない。
    """
    import dataclasses

    results = []
    final_price = trades_list[-1].price if trades_list else base_price

    for width in grid_widths:
        cfg = dataclasses.replace(base_cfg, grid_width_default_jpy=width)
        for window in trend_windows:
            for threshold in trend_thresholds:
                fills, stop_events, halted = run_simulation_with_stop_loss(
                    base_price, cfg, hard_stop_cfg, iter(trades_list),
                    trend_window=window, trend_threshold_ratio=threshold,
                )
                pnl = compute_pnl(fills, final_price)
                results.append({
                    "grid_width": width,
                    "trend_window": window,
                    "trend_threshold": threshold,
                    "total_pnl_jpy": pnl["total_pnl_jpy"],
                    "net_inventory_xrp": pnl["net_inventory_xrp"],
                    "fills": len(fills),
                    "stop_events": len(stop_events),
                    "halted": halted,
                })

    results.sort(key=lambda r: r["total_pnl_jpy"], reverse=True)
    return results


def run_simulation_with_regime_filter(
    base_price: float,
    cfg,  # GridEnvelopeConfig
    trades: Iterator[Trade],
    trend_window: int = 500,
    trend_threshold_ratio: float = 0.01,
    max_active_levels: int = None,
) -> List[SimulatedFill]:
    """
    run_simulation_with_replenishment に「トレンド判定時は新規発注(再投入)を
    停止する」ロジックを追加した版。

    trend_window: 直近何件のtradeを基準価格として使うか
    trend_threshold_ratio: 基準価格からの変化率がこの値以上ならトレンドと判定

    重要な設計上の注意:
    - トレンド判定中でも「既にactiveなレベル」は約定し得る（安全側フォールバック
      の考え方=新規発注停止のみで、既存注文のキャンセルはHardStopLossManagerの
      領分であり、ここでは行わない）
    - 判定に使う基準価格は必ず trend_window 件「前」の価格。現在のtradeを
      含む範囲を使うと先読みバイアスになるため、価格履歴への追加は
      約定判定の後に行う。
    """
    from collections import deque
    from .grid_engine import generate_grid, GridLevel, detect_trend

    if max_active_levels is None:
        max_active_levels = cfg.max_buy_levels + cfg.max_sell_levels

    levels = generate_grid(base_price, cfg)
    simulator = ConservativeFillSimulator(default_amount=cfg.amount_per_level_xrp)
    fills: List[SimulatedFill] = []
    width = cfg.grid_width_default_jpy
    price_history: "deque[float]" = deque(maxlen=trend_window)
    trend_pause_count = 0

    for trade in trades:
        # 判定は必ず「これまでの価格履歴」のみを使う（未来を見ない）
        is_trend = False
        if len(price_history) >= trend_window:
            reference_price = price_history[0]
            is_trend = detect_trend(reference_price, trade.price, trend_threshold_ratio)
            if is_trend:
                trend_pause_count += 1

        still_open = []
        newly_added = []
        for level in levels:
            fill = simulator.check_fill(level, trade)
            if fill:
                fills.append(fill)
                if not is_trend and len(levels) - 1 + len(newly_added) < max_active_levels:
                    if level.side == "buy":
                        newly_added.append(GridLevel(
                            side="sell", price=round(level.price + width, 4), amount=level.amount,
                        ))
                    else:
                        newly_added.append(GridLevel(
                            side="buy", price=round(level.price - width, 4), amount=level.amount,
                        ))
                # トレンド判定中は新規発注(再投入)しない。約定自体は成立させる
                # （既存の指値が実際に約定した事実は変えられないため）
            else:
                still_open.append(level)
        levels = still_open + newly_added

        price_history.append(trade.price)

    return fills, trend_pause_count


def _load_trades_from_csv(path: str) -> Iterator[Trade]:
    import csv
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            yield Trade(
                timestamp=float(row["timestamp"]),
                side=row["side"],
                price=float(row["price"]),
                amount=float(row["amount"]),
            )


def summarize(fills: List[SimulatedFill], maker_rebate_rate: float = 0.0002) -> dict:
    buy_fills = [f for f in fills if f.side == "buy"]
    sell_fills = [f for f in fills if f.side == "sell"]
    total_rebate = sum(f.price * f.amount for f in fills) * maker_rebate_rate
    return {
        "buy_count": len(buy_fills),
        "sell_count": len(sell_fills),
        "total_fills": len(fills),
        "estimated_rebate_jpy": round(total_rebate, 2),
    }


def compute_pnl(fills: List[SimulatedFill], final_price: float, maker_rebate_rate: float = 0.0002) -> dict:
    """
    fills全体のキャッシュフロー + 期末在庫の時価評価でトータル損益を計算する。
      cash_flow = 売り約定の受取総額 - 買い約定の支払総額
      net_inventory = 買い数量合計 - 売り数量合計（プラスならXRPを積み増した状態）
      total_pnl = cash_flow + net_inventory * final_price + リベート収益

    この方法は「個々のラウンドトリップを追跡する」よりシンプルだが、
    グリッド戦略の実質的な損益（値幅差益 + 在庫の時価変動 + リベート）を
    過不足なく捉えられる。
    """
    cash_flow = 0.0
    net_inventory = 0.0
    for f in fills:
        if f.side == "buy":
            cash_flow -= f.price * f.amount
            net_inventory += f.amount
        else:
            cash_flow += f.price * f.amount
            net_inventory -= f.amount

    rebate = sum(f.price * f.amount for f in fills) * maker_rebate_rate
    inventory_value = net_inventory * final_price
    total_pnl = cash_flow + inventory_value + rebate

    return {
        "cash_flow_jpy": round(cash_flow, 2),
        "net_inventory_xrp": round(net_inventory, 4),
        "inventory_value_at_final_price_jpy": round(inventory_value, 2),
        "rebate_jpy": round(rebate, 2),
        "total_pnl_jpy": round(total_pnl, 2),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", default="xrp_jpy")
    parser.add_argument("--trades-csv", default=None, help="timestamp,side,price,amount 列を持つCSV（data_fetch.pyで生成）")
    parser.add_argument("--base-price", type=float, default=159.61)
    parser.add_argument("--trend-window", type=int, default=500, help="トレンド判定に使う直近trade件数")
    parser.add_argument("--trend-threshold", type=float, default=0.01, help="トレンド判定の変化率閾値(0.01=1%)")
    parser.add_argument("--sweep", action="store_true", help="パラメータスイープモード(grid_width/trend_window/trend_thresholdを総当たり)")
    parser.add_argument("--grid-width", type=float, default=None, help="grid幅(円)を明示指定。省略時はconfig.pyのデフォルト値(0.5円)")
    args = parser.parse_args()

    import dataclasses
    from .grid_engine import generate_grid
    from .config import GRID_ENVELOPE, HARD_STOP_LOSS

    cfg = GRID_ENVELOPE if args.grid_width is None else dataclasses.replace(GRID_ENVELOPE, grid_width_default_jpy=args.grid_width)
    grid = generate_grid(args.base_price, cfg)

    if args.trades_csv and args.sweep:
        trades_all = list(_load_trades_from_csv(args.trades_csv))
        results = run_parameter_sweep(
            args.base_price, cfg, HARD_STOP_LOSS, trades_all,
            trend_windows=[500, 1000, 2000, 3000, 5000],
            trend_thresholds=[0.005, 0.01, 0.015, 0.02, 0.03],
            grid_widths=[0.3, 0.4, 0.5, 0.6, 0.8, 1.0],
        )
        print(f"{'width':>6} {'window':>7} {'thresh':>7} {'PnL(円)':>10} {'在庫変化':>9} {'約定数':>6} {'損切り':>6} {'停止':>5}")
        for r in results[:20]:
            print(f"{r['grid_width']:>6} {r['trend_window']:>7} {r['trend_threshold']:>7} "
                  f"{r['total_pnl_jpy']:>10} {r['net_inventory_xrp']:>9} {r['fills']:>6} "
                  f"{r['stop_events']:>6} {'Y' if r['halted'] else 'N':>5}")
        print()
        print(f"総組み合わせ数: {len(results)}")
        print("※ これは同一データへの過学習探索です。上位の組み合わせは必ず別期間のデータで再検証してください。")
    elif args.trades_csv:
        trades_all = list(_load_trades_from_csv(args.trades_csv))
        final_price = trades_all[-1].price if trades_all else args.base_price

        # 1. 簡易版（使い捨てグリッド、参考値）
        fills_naive = run_simulation(grid, iter(trades_all))
        pnl_naive = compute_pnl(fills_naive, final_price)

        # 2. 現実的版（ラウンドトリップ再投入あり、レジーム判定なし）
        fills_replenished = run_simulation_with_replenishment(args.base_price, cfg, iter(trades_all))
        pnl_replenished = compute_pnl(fills_replenished, final_price)

        # 3. レジームフィルタ版（トレンド判定時は新規発注停止）
        fills_regime, trend_pause_count = run_simulation_with_regime_filter(
            args.base_price, cfg, iter(trades_all),
            trend_window=args.trend_window, trend_threshold_ratio=args.trend_threshold,
        )
        pnl_regime = compute_pnl(fills_regime, final_price)

        def _print_result(title, fills, pnl):
            buy_n = len([f for f in fills if f.side == "buy"])
            sell_n = len([f for f in fills if f.side == "sell"])
            print(f"=== {title} ===")
            print(f"買い約定: {buy_n}件 / 売り約定: {sell_n}件")
            print(f"  値幅差損益: {pnl['cash_flow_jpy']} 円 / 在庫評価: {pnl['inventory_value_at_final_price_jpy']} 円")
            print(f"  リベート: {pnl['rebate_jpy']} 円 / 合計損益: {pnl['total_pnl_jpy']} 円")
            print(f"  期末純在庫変化: {pnl['net_inventory_xrp']} XRP")
            print()

        _print_result("① 簡易版（使い捨てグリッド、参考値）", fills_naive, pnl_naive)
        _print_result("② 現実的版（ラウンドトリップ再投入あり、レジーム判定なし）", fills_replenished, pnl_replenished)
        _print_result(
            f"③ レジームフィルタ版（trend_window={args.trend_window}, threshold={args.trend_threshold}）",
            fills_regime, pnl_regime,
        )
        print(f"トレンド判定でスキップされた新規発注: {trend_pause_count}件")
        print()

        # 4. ストップロス統合版
        fills_stop, stop_events, halted = run_simulation_with_stop_loss(
            args.base_price, cfg, HARD_STOP_LOSS, iter(trades_all),
            trend_window=args.trend_window, trend_threshold_ratio=args.trend_threshold,
        )
        pnl_stop = compute_pnl(fills_stop, final_price)
        _print_result("④ ストップロス統合版（段階的損切りあり）", fills_stop, pnl_stop)
        print(f"損切りイベント: {len(stop_events)}件")
        for ev in stop_events:
            print(f"  {ev['type']} @ {ev['price']}円 (drawdown_ratio={ev['drawdown_ratio']:.3f})")
        print(f"緊急停止で取引終了: {'はい' if halted else 'いいえ'}")
        print()

        print("※ 出金・税金コストは含まず。実運用の約定率はこれより低くなる前提で見ること。")
        print("※ ③④のtrend_window/thresholdは決め打ちの初期値。複数パターンで比較して妥当性を検証すること。")
    else:
        print("--trades-csv で data_fetch.py が生成したCSVを指定してください。")
        print(f"生成されたグリッド（{len(grid)}本）:")
        for level in grid:
            print(f"  {level.side}: {level.price} ({level.amount} XRP)")
