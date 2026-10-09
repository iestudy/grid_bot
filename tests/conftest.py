"""
全テスト共通のフィクスチャ。

run_loop.pyはEMERGENCY_STOP_FLAG_PATH(EMERGENCY_STOP発動時)と
HEARTBEAT_PATH(各iteration開始時)に、本物のファイルシステム
(grid_bot/run/配下)へ書き込む設計になっている。個々のテストで
monkeypatchし忘れると、テスト実行のたびに本物のファイルへ書き込んで
しまう(実際に2026-09-08と09-18、2種類のフラグファイルでこの問題が
発生した)。

autouseのフィクスチャで全テストに対して自動的にtmp_pathへ
リダイレクトすることで、個々のテストでのmonkeypatch忘れを構造的に
防ぐ。
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_run_loop_flag_files(tmp_path, monkeypatch):
    try:
        import src.run_loop as run_loop_module
    except Exception:
        # run_loopをimportできない/依存関係が無いテストファイルもあるため、
        # importできない場合は何もしない。
        yield
        return

    monkeypatch.setattr(run_loop_module, "EMERGENCY_STOP_FLAG_PATH", tmp_path / "run" / "emergency_stop.flag")
    monkeypatch.setattr(run_loop_module, "HEARTBEAT_PATH", tmp_path / "run" / "heartbeat.flag")
    # reset_state.pyも、リセット時の実残高を run/reset_baseline.json に書き込む。
    # 隔離しないと、reset_state()を呼ぶテストが本物のファイルを汚染する
    # (2026-10-09、モックのclientから残高の無い基準値が書き込まれた)。
    try:
        import src.reset_state as reset_state_module
        monkeypatch.setattr(reset_state_module, "RESET_BASELINE_PATH", tmp_path / "run" / "reset_baseline.json")
    except Exception:
        pass
    yield
