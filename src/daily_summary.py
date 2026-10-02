"""
日次サマリーをSlackに送信する。cron等で1日1回起動する独立スクリプト。

run_loopプロセス内にタイマーを持たせず、あえて別プロセス・別スケジューラ
(cron)に分離している。理由:
- run_loopの再起動・クラッシュに影響されず、確実に1日1回実行できる
- 通知ロジックの障害が取引ループ本体に影響しない

「当日分」の実現損益・約定件数は、前回のスナップショット(DailySnapshot)からの
差分として計算する。累積値(PortfolioState.realized_profit_jpy等)自体は
botが起動してからの全期間の合計であり、当日分だけを取り出すには
スナップショットとの差分を取る必要がある。

使い方(cron例、毎日23:55に実行):
    55 23 * * * cd /home/ec2-user/grid_bot && venv/bin/python3 -m src.daily_summary --use-dynamodb
"""

import argparse
import logging
import time
from datetime import datetime, timezone, timedelta

from .state_store import InMemoryStateStore, DynamoDBStateStore, DailySnapshot
from .notifications import SlackNotifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))


def send_daily_summary(store, notifier: SlackNotifier) -> None:
    portfolio = store.get_portfolio_state()
    snapshot = store.get_daily_snapshot()

    delta_profit = portfolio.realized_profit_jpy - snapshot.realized_profit_jpy
    delta_fill_count = portfolio.total_fill_count - snapshot.total_fill_count

    # reset_state.py実行でportfolio側の累積値(total_fill_count/realized_profit_jpy)が
    # ゼロにリセットされるため、その日のうちにリセットが挟まると差分が負になる
    # (実際に2026-10-02に発生: 約定件数=-3件)。
    # total_fill_countは物理的に負になり得ないため、負の差分が出たら
    # 「リセットを挟んだため当日分を正確には計算できない」とみなし0にクランプする。
    # realized_profit_jpyは本当に当日マイナスだった可能性もあるため自動クランプは
    # せず、ログでの注意喚起のみ行う(正確な値が必要な場合はreset_state.py実行ログ
    # と突き合わせて手動で確認すること)。
    if delta_fill_count < 0:
        logger.warning(
            f"約定件数の差分が負({delta_fill_count}件)になったため0にクランプします。"
            f"当日中にreset_state.pyが実行された可能性があります(portfolio.total_fill_count="
            f"{portfolio.total_fill_count}, snapshot.total_fill_count={snapshot.total_fill_count})。"
        )
        delta_fill_count = 0
        if delta_profit < 0:
            logger.warning(
                f"約定件数と同時に実現損益の差分も負({delta_profit:+.2f}円)です。"
                f"reset_state.pyによるリセットの影響で、この値は不正確な可能性があります。"
            )

    date_str = datetime.now(JST).strftime("%Y-%m-%d")
    notifier.notify_daily_summary(date_str, delta_profit, delta_fill_count)
    logger.info(f"日次サマリー送信: {date_str} 実現損益={delta_profit:+.2f}円 約定件数={delta_fill_count}件")

    # 送信後、今の累積値を新しいスナップショットとして保存(次回はここからの差分になる)
    store.save_daily_snapshot(DailySnapshot(
        realized_profit_jpy=portfolio.realized_profit_jpy,
        total_fill_count=portfolio.total_fill_count,
        timestamp=time.time(),
    ))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--use-dynamodb", action="store_true")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv()

    store = DynamoDBStateStore() if args.use_dynamodb else InMemoryStateStore()
    if not args.use_dynamodb:
        logger.warning("InMemoryStateStoreを使用しています。run_loopと別プロセスの場合、常に差分ゼロになります。")

    notifier = SlackNotifier()
    send_daily_summary(store, notifier)
