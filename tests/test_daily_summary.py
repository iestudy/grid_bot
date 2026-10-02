from unittest.mock import MagicMock

import pytest

from src.daily_summary import send_daily_summary
from src.state_store import InMemoryStateStore, PortfolioState, DailySnapshot


def test_send_daily_summary_computes_delta_from_snapshot():
    store = InMemoryStateStore()
    store.save_portfolio_state(PortfolioState(realized_profit_jpy=500.0, total_fill_count=20))
    store.save_daily_snapshot(DailySnapshot(realized_profit_jpy=350.0, total_fill_count=15))

    notifier = MagicMock()
    send_daily_summary(store, notifier)

    notifier.notify_daily_summary.assert_called_once()
    args, kwargs = notifier.notify_daily_summary.call_args
    # (date_str, realized_profit_jpy, fill_count)
    assert args[1] == pytest.approx(150.0)  # 500 - 350
    assert args[2] == 5  # 20 - 15


def test_send_daily_summary_updates_snapshot_after_sending():
    store = InMemoryStateStore()
    store.save_portfolio_state(PortfolioState(realized_profit_jpy=500.0, total_fill_count=20))
    store.save_daily_snapshot(DailySnapshot(realized_profit_jpy=350.0, total_fill_count=15))

    notifier = MagicMock()
    send_daily_summary(store, notifier)

    new_snapshot = store.get_daily_snapshot()
    assert new_snapshot.realized_profit_jpy == pytest.approx(500.0)
    assert new_snapshot.total_fill_count == 20


def test_send_daily_summary_with_no_prior_snapshot_treats_all_as_today():
    store = InMemoryStateStore()
    store.save_portfolio_state(PortfolioState(realized_profit_jpy=100.0, total_fill_count=4))
    # スナップショット未保存 = デフォルト(0, 0)として扱われる

    notifier = MagicMock()
    send_daily_summary(store, notifier)

    args, kwargs = notifier.notify_daily_summary.call_args
    assert args[1] == pytest.approx(100.0)
    assert args[2] == 4


def test_send_daily_summary_clamps_negative_fill_count_to_zero():
    """
    reset_state.py実行でportfolio.total_fill_countがゼロにリセットされた後に
    daily_summaryが走ると、snapshot(リセット前の値)との差分が負になる
    (実際に2026-10-02、約定件数=-3件という不可能な値がSlackに送信された)。
    約定件数は物理的に負になり得ないため、0にクランプされることを確認する。
    """
    store = InMemoryStateStore()
    # reset_state.pyでゼロリセットされた後の状態(当日3件約定済み)
    store.save_portfolio_state(PortfolioState(realized_profit_jpy=10.0, total_fill_count=3))
    # リセット前、同日の朝に保存されていたスナップショット
    store.save_daily_snapshot(DailySnapshot(realized_profit_jpy=20.0, total_fill_count=6))

    notifier = MagicMock()
    send_daily_summary(store, notifier)

    args, kwargs = notifier.notify_daily_summary.call_args
    assert args[2] == 0  # 3 - 6 = -3 だが、0にクランプされる


def test_send_daily_summary_does_not_clamp_negative_profit_alone():
    """
    実現損益だけが負になるケース(本当に当日マイナスだった場合)は、
    fill_countが負でない限りクランプしない(自然な損益のマイナスを
    誤って隠してしまわないようにする)。
    """
    store = InMemoryStateStore()
    store.save_portfolio_state(PortfolioState(realized_profit_jpy=100.0, total_fill_count=20))
    store.save_daily_snapshot(DailySnapshot(realized_profit_jpy=150.0, total_fill_count=15))

    notifier = MagicMock()
    send_daily_summary(store, notifier)

    args, kwargs = notifier.notify_daily_summary.call_args
    assert args[1] == pytest.approx(-50.0)  # 100 - 150、クランプされない
    assert args[2] == 5  # 20 - 15、正の値なのでそのまま


def test_main_block_calls_load_dotenv():
    """
    daily_summary.pyの__main__ブロックがload_dotenv()を呼んでいることを
    ソースコードレベルで確認する回帰テスト。
    (env変数読み込み漏れにより、cron実行時にSLACK_WEBHOOK_URL等が
     読み込まれない事故が過去にあったため)
    """
    import inspect
    import src.daily_summary as module

    source = inspect.getsource(module)
    main_block_start = source.index('if __name__ == "__main__":')
    main_block = source[main_block_start:]
    assert "load_dotenv()" in main_block
