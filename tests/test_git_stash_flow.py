"""
run_incident_response.pyのStep 4(git stash -> pull -> stash pop)と同じ操作列を、
一時リポジトリで再現して、本物のgitの挙動を確認する。

背景: 2026-10-15、resize_grid.py --applyが書いた未コミットのconfig.pyを持ったまま
git pullしたため、origin/mainのconfig.py更新と衝突し、5回リトライしても失敗した。
(通信不調ではなかった。)
"""
import subprocess
from pathlib import Path


def _git(cwd, *args, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stdout}{r.stderr}")
    return r


def _setup(tmp_path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "-b", "main")

    work = tmp_path / "work"
    _git(tmp_path, "clone", str(remote), "work")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    cfg = work / "config.py"
    cfg.write_text(
        "grid_width: float = 1.0\n"
        "buy_amount: float = 0.1\n"
        "sell_amount: float = 13.3\n"
        "# base_priceからの片道乖離がこの値以上になったら、新規発注を一時停止する。\n# (既存注文はそのまま維持される)\n#\n"
        "halt_deviation: float = 5.0\n"
    )
    _git(work, "add", ".")
    _git(work, "commit", "-m", "init")
    _git(work, "push", "-u", "origin", "main")

    other = tmp_path / "other"
    _git(tmp_path, "clone", str(remote), "other")
    _git(other, "config", "user.email", "t@example.com")
    _git(other, "config", "user.name", "t")
    return work, other


def _flow(work):
    """Step 4と同じ操作列。pop失敗時はFalseを返す。"""
    stash = _git(work, "stash")
    stashed = "No local changes" not in stash.stdout
    _git(work, "checkout", "main")
    _git(work, "pull", "origin", "main")
    if stashed:
        pop = _git(work, "stash", "pop", check=False)
        if pop.returncode != 0:
            _git(work, "reset", "--hard", "HEAD")
            return False
    return True


def test_pull_without_stash_fails_on_overlapping_file(tmp_path):
    """再現: 未コミットの変更を持ったままpullすると、別の行の更新でも拒否される。"""
    work, other = _setup(tmp_path)
    (other / "config.py").write_text(
        "grid_width: float = 1.0\nbuy_amount: float = 0.1\nsell_amount: float = 13.3\n# base_priceからの片道乖離がこの値以上になったら、新規発注を一時停止する。\n# (既存注文はそのまま維持される)\n#\nhalt_deviation: float = 2.0\n"
    )
    _git(other, "commit", "-am", "daily proposal")
    _git(other, "push", "origin", "main")

    (work / "config.py").write_text(
        "grid_width: float = 1.0\nbuy_amount: float = 8.9\nsell_amount: float = 8.5\n# base_priceからの片道乖離がこの値以上になったら、新規発注を一時停止する。\n# (既存注文はそのまま維持される)\n#\nhalt_deviation: float = 5.0\n"
    )
    r = _git(work, "pull", "origin", "main", check=False)
    assert r.returncode != 0
    assert "overwritten" in (r.stdout + r.stderr) or "local changes" in (r.stdout + r.stderr)


def test_stash_flow_merges_changes_on_different_lines(tmp_path):
    """修正後の操作列: 別の行なら、両方の変更が残る。"""
    work, other = _setup(tmp_path)
    (other / "config.py").write_text(
        "grid_width: float = 1.0\nbuy_amount: float = 0.1\nsell_amount: float = 13.3\n# base_priceからの片道乖離がこの値以上になったら、新規発注を一時停止する。\n# (既存注文はそのまま維持される)\n#\nhalt_deviation: float = 2.0\n"
    )
    _git(other, "commit", "-am", "daily proposal")
    _git(other, "push", "origin", "main")

    (work / "config.py").write_text(
        "grid_width: float = 1.0\nbuy_amount: float = 8.9\nsell_amount: float = 8.5\n# base_priceからの片道乖離がこの値以上になったら、新規発注を一時停止する。\n# (既存注文はそのまま維持される)\n#\nhalt_deviation: float = 5.0\n"
    )
    assert _flow(work) is True
    text = (work / "config.py").read_text()
    assert "buy_amount: float = 8.9" in text
    assert "sell_amount: float = 8.5" in text
    assert "halt_deviation: float = 2.0" in text  # origin側の更新も取り込まれている


def test_stash_flow_keeps_changes_when_same_line_conflicts(tmp_path):
    """同じ行を両方が更新したら、popは失敗する。変更はstashに残り、失われない。"""
    work, other = _setup(tmp_path)
    (other / "config.py").write_text(
        "grid_width: float = 1.0\nbuy_amount: float = 5.5\nsell_amount: float = 13.3\n# base_priceからの片道乖離がこの値以上になったら、新規発注を一時停止する。\n# (既存注文はそのまま維持される)\n#\nhalt_deviation: float = 5.0\n"
    )
    _git(other, "commit", "-am", "someone changed buy_amount")
    _git(other, "push", "origin", "main")

    (work / "config.py").write_text(
        "grid_width: float = 1.0\nbuy_amount: float = 8.9\nsell_amount: float = 8.5\n# base_priceからの片道乖離がこの値以上になったら、新規発注を一時停止する。\n# (既存注文はそのまま維持される)\n#\nhalt_deviation: float = 5.0\n"
    )
    assert _flow(work) is False
    assert "stash@{0}" in _git(work, "stash", "list").stdout  # 変更はstashに残っている
    assert "<<<<<<<" not in (work / "config.py").read_text()  # 作業ツリーに衝突マーカーが残っていない


def test_stash_flow_without_local_changes_does_not_fail(tmp_path):
    """未コミットの変更がない場合(No local changes)も、正常に完了する。"""
    work, other = _setup(tmp_path)
    assert _flow(work) is True
