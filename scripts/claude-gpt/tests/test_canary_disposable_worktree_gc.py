"""scripts/claude-gpt/tests/test_canary_disposable_worktree_gc.py

Issue #2906: Claude-GPT canary が作る使い捨て worktree / branch の orphan GC と、
既存の削除処理 (`_remove_disposable_worktree` / `_prepare_disposable_worktree` の失敗経路) の是正。

この file は mock で挙動を偽装しない。tmp の git repo 上で実 git を起動し、owner の寿命は短命の
実 subprocess (親の SIGKILL を含む) で固定し、remove 拒否 / timeout / GC 途中中断は `canary._git` の
差し込み (実 git に lock を掛ける等) による failure injection で再現する。live Claude / gh / proxy は使わない。

Runtime Verification Applicability: immediate (AC8)。explicit GC の CLI を subprocess として実際に起動し、
exit code と stdout を観測する (`test_cli_gc_dry_run_changes_nothing`)。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TESTS_DIR.parent
CANARY_PY = SCRIPT_DIR / "auto_mode_canary.py"


def _load_canary_module():
    spec = importlib.util.spec_from_file_location("auto_mode_canary_gc_under_test", CANARY_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass の postponed annotation 解決に必要
    spec.loader.exec_module(module)
    return module


canary = _load_canary_module()

HOLDER_PREFIX = canary.CANONICAL_WORKFLOW_DISPOSABLE_HOLDER_PREFIX
BRANCH_PREFIX = canary.CANONICAL_WORKFLOW_DISPOSABLE_BRANCH_PREFIX

# 実 launch 経路の owner 寿命を観測するため、別プロセスで canary の関数を呼ぶ小さな driver。
_CHILD_PREPARE = """
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("auto_mode_canary_gc_child", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
target, reason = module._prepare_disposable_worktree(Path(sys.argv[2]))
print(target if target is not None else "NONE:" + str(reason), flush=True)
"""

_CHILD_RUN_SIDE = """
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("auto_mode_canary_gc_child", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module._run_canonical_workflow_side(
    Path(sys.argv[3]), Path(sys.argv[2]), module.canonical_workflow_prompt(), timeout=120.0
)
"""


def _git_ok(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", "-c", "user.email=canary@example.invalid", "-c", "user.name=canary", *args],
        cwd=str(cwd), capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, (args, result.stderr)
    return result.stdout.strip()


def _git_rc(*args: str, cwd: Path) -> int:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60, check=False
    ).returncode


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git_ok("init", "-q", "-b", "main", cwd=repo)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git_ok("add", "README.md", cwd=repo)
    _git_ok("commit", "-q", "-m", "init", cwd=repo)
    _git_ok("remote", "add", "origin", f"https://github.com/{canary.TRUSTED_REPO}.git", cwd=repo)
    return repo


def _root(repo: Path) -> Path:
    return Path(os.path.realpath(repo / ".claude" / "worktrees"))


def _branches(repo: Path) -> set[str]:
    return set(_git_ok("branch", "--format=%(refname:short)", cwd=repo).split())


def _porcelain(repo: Path) -> str:
    return _git_ok("worktree", "list", "--porcelain", cwd=repo)


def _suffix(target: Path) -> str:
    return target.parent.name[len(HOLDER_PREFIX):]


def _branch_of(target: Path) -> str:
    return BRANCH_PREFIX + _suffix(target)


def _prepare_in_dead_child(repo: Path) -> Path:
    """別プロセスで実際に `_prepare_disposable_worktree` を呼んで終了させる = 終了済み owner の残骸を作る。"""
    result = subprocess.run(
        [sys.executable, "-c", _CHILD_PREPARE, str(CANARY_PY), str(repo)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    out = result.stdout.strip()
    assert result.returncode == 0 and not out.startswith("NONE:"), (out, result.stderr)
    return Path(out)


def _snapshot(repo: Path, *, exclude: tuple[str, ...] = (), with_mtime: bool = False) -> dict:
    """refs・worktree 登録・`.claude/worktrees` 配下の filesystem (marker 含む) の観測。"""
    refs = [
        line for line in _git_ok("for-each-ref", "--format=%(refname) %(objectname)", cwd=repo).splitlines()
        if not any(token in line for token in exclude)
    ]
    blocks = [b for b in _porcelain(repo).split("\n\n") if not any(token in b for token in exclude)]
    files: dict[str, object] = {}
    root = repo / ".claude" / "worktrees"
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(dirnames) + sorted(filenames):
            path = Path(dirpath) / name
            rel = str(path.relative_to(root))
            if any(token in rel for token in exclude):
                continue
            st = os.lstat(path)
            if path.is_symlink():
                files[rel] = ("symlink", os.readlink(path))
            elif path.is_dir():
                files[rel] = ("dir", st.st_mtime_ns if with_mtime else None)
            else:
                files[rel] = ("file", path.read_bytes(), st.st_mtime_ns if with_mtime else None)
    return {"refs": refs, "worktrees": blocks, "files": files}


def _entry(report: dict, holder_name: str) -> dict:
    matches = [c for c in report["candidates"] if c["holder"] == holder_name]
    assert len(matches) == 1, report
    return matches[0]


def _wrap_git(monkeypatch, hook):
    """canary._git の差し込み。hook(args, cwd, timeout, real) が None 以外を返せばそれを結果にする。"""
    real = canary._git

    def wrapper(args, *, cwd, timeout=60.0, **kwargs):
        injected = hook(list(args), cwd, timeout, real)
        return injected if injected is not None else real(args, cwd=cwd, timeout=timeout, **kwargs)

    monkeypatch.setattr(canary, "_git", wrapper)


def _no_rmtree(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("shutil.rmtree must not be used to bypass a rejected git worktree remove")

    monkeypatch.setattr(canary.shutil, "rmtree", forbidden)


def _wait_for(predicate, *, timeout: float = 30.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _fake_launcher(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


def _make_marker_only_holder(repo: Path, *, live: bool) -> Path:
    """owner 情報だけを確立した holder (worktree 作成前の creating 状態)。live=False なら owner を終了させる。"""
    root = repo / ".claude" / "worktrees"
    root.mkdir(parents=True, exist_ok=True)
    holder = Path(tempfile.mkdtemp(prefix=HOLDER_PREFIX, dir=str(root)))
    canary._establish_owner(holder, holder.name[len(HOLDER_PREFIX):])
    if not live:
        canary._release_owner_lock(holder)
    return holder


# ---------------------------------------------------------------------------
# AC1
# ---------------------------------------------------------------------------
def test_gc_reclaims_dead_owner_holder_and_branch(tmp_path):
    """GIVEN 終了済み owner (実 subprocess が prepare して終了) の holder + worktree 登録 + branch
    WHEN GC を実行する
    THEN worktree 登録 / holder / marker / 対応 branch が消え、他の branch は変わらない。再実行は何も変えない
    """
    repo = _make_repo(tmp_path)
    _git_ok("branch", "worktree-issue-1-keep", cwd=repo)
    target = _prepare_in_dead_child(repo)
    holder, branch = target.parent, _branch_of(target)
    assert (holder / canary.DISPOSABLE_OWNER_MARKER_NAME).is_file() and branch in _branches(repo)
    assert str(Path(os.path.realpath(target))) in _porcelain(repo)
    # canary 自身の fixture file (想定内の untracked) があっても回収を妨げない。
    fixture = target / canary.CANONICAL_WORKFLOW_FIXTURE_BODY_RELPATH
    fixture.parent.mkdir(parents=True)
    fixture.write_text("fixture\n", encoding="utf-8")

    report = canary.gc_disposable_worktrees(repo)

    assert _entry(report, holder.name) == {"holder": holder.name, "action": "reclaimed", "reason": None}
    assert report["outcome"] == "complete" and not report["truncated"]
    assert not holder.exists() and not os.path.lexists(target)
    assert branch not in _branches(repo) and _branches(repo) == {"main", "worktree-issue-1-keep"}
    assert str(Path(os.path.realpath(target))) not in _porcelain(repo)

    before = _snapshot(repo)
    again = canary.gc_disposable_worktrees(repo)
    assert again["candidates"] == [] and again["outcome"] == "complete"
    assert _snapshot(repo) == before

    # Git の remove 成功後に marker だけが残った holder (+ 未 checkout の branch) は marker 除去 + rmdir まで進む。
    residue = _make_marker_only_holder(repo, live=False)
    residue_branch = BRANCH_PREFIX + residue.name[len(HOLDER_PREFIX):]
    _git_ok("branch", residue_branch, cwd=repo)
    assert canary.gc_disposable_worktrees(repo)["outcome"] == "complete"
    assert not residue.exists() and residue_branch not in _branches(repo)


# ---------------------------------------------------------------------------
# AC2
# ---------------------------------------------------------------------------
def _build_foreign_state(repo: Path, tmp_path: Path) -> list[str]:
    """canary の対象ではない資源。GC 後も登録を含めて不変でなければならない。返り値は hold 理由つきで出るべき名前。"""
    root = repo / ".claude" / "worktrees"
    root.mkdir(parents=True, exist_ok=True)
    # prefix が近い / 別 fixture 番号 / 長さの違う suffix の branch
    for name in (
        "worktree-issue-1-keep",
        f"{BRANCH_PREFIX}abcdefg",
        f"{BRANCH_PREFIX}abcdefghi",
        f"{BRANCH_PREFIX}abc-defg",
        "worktree-issue-2147483645-canary-abcdefgh",
        "canary-canonical-workflow-branch",
    ):
        _git_ok("branch", name, cwd=repo)
    # 名前が近い holder (実 worktree + 死んだ marker 風 file を持つ)
    for index, near in enumerate(
        (
            "canary-canonical-workflow-abcdefghi",
            "canary-canonical-workflow-abcdefg",
            "canary-canonical-workflows-abcdefgh",
            "xcanary-canonical-workflow-abcdefgh",
        )
    ):
        (root / near).mkdir()
        (root / near / canary.DISPOSABLE_OWNER_MARKER_NAME).write_text("{}", encoding="utf-8")
        _git_ok("worktree", "add", "-q", "-b", f"near-miss-{index}", str(root / near / "wt"), "HEAD", cwd=repo)
    # symlink の holder (canary 形式の名前、対応 branch あり)
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep\n", encoding="utf-8")
    os.symlink(outside, root / f"{HOLDER_PREFIX}linkaaaa")
    _git_ok("branch", f"{BRANCH_PREFIX}linkaaaa", cwd=repo)
    # 移動・欠落した foreign 登録 (global prune なら消えるもの)
    _git_ok("worktree", "add", "-q", "-b", "foreign-moved", str(tmp_path / "foreign-moved"), "HEAD", cwd=repo)
    os.rename(tmp_path / "foreign-moved", tmp_path / "foreign-moved-away")
    _git_ok("worktree", "add", "-q", "-b", "foreign-gone", str(root / "foreign-gone"), "HEAD", cwd=repo)
    os.rename(root / "foreign-gone", tmp_path / "foreign-gone-away")
    # canary 形式の path だが holder が消え、branch が foreign の登録 (所有を確定できない = 曖昧)
    ambiguous = root / f"{HOLDER_PREFIX}ambig001"
    ambiguous.mkdir()
    _git_ok("worktree", "add", "-q", "-b", "foreign-in-canary-path", str(ambiguous / "wt"), "HEAD", cwd=repo)
    os.rename(ambiguous / "wt", tmp_path / "ambiguous-wt-away")
    ambiguous.rmdir()
    return [f"{HOLDER_PREFIX}linkaaaa", f"{HOLDER_PREFIX}ambig001"]


def test_gc_leaves_foreign_registrations_unchanged(tmp_path):
    """GIVEN foreign な branch / worktree / symlink holder / 移動・欠落した foreign 登録 / 近い名前の資源
    WHEN GC を (legacy opt-in 付きでも) 実行する
    THEN 登録を含めて一切変わらず、symlink holder と曖昧な登録は理由付き hold になる。
         同時に存在する本物の残骸だけが回収される (GC は有効に動いている)
    """
    repo = _make_repo(tmp_path)
    holds = _build_foreign_state(repo, tmp_path)
    assert "prunable" in _porcelain(repo)  # 実 Git で「global prune なら消える」登録が存在する
    before = _snapshot(repo)

    report = canary.gc_disposable_worktrees(repo, allow_legacy=True, legacy_grace_seconds=0.0)

    assert _snapshot(repo) == before
    assert _entry(report, holds[0])["reason"] == "holder_is_symlink"
    assert _entry(report, holds[1])["reason"] == "ambiguous_registration"
    assert {c["holder"] for c in report["candidates"]} == set(holds)
    assert report["outcome"] == "partial"
    assert (tmp_path / "outside-dir" / "precious.txt").read_text(encoding="utf-8") == "keep\n"

    # 本物の残骸を足しても、回収されるのはそれだけで foreign 側は変わらない。
    target = _prepare_in_dead_child(repo)
    suffix = _suffix(target)
    expected_foreign = _snapshot(repo, exclude=(suffix,))
    report = canary.gc_disposable_worktrees(repo, allow_legacy=True, legacy_grace_seconds=0.0)
    assert _entry(report, target.parent.name)["action"] == "reclaimed"
    assert not target.parent.exists() and _branch_of(target) not in _branches(repo)
    assert _snapshot(repo, exclude=(suffix,)) == expected_foreign


# ---------------------------------------------------------------------------
# AC3
# ---------------------------------------------------------------------------
def _lock_before_remove(args, cwd, _timeout, real):
    if args[:2] == ["worktree", "remove"]:
        real(["worktree", "lock", args[-1]], cwd=cwd)  # 判定後に別主体が lock を掛けた状態を実 Git で作る
    return None


def test_gc_remove_rejected_never_falls_back_to_rmtree(tmp_path, monkeypatch):
    """GIVEN `git worktree remove` が拒否 (locked) / timeout する対象
    WHEN GC と `_remove_disposable_worktree` が回収を試みる
    THEN Python の再帰削除に進まず、作業ディレクトリ・登録・branch・marker が残り、理由付き hold になる
    """
    repo = _make_repo(tmp_path)
    foreign_branch = "worktree-issue-1-keep"
    _git_ok("branch", foreign_branch, cwd=repo)
    target = _prepare_in_dead_child(repo)
    work_file = target / canary.CANONICAL_WORKFLOW_FIXTURE_BODY_RELPATH
    work_file.parent.mkdir(parents=True)
    work_file.write_text("work\n", encoding="utf-8")
    holder, branch = target.parent, _branch_of(target)

    # (a) GC: 判定後に lock された -> Git が remove を拒否 (exit 128)
    with monkeypatch.context() as patch:
        _wrap_git(patch, _lock_before_remove)
        _no_rmtree(patch)
        report = canary.gc_disposable_worktrees(repo)
    entry = _entry(report, holder.name)
    assert entry["action"] == "hold" and entry["reason"] == "worktree_remove_rejected"
    assert report["outcome"] == "partial"
    assert work_file.read_text(encoding="utf-8") == "work\n"
    assert "locked" in _porcelain(repo) and str(Path(os.path.realpath(target))) in _porcelain(repo)
    assert branch in _branches(repo) and foreign_branch in _branches(repo)
    assert (holder / canary.DISPOSABLE_OWNER_MARKER_NAME).is_file()  # 次回 GC のため owner 情報を残す

    # (b) `finally` 経路 (_remove_disposable_worktree): locked worktree は Python に迂回削除されない
    with monkeypatch.context() as patch:
        _no_rmtree(patch)
        canary._remove_disposable_worktree(repo, target)
    assert work_file.read_text(encoding="utf-8") == "work\n" and branch in _branches(repo)
    assert str(Path(os.path.realpath(target))) in _porcelain(repo)

    # (c) 自動 GC 相当の再実行: locked は理由付き hold のまま、状態は変わらない
    before = _snapshot(repo)
    report = canary.gc_disposable_worktrees(repo)
    assert _entry(report, holder.name)["reason"] == "worktree_locked"
    assert _snapshot(repo) == before

    # (d) 解錠すれば次回の GC が続きから回収する
    _git_ok("worktree", "unlock", str(target), cwd=repo)
    assert _entry(canary.gc_disposable_worktrees(repo), holder.name)["action"] == "reclaimed"
    assert not holder.exists() and branch not in _branches(repo)

    # (e) remove の timeout: 状態を再取得して hold。迂回削除しない
    target2 = _prepare_in_dead_child(repo)
    (target2 / "x.txt").write_text("x\n", encoding="utf-8")

    def timeout_on_remove(args, _cwd, timeout, _real):
        if args[:2] == ["worktree", "remove"]:
            raise subprocess.TimeoutExpired(["git", *args], timeout)
        return None

    with monkeypatch.context() as patch:
        _wrap_git(patch, timeout_on_remove)
        _no_rmtree(patch)
        # x.txt は想定外の untracked なので GC は元々 hold する。timeout 経路は self cleanup で確認する。
        canary._remove_disposable_worktree(repo, target2)
    assert (target2 / "x.txt").read_text(encoding="utf-8") == "x\n"
    assert str(Path(os.path.realpath(target2))) in _porcelain(repo) and _branch_of(target2) in _branches(repo)
    assert (target2.parent / canary.DISPOSABLE_OWNER_MARKER_NAME).is_file()

    # (f) 保護判定を通った (想定内の fixture file だけの) worktree でも、remove が timeout すれば迂回削除せず hold。
    target3 = _prepare_in_dead_child(repo)
    fixture3 = target3 / canary.CANONICAL_WORKFLOW_FIXTURE_BODY_RELPATH
    fixture3.parent.mkdir(parents=True)
    fixture3.write_text("fixture\n", encoding="utf-8")
    with monkeypatch.context() as patch:
        _wrap_git(patch, timeout_on_remove)
        _no_rmtree(patch)
        canary._remove_disposable_worktree(repo, target3)
    assert fixture3.read_text(encoding="utf-8") == "fixture\n"
    assert str(Path(os.path.realpath(target3))) in _porcelain(repo) and _branch_of(target3) in _branches(repo)
    assert (target3.parent / canary.DISPOSABLE_OWNER_MARKER_NAME).is_file()


# ---------------------------------------------------------------------------
# AC4
# ---------------------------------------------------------------------------
def test_gc_protects_live_creating_indeterminate_owner_and_branch(tmp_path):
    """GIVEN live (親 SIGKILL 後も子が生存) / creating (git worktree add が hook で進行中) /
            indeterminate (marker 判定不能) な owner、および owner が live で branch が未 checkout の holder
    WHEN GC を実行する
    THEN holder も対応 branch も削除されない。owner が終了すれば次の GC で回収される
    """
    repo = _make_repo(tmp_path)
    root = repo / ".claude" / "worktrees"

    # --- live: 実 launch 経路 (_run_canonical_workflow_side -> pass_fds で launcher に lock が継承される)。
    #     親 (side runner) を SIGKILL しても launcher が生きている間は使用終了と断定されない。
    pid_file = tmp_path / "launcher.pid"
    launcher = _fake_launcher(
        tmp_path / "launcher.py",
        "import os, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(120)\n",
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", _CHILD_RUN_SIDE, str(CANARY_PY), str(repo), str(launcher)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    launcher_pid = None
    try:
        assert _wait_for(pid_file.exists), "launcher did not start"
        assert _wait_for(lambda: pid_file.read_text(encoding="utf-8").strip() != "")
        launcher_pid = int(pid_file.read_text(encoding="utf-8"))
        parent.send_signal(signal.SIGKILL)
        parent.wait(timeout=30)
        os.kill(launcher_pid, 0)  # 親が死んでも launcher は生存している
        live_holders = [p for p in root.iterdir() if p.name.startswith(HOLDER_PREFIX)]
        assert len(live_holders) == 1
        live_holder = live_holders[0]
        live_branch = BRANCH_PREFIX + live_holder.name[len(HOLDER_PREFIX):]
        before = _snapshot(repo)

        report = canary.gc_disposable_worktrees(repo, allow_legacy=True, legacy_grace_seconds=0.0)
        entry = _entry(report, live_holder.name)
        assert entry["action"] == "hold" and entry["reason"] == "owner_live"
        assert _snapshot(repo) == before and live_branch in _branches(repo)
        assert (live_holder / "wt").is_dir()
    finally:
        if launcher_pid is not None:
            try:
                os.kill(launcher_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=30)
    # launcher が終了して初めて owner が終了済みになり、回収される
    assert _wait_for(
        lambda: _entry(canary.gc_disposable_worktrees(repo, dry_run=True), live_holder.name)["reason"] != "owner_live"
    )
    assert _entry(canary.gc_disposable_worktrees(repo), live_holder.name)["action"] == "reclaimed"
    assert not live_holder.exists() and live_branch not in _branches(repo)

    # --- creating: git worktree add の post-checkout hook が進行中 (branch は作成済み)。
    started, release = tmp_path / "hook-started", tmp_path / "hook-release"
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(
        "#!/bin/sh\n"
        f"touch {started}\n"
        "i=0\n"
        f"while [ ! -e {release} ] && [ $i -lt 600 ]; do sleep 0.1; i=$((i+1)); done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    creating = subprocess.Popen(
        [sys.executable, "-c", _CHILD_PREPARE, str(CANARY_PY), str(repo)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert _wait_for(started.exists), "post-checkout hook did not start"
        holders = [p for p in root.iterdir() if p.name.startswith(HOLDER_PREFIX)]
        assert len(holders) == 1
        creating_holder = holders[0]
        creating_branch = BRANCH_PREFIX + creating_holder.name[len(HOLDER_PREFIX):]
        assert creating_branch in _branches(repo)
        report = canary.gc_disposable_worktrees(repo, allow_legacy=True, legacy_grace_seconds=0.0)
        assert _entry(report, creating_holder.name)["reason"] == "owner_live"
        assert creating_holder.is_dir() and creating_branch in _branches(repo)
    finally:
        release.write_text("go", encoding="utf-8")
        out, _err = creating.communicate(timeout=60)
    assert creating.returncode == 0 and out.strip().startswith(str(root))
    hook.unlink()
    assert _entry(canary.gc_disposable_worktrees(repo), creating_holder.name)["action"] == "reclaimed"

    # --- owner が live で branch が未 checkout (worktree add の途中状態): holder も branch も保護される
    holder = _make_marker_only_holder(repo, live=True)
    branch = BRANCH_PREFIX + holder.name[len(HOLDER_PREFIX):]
    _git_ok("branch", branch, cwd=repo)
    before = _snapshot(repo)
    report = canary.gc_disposable_worktrees(repo, allow_legacy=True, legacy_grace_seconds=0.0)
    assert _entry(report, holder.name)["reason"] == "owner_live"
    assert _snapshot(repo) == before and branch in _branches(repo)
    canary._release_owner_lock(holder)
    assert _entry(canary.gc_disposable_worktrees(repo), holder.name)["action"] == "reclaimed"
    assert not holder.exists() and branch not in _branches(repo)

    # --- indeterminate: marker が判定不能 (内容不正 / suffix 不一致 / symlink) な holder と branch は保護される
    cases = {}
    for label, writer in {
        "garbage": lambda marker, suffix: marker.write_text("not json", encoding="utf-8"),
        "wrong_suffix": lambda marker, suffix: marker.write_text(
            json.dumps({"marker_version": 1, "suffix": "zzzzzzzz", "pid": 1}), encoding="utf-8"
        ),
        "empty": lambda marker, suffix: marker.write_text("", encoding="utf-8"),
        "symlink": lambda marker, suffix: os.symlink(tmp_path / "elsewhere", marker),
    }.items():
        indeterminate = Path(tempfile.mkdtemp(prefix=HOLDER_PREFIX, dir=str(root)))
        suffix = indeterminate.name[len(HOLDER_PREFIX):]
        writer(indeterminate / canary.DISPOSABLE_OWNER_MARKER_NAME, suffix)
        _git_ok("branch", BRANCH_PREFIX + suffix, cwd=repo)
        cases[label] = (indeterminate, BRANCH_PREFIX + suffix)
    before = _snapshot(repo)
    report = canary.gc_disposable_worktrees(repo, allow_legacy=True, legacy_grace_seconds=0.0)
    for label, (indeterminate, indeterminate_branch) in cases.items():
        assert _entry(report, indeterminate.name)["reason"] == "owner_indeterminate", label
        assert indeterminate.is_dir() and indeterminate_branch in _branches(repo), label
    assert _snapshot(repo) == before


# ---------------------------------------------------------------------------
# AC5
# ---------------------------------------------------------------------------
def test_gc_protects_foreign_branch_and_dirty_state(tmp_path):
    """GIVEN 終了済み owner の holder だが、foreign branch が checkout されている / 未コミット変更がある /
            lock されている worktree
    WHEN GC を実行する
    THEN 自動回収されず作業ファイルも対応 branch も失われない (理由付き hold)
    """
    repo = _make_repo(tmp_path)

    foreign = _prepare_in_dead_child(repo)
    _git_ok("switch", "-q", "-c", "foreign-work", cwd=foreign)
    (foreign / "notes.txt").write_text("precious foreign work\n", encoding="utf-8")

    modified = _prepare_in_dead_child(repo)
    (modified / "README.md").write_text("changed\n", encoding="utf-8")

    untracked = _prepare_in_dead_child(repo)
    (untracked / "scratch.txt").write_text("scratch\n", encoding="utf-8")

    staged = _prepare_in_dead_child(repo)
    (staged / "staged.txt").write_text("staged\n", encoding="utf-8")
    _git_ok("add", "staged.txt", cwd=staged)

    locked = _prepare_in_dead_child(repo)
    _git_ok("worktree", "lock", "--reason", "protected", str(locked), cwd=repo)

    expected = {
        foreign: "foreign_branch_checked_out",
        modified: "unexpected_working_state",
        untracked: "unexpected_working_state",
        staged: "unexpected_working_state",
        locked: "worktree_locked",
    }
    before = _snapshot(repo)
    report = canary.gc_disposable_worktrees(repo, allow_legacy=True, legacy_grace_seconds=0.0)
    for target, reason in expected.items():
        entry = _entry(report, target.parent.name)
        assert (entry["action"], entry["reason"]) == ("hold", reason), (target.name, entry)
        assert _branch_of(target) in _branches(repo)
        assert str(Path(os.path.realpath(target))) in _porcelain(repo)
    assert report["outcome"] == "partial"
    assert (foreign / "notes.txt").read_text(encoding="utf-8") == "precious foreign work\n"
    assert (modified / "README.md").read_text(encoding="utf-8") == "changed\n"
    assert (untracked / "scratch.txt").read_text(encoding="utf-8") == "scratch\n"
    assert _snapshot(repo) == before
    assert _git_ok("symbolic-ref", "--short", "HEAD", cwd=foreign) == "foreign-work"

    # `finally` 経路でも、foreign branch が checkout された worktree の作業ファイルを失わせない。
    canary._remove_disposable_worktree(repo, foreign)
    assert (foreign / "notes.txt").read_text(encoding="utf-8") == "precious foreign work\n"


# ---------------------------------------------------------------------------
# AC6
# ---------------------------------------------------------------------------
def _make_legacy_holder(repo: Path, suffix: str, *, age_seconds: float) -> Path:
    """owner 情報のない既存残骸 (旧実装が作った形)。"""
    root = repo / ".claude" / "worktrees"
    root.mkdir(parents=True, exist_ok=True)
    holder = root / f"{HOLDER_PREFIX}{suffix}"
    holder.mkdir()
    _git_ok("worktree", "add", "-q", "-b", f"{BRANCH_PREFIX}{suffix}", str(holder / "wt"), "HEAD", cwd=repo)
    old = time.time() - age_seconds
    os.utime(holder, (old, old))
    return holder / "wt"


def test_gc_legacy_ttl_expiry_alone_holds(tmp_path):
    """GIVEN owner 情報のない legacy holder (TTL / mtime が大きく失効)
    WHEN 自動 GC 相当 (opt-in なし) で実行する
    THEN 理由付き hold で何も変わらない。explicit opt-in でも Git state が安全でなければ hold、
         猶予内の legacy も hold。opt-in かつ安全 (unlocked・想定 branch・clean) で猶予を過ぎたものだけ回収する
    """
    repo = _make_repo(tmp_path)
    year = 365 * 24 * 3600.0
    old_safe = _make_legacy_holder(repo, "oldsafe1", age_seconds=year)
    recent = _make_legacy_holder(repo, "recent01", age_seconds=60.0)
    old_dirty = _make_legacy_holder(repo, "olddirty", age_seconds=year)
    (old_dirty / "work.txt").write_text("work\n", encoding="utf-8")
    os.utime(old_dirty.parent, (time.time() - year,) * 2)
    old_locked = _make_legacy_holder(repo, "oldlockd", age_seconds=year)
    _git_ok("worktree", "lock", str(old_locked), cwd=repo)
    old_detached = _make_legacy_holder(repo, "olddetch", age_seconds=year)
    _git_ok("switch", "-q", "--detach", cwd=old_detached)
    os.utime(old_detached.parent, (time.time() - year,) * 2)
    before = _snapshot(repo)

    # 自動 GC (opt-in なし): TTL が大きく失効していても全て hold
    report = canary.gc_disposable_worktrees(repo, max_candidates=10, time_budget_seconds=60.0)
    assert {c["holder"]: (c["action"], c["reason"]) for c in report["candidates"]} == {
        f"{HOLDER_PREFIX}oldsafe1": ("hold", "legacy_owner_unknown"),
        f"{HOLDER_PREFIX}recent01": ("hold", "legacy_within_grace"),
        f"{HOLDER_PREFIX}olddirty": ("hold", "legacy_owner_unknown"),
        f"{HOLDER_PREFIX}oldlockd": ("hold", "legacy_owner_unknown"),
        f"{HOLDER_PREFIX}olddetch": ("hold", "legacy_owner_unknown"),
    }
    assert report["outcome"] == "partial" and _snapshot(repo) == before

    # explicit + opt-in: 安全なものだけ
    report = canary.gc_disposable_worktrees(repo, allow_legacy=True)
    actions = {c["holder"]: (c["action"], c["reason"]) for c in report["candidates"]}
    assert actions == {
        f"{HOLDER_PREFIX}oldsafe1": ("reclaimed", None),
        f"{HOLDER_PREFIX}recent01": ("hold", "legacy_within_grace"),
        f"{HOLDER_PREFIX}olddirty": ("hold", "unexpected_working_state"),
        f"{HOLDER_PREFIX}oldlockd": ("hold", "worktree_locked"),
        f"{HOLDER_PREFIX}olddetch": ("hold", "head_not_expected_branch"),
    }
    assert not old_safe.parent.exists() and f"{BRANCH_PREFIX}oldsafe1" not in _branches(repo)
    for kept in (recent, old_dirty, old_locked, old_detached):
        assert kept.is_dir()
    assert (old_dirty / "work.txt").read_text(encoding="utf-8") == "work\n"
    assert {f"{BRANCH_PREFIX}{s}" for s in ("recent01", "olddirty", "oldlockd")} <= _branches(repo)


# ---------------------------------------------------------------------------
# AC7
# ---------------------------------------------------------------------------
def _install_failing_post_checkout(repo: Path) -> Path:
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    return hook


def test_gc_recovers_registration_without_holder(tmp_path, monkeypatch):
    """GIVEN post-checkout hook の失敗で holder が消え Git 登録だけ残った状態 (+ foreign / 曖昧な登録)
    WHEN GC を実行する
    THEN 所有を確定できる登録だけが対象 path 指定の remove で回収され、他は不変。
         GC が途中で中断されても再実行で続きから回収され、無関係な状態は変わらない
    """
    repo = _make_repo(tmp_path)
    root = repo / ".claude" / "worktrees"
    hook = _install_failing_post_checkout(repo)

    # 是正した `_prepare_disposable_worktree` の失敗経路: 登録 / branch / holder が残らない (無条件 rmtree なし)
    target, reason = canary._prepare_disposable_worktree(repo)
    assert target is None and reason == "disposable_worktree_add_failed"
    assert _branches(repo) == {"main"} and _porcelain(repo).count("worktree ") == 1
    assert not [p for p in root.iterdir() if p.name.startswith(HOLDER_PREFIX)]

    # 旧実装の失敗経路 (holder を rmtree して戻る) が作る「holder は無いが登録と branch だけ残る」状態を実 Git で作る。
    orphan_suffix = "orphan01"
    orphan_holder = root / f"{HOLDER_PREFIX}{orphan_suffix}"
    orphan_holder.mkdir()
    orphan_branch = f"{BRANCH_PREFIX}{orphan_suffix}"
    rc = _git_rc("worktree", "add", "-b", orphan_branch, str(orphan_holder / "wt"), "HEAD", cwd=repo)
    assert rc != 0  # hook 失敗で非ゼロ終了
    shutil.rmtree(orphan_holder)  # テスト側で旧実装相当の欠落状態を作るだけ (canary 側の処理ではない)
    assert "prunable" in _porcelain(repo) and f"{BRANCH_PREFIX}{orphan_suffix}" in _branches(repo)
    hook.unlink()

    foreign_state = _build_foreign_state(repo, tmp_path)
    # 曖昧な登録: canary の path だが branch が foreign / branch は canary 形だが path が holder 外
    _git_ok("branch", f"{BRANCH_PREFIX}outside1", cwd=repo)
    _git_ok("worktree", "add", "-q", str(tmp_path / "outside-wt"), f"{BRANCH_PREFIX}outside1", cwd=repo)
    before_foreign = _snapshot(repo, exclude=(orphan_suffix,))

    report = canary.gc_disposable_worktrees(repo)
    assert _entry(report, f"{HOLDER_PREFIX}{orphan_suffix}") == {
        "holder": f"{HOLDER_PREFIX}{orphan_suffix}", "action": "reclaimed", "reason": None,
    }
    assert {c["holder"] for c in report["candidates"]} == {f"{HOLDER_PREFIX}{orphan_suffix}", *foreign_state}
    assert f"{BRANCH_PREFIX}{orphan_suffix}" not in _branches(repo)
    assert str(root / f"{HOLDER_PREFIX}{orphan_suffix}") not in _porcelain(repo)
    assert _snapshot(repo, exclude=(orphan_suffix,)) == before_foreign  # foreign / 曖昧な登録は不変

    # --- GC 途中中断後の再実行: worktree remove 後・branch 削除前に中断しても、次回に続きから回収する
    dead = _prepare_in_dead_child(repo)
    dead_branch = _branch_of(dead)
    before_unrelated = _snapshot(repo, exclude=(_suffix(dead),))

    def interrupt_on_branch_delete(args, _cwd, _timeout, _real):
        if args[:2] == ["branch", "-D"]:
            raise KeyboardInterrupt
        return None

    with monkeypatch.context() as patch:
        _wrap_git(patch, interrupt_on_branch_delete)
        with pytest.raises(KeyboardInterrupt):
            canary.gc_disposable_worktrees(repo)
    assert not os.path.lexists(dead) and dead_branch in _branches(repo)  # worktree は除去済み
    assert (dead.parent / canary.DISPOSABLE_OWNER_MARKER_NAME).is_file()  # owner 情報は branch 削除まで残る
    assert str(Path(os.path.realpath(dead))) not in _porcelain(repo)

    report = canary.gc_disposable_worktrees(repo)
    assert _entry(report, dead.parent.name)["action"] == "reclaimed"
    assert not dead.parent.exists() and dead_branch not in _branches(repo)
    assert _snapshot(repo, exclude=(_suffix(dead),)) == before_unrelated

    # branch 削除が失敗した場合 (差し込み) は hold + marker 保持。再実行は冪等に続きを回収する。
    dead2 = _prepare_in_dead_child(repo)
    from subprocess import CompletedProcess

    def fail_branch_delete(args, cwd, _timeout, _real):
        if args[:2] == ["branch", "-D"]:
            return CompletedProcess(["git", *args], 1, "", "injected failure")
        return None

    with monkeypatch.context() as patch:
        _wrap_git(patch, fail_branch_delete)
        report = canary.gc_disposable_worktrees(repo)
    assert _entry(report, dead2.parent.name)["reason"] == "branch_delete_failed"
    assert (dead2.parent / canary.DISPOSABLE_OWNER_MARKER_NAME).is_file() and _branch_of(dead2) in _branches(repo)
    assert _entry(canary.gc_disposable_worktrees(repo), dead2.parent.name)["action"] == "reclaimed"
    assert not dead2.parent.exists() and _branch_of(dead2) not in _branches(repo)


# ---------------------------------------------------------------------------
# AC8 (runtime verification: process I/O)
# ---------------------------------------------------------------------------
def _run_cli(*cli_args: str, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CANARY_PY), *cli_args],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120, check=False,
    )


def test_cli_gc_dry_run_changes_nothing(tmp_path):
    """GIVEN 終了済み owner の残骸 / legacy holder / foreign 資源がある tmp git repo
    WHEN explicit GC の CLI を実 subprocess として `--dry-run` で起動する (`--mode` なし)
    THEN exit code と stdout の候補一覧だけが得られ、refs・worktree 登録・filesystem (marker / evidence を含む)
         は不変で、Claude / gh は起動されない。完全成功と部分失敗は exit code で区別できる
    """
    repo = _make_repo(tmp_path)
    dead = _prepare_in_dead_child(repo)
    legacy = _make_legacy_holder(repo, "legacyaa", age_seconds=365 * 24 * 3600.0)
    _git_ok("branch", "worktree-issue-1-keep", cwd=repo)

    # Claude / gh / proxy が起動されたら記録される shim。dry-run は git 以外を一切起動しない。
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    spawn_log = tmp_path / "spawned.log"
    for name in ("claude", "claude-gpt", "gh", "launch.sh"):
        shim = shim_dir / name
        shim.write_text(f"#!/bin/sh\necho {name} >> {spawn_log}\nexit 0\n", encoding="utf-8")
        shim.chmod(0o755)
    env = {**os.environ, "PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    evidence_dir = SCRIPT_DIR / ".evidence"
    evidence_before = sorted(p.name for p in evidence_dir.iterdir()) if evidence_dir.exists() else None
    before = _snapshot(repo, with_mtime=True)

    result = _run_cli(
        "--gc-disposable-worktrees", "--dry-run", "--canonical-workflow-worktree", str(repo), cwd=tmp_path, env=env
    )

    assert result.returncode == canary.EXIT_GC_PARTIAL, (result.stdout, result.stderr)  # legacy が hold = 部分
    report = json.loads(result.stdout)["gc_disposable_worktrees"]
    assert report["dry_run"] is True and report["outcome"] == "partial"
    assert {c["holder"]: (c["action"], c["reason"]) for c in report["candidates"]} == {
        dead.parent.name: ("would_reclaim", None),
        legacy.parent.name: ("hold", "legacy_owner_unknown"),
    }
    assert str(tmp_path) not in result.stdout  # 絶対 path (HOME 等) を出力しない
    assert _snapshot(repo, with_mtime=True) == before
    assert (dead.parent / canary.DISPOSABLE_OWNER_MARKER_NAME).is_file() and _branch_of(dead) in _branches(repo)
    assert not spawn_log.exists()
    evidence_after = sorted(p.name for p in evidence_dir.iterdir()) if evidence_dir.exists() else None
    assert evidence_after == evidence_before

    # 部分失敗が無ければ完全成功 (exit 0)。dry-run でも変更しない。
    legacy_branch = f"{BRANCH_PREFIX}legacyaa"
    _git_ok("worktree", "remove", "--force", str(legacy), cwd=repo)
    _git_ok("branch", "-D", legacy_branch, cwd=repo)
    legacy.parent.rmdir()
    before = _snapshot(repo, with_mtime=True)
    result = _run_cli(
        "--gc-disposable-worktrees", "--dry-run", "--canonical-workflow-worktree", str(repo), cwd=tmp_path, env=env
    )
    assert result.returncode == canary.EXIT_OK, (result.stdout, result.stderr)
    assert json.loads(result.stdout)["gc_disposable_worktrees"]["outcome"] == "complete"
    assert _snapshot(repo, with_mtime=True) == before

    # 同じ CLI を dry-run なしで起動すると実際に回収する (exit 0)
    result = _run_cli("--gc-disposable-worktrees", "--canonical-workflow-worktree", str(repo), cwd=tmp_path, env=env)
    assert result.returncode == canary.EXIT_OK, (result.stdout, result.stderr)
    assert not dead.parent.exists() and _branch_of(dead) not in _branches(repo)
    assert not spawn_log.exists()

    # --dry-run は GC flag なしでは不正な起動。--mode と GC は同時に指定できない。
    assert _run_cli("--dry-run", cwd=tmp_path, env=env).returncode == canary.EXIT_INVALID_INVOCATION
    assert _run_cli("--mode", "agy", "--gc-disposable-worktrees", cwd=tmp_path, env=env).returncode == (
        canary.EXIT_INVALID_INVOCATION
    )
    assert _run_cli(cwd=tmp_path, env=env).returncode == canary.EXIT_INVALID_INVOCATION  # --mode 必須は維持


# ---------------------------------------------------------------------------
# AC9
# ---------------------------------------------------------------------------
def test_gc_candidate_failure_does_not_stop_others_or_canary(tmp_path, monkeypatch):
    """GIVEN 複数の回収候補があり、うち 1 件の回収が例外 / timeout になる
    WHEN GC と canary の両 mode (準備前の自動 GC / `finally` の cleanup) を実行する
    THEN 他 candidate は回収され、canary 本来の結果は cleanup / 自動 GC の失敗・競合で覆らない。
         自動 GC は件数と総時間で bounded、lock 競合は待たない
    """
    repo = _make_repo(tmp_path)

    # --- 1 件の例外 / timeout が他 candidate を止めない
    first = _prepare_in_dead_child(repo)
    second = _prepare_in_dead_child(repo)
    victim, healthy = sorted((first, second), key=lambda t: t.parent.name)
    victim_branch = _branch_of(victim)

    def boom_on_victim(args, _cwd, timeout, _real):
        if args[:2] == ["worktree", "remove"] and str(victim.parent.name) in args[-1]:
            raise RuntimeError("injected failure")
        return None

    with monkeypatch.context() as patch:
        _wrap_git(patch, boom_on_victim)
        report = canary.gc_disposable_worktrees(repo)
    assert _entry(report, victim.parent.name) == {
        "holder": victim.parent.name, "action": "failed", "reason": "exception:RuntimeError",
    }
    assert _entry(report, healthy.parent.name)["action"] == "reclaimed"
    assert report["outcome"] == "partial"
    assert victim.is_dir() and victim_branch in _branches(repo) and not healthy.parent.exists()

    def timeout_on_victim(args, _cwd, timeout, _real):
        if args[:2] == ["worktree", "remove"] and victim.parent.name in args[-1]:
            raise subprocess.TimeoutExpired(["git", *args], timeout)
        return None

    third = _prepare_in_dead_child(repo)
    with monkeypatch.context() as patch:
        _wrap_git(patch, timeout_on_victim)
        _no_rmtree(patch)
        report = canary.gc_disposable_worktrees(repo)
    assert _entry(report, victim.parent.name)["reason"] == "worktree_remove_timeout"
    assert _entry(report, third.parent.name)["action"] == "reclaimed"
    assert victim.is_dir() and not third.parent.exists()
    assert _entry(canary.gc_disposable_worktrees(repo), victim.parent.name)["action"] == "reclaimed"

    # --- 自動 GC は bounded: 件数上限 / 総時間上限で打ち切り、残りは次回に持ち越す
    dead = [_prepare_in_dead_child(repo) for _ in range(3)]
    report = canary.gc_disposable_worktrees(repo, max_candidates=1)
    assert [c["action"] for c in report["candidates"]].count("reclaimed") == 1
    assert [c["action"] for c in report["candidates"]].count("deferred") == 2
    assert report["truncated"] and report["outcome"] == "partial"
    # 総時間 budget が最初から尽きている場合は、deadline 後に新しい git query を 1 本も始めない
    # (#2906 P2-2: 1 秒の最小 timeout を復活させない)。候補の列挙にも git query が要るため、candidate は
    # 列挙されず、truncated / partial で何も回収せず次回に持ち越す。
    report = canary.gc_disposable_worktrees(repo, time_budget_seconds=0.0)
    assert report["truncated"] and report["outcome"] == "partial" and report["error"] == "gc_budget_exhausted"
    assert not [c for c in report["candidates"] if c["action"] == "reclaimed"]
    assert all(_branch_of(t) in _branches(repo) for t in dead if t.parent.exists())
    assert sum(1 for t in dead if t.parent.exists()) == 2  # max_candidates=1 の回収で 1 件だけが消えている
    assert canary.gc_disposable_worktrees(repo)["outcome"] == "complete"
    assert not any(t.parent.exists() for t in dead)

    # --- canary 本来の結果は、自動 GC の失敗 / 競合、cleanup の例外で覆らない (canonical-workflow side)
    launcher = _fake_launcher(tmp_path / "ok-launcher.py", "import sys\nsys.exit(0)\n")
    prompt = canary.canonical_workflow_prompt()
    contended = _make_marker_only_holder(repo, live=True)  # lock 競合 (live owner): 待たずに hold される
    contended_branch = BRANCH_PREFIX + contended.name[len(HOLDER_PREFIX):]
    _git_ok("branch", contended_branch, cwd=repo)

    started = time.monotonic()
    side, side_reason = canary._run_canonical_workflow_side(launcher, repo, prompt, timeout=60.0)
    assert side_reason is None and side["side_outcome"] == "unavailable"
    assert time.monotonic() - started < 30.0
    assert contended.is_dir() and contended_branch in _branches(repo)  # 競合した candidate は触られない

    def exploding_gc(*_args, **_kwargs):
        raise RuntimeError("gc exploded")

    def exploding_remove(*_args, **_kwargs):
        raise RuntimeError("cleanup exploded")

    with monkeypatch.context() as patch:
        patch.setattr(canary, "gc_disposable_worktrees", exploding_gc)
        patch.setattr(canary, "_remove_disposable_worktree", exploding_remove)
        side2, side_reason2 = canary._run_canonical_workflow_side(launcher, repo, prompt, timeout=60.0)
    assert side_reason2 is None and side2["side_outcome"] == "unavailable"
    assert side2["launcher_exit_code"] == side["launcher_exit_code"] == 0

    # --- classifier-semantics side も同様 (準備前の自動 GC と finally の cleanup)
    with monkeypatch.context() as patch:
        patch.setattr(canary, "CLAUDE_GPT_LAUNCHER", launcher)
        patch.setattr(canary, "gc_disposable_worktrees", exploding_gc)
        patch.setattr(canary, "_remove_disposable_worktree", exploding_remove)
        detail, reason = canary._run_classifier_semantics_case(repo, "positive", timeout=60.0)
    assert reason is None and detail["launcher_exit_code"] == 0

    # --- 準備前の自動 GC が実際に残骸を回収してから canary が続行する
    stale = _prepare_in_dead_child(repo)
    side3, side_reason3 = canary._run_canonical_workflow_side(launcher, repo, prompt, timeout=60.0)
    assert side_reason3 is None and side3["side_outcome"] == "unavailable"
    assert not stale.parent.exists() and _branch_of(stale) not in _branches(repo)
    canary._release_owner_lock(contended)


# ---------------------------------------------------------------------------
# fix_delta iteration 1 (OWNER REQUEST_CHANGES): 追加の回帰 test
# ---------------------------------------------------------------------------
def _holder_names(repo: Path) -> list[str]:
    root = repo / ".claude" / "worktrees"
    return sorted(p.name for p in root.iterdir() if p.name.startswith(HOLDER_PREFIX)) if root.exists() else []


def _make_dead_marker_holder(repo: Path, suffix: str) -> Path:
    """owner が終了済み (marker は有効だが lock 保持者なし) の holder + 対応 branch。worktree 登録は持たない。"""
    root = repo / ".claude" / "worktrees"
    root.mkdir(parents=True, exist_ok=True)
    holder = root / f"{HOLDER_PREFIX}{suffix}"
    holder.mkdir()
    canary._establish_owner(holder, suffix)
    canary._release_owner_lock(holder)
    _git_ok("branch", f"{BRANCH_PREFIX}{suffix}", cwd=repo)
    return holder


_DESCENDANT_LAUNCHER = """
import os, sys, time
if os.fork() == 0:
    # 子孫 (launcher timeout 後も生き残る): 継承した owner lock fd を保持したまま release file を待つ。
    devnull = os.open(os.devnull, os.O_RDWR)
    for stdio in (0, 1, 2):
        os.dup2(devnull, stdio)
    open({pid_file!r}, "w").write(str(os.getpid()))
    stop = time.time() + 90
    while not os.path.exists({release!r}) and time.time() < stop:
        time.sleep(0.05)
    os._exit(0)
{extra}
time.sleep(120)
"""


def _drive_side(mode: str, repo: Path, launcher: Path, monkeypatch, *, timeout: float):
    """canary の実 call site を駆動する。canonical-workflow は `_run_canonical_workflow_side`、
    classifier-semantics は `_run_classifier_semantics_case` (launcher は module 定数を差し替える)。
    どちらも `_owner_lock_pass_fds` の実 launcher 経路と `finally` の `_cleanup_disposable_worktree_safely` を通る。"""
    if mode == "canonical":
        return canary._run_canonical_workflow_side(
            launcher, repo, canary.canonical_workflow_prompt(), timeout=timeout
        )
    with monkeypatch.context() as patch:
        patch.setattr(canary, "CLAUDE_GPT_LAUNCHER", launcher)
        return canary._run_classifier_semantics_case(repo, "positive", timeout=timeout)


@pytest.mark.parametrize("mode", ["canonical", "classifier"])
def test_finally_cleanup_holds_while_descendant_owns_lock_after_launcher_timeout(tmp_path, monkeypatch, mode):
    """GIVEN launcher が子孫 (owner lock fd を継承) を残したまま timeout で kill される
    WHEN canary の両 mode の実 call site (`finally` の `_cleanup_disposable_worktree_safely`) が走る
    THEN 子孫が生きている間は holder / worktree / marker / branch が残る (timeout は owner 終了ではない)。
         子孫が終了すれば次の GC が回収する。通常の (git 管理外の) dirty file は self-cleanup で消えない。
    mutation 対象: `_remove_disposable_worktree` が保護判定なしで `git worktree remove --force` に進む退行
    """
    repo = _make_repo(tmp_path)
    pid_file, release = tmp_path / "descendant.pid", tmp_path / "descendant.release"
    launcher = _fake_launcher(
        tmp_path / "launcher-descendant.py",
        _DESCENDANT_LAUNCHER.format(pid_file=str(pid_file), release=str(release), extra=""),
    )
    descendant_pid = None
    try:
        detail, reason = _drive_side(mode, repo, launcher, monkeypatch, timeout=3.0)
        assert reason is None
        if mode == "canonical":
            assert detail["timed_out"] is True  # launcher timeout が実際に発火した
        else:
            assert detail["reason"] == "runtime_timeout"
        assert _wait_for(lambda: pid_file.exists() and pid_file.read_text(encoding="utf-8").strip() != "")
        descendant_pid = int(pid_file.read_text(encoding="utf-8"))
        os.kill(descendant_pid, 0)  # launcher は kill されたが子孫は生存している

        holders = _holder_names(repo)
        assert len(holders) == 1, holders
        holder = repo / ".claude" / "worktrees" / holders[0]
        branch = BRANCH_PREFIX + holders[0][len(HOLDER_PREFIX):]
        assert (holder / "wt").is_dir() and (holder / canary.DISPOSABLE_OWNER_MARKER_NAME).is_file()
        assert branch in _branches(repo) and str(Path(os.path.realpath(holder / "wt"))) in _porcelain(repo)
        report = canary.gc_disposable_worktrees(repo, dry_run=True)
        assert _entry(report, holders[0])["reason"] == "owner_live"
    finally:
        release.write_text("go", encoding="utf-8")
        if descendant_pid is not None:
            assert _wait_for(
                lambda: _entry(canary.gc_disposable_worktrees(repo, dry_run=True), holders[0])["reason"] != "owner_live"
            )
    # 子孫の終了後に、次の GC が回収する
    assert _entry(canary.gc_disposable_worktrees(repo), holders[0])["action"] == "reclaimed"
    assert not holder.exists() and branch not in _branches(repo)

    # 通常の dirty file (git 管理外・fixture 以外) は launcher 正常終了後の self-cleanup でも消えない。
    notes_launcher = _fake_launcher(
        tmp_path / "launcher-notes.py",
        "import sys\nopen('notes.txt', 'w').write('precious\\n')\nsys.exit(0)\n",
    )
    detail, reason = _drive_side(mode, repo, notes_launcher, monkeypatch, timeout=60.0)
    assert reason is None
    holders = _holder_names(repo)
    assert len(holders) == 1, holders
    holder = repo / ".claude" / "worktrees" / holders[0]
    branch = BRANCH_PREFIX + holders[0][len(HOLDER_PREFIX):]
    assert (holder / "wt" / "notes.txt").read_text(encoding="utf-8") == "precious\n"
    assert branch in _branches(repo) and (holder / canary.DISPOSABLE_OWNER_MARKER_NAME).is_file()
    held = _entry(canary.gc_disposable_worktrees(repo), holders[0])
    assert (held["action"], held["reason"]) == ("hold", "unexpected_working_state")
    (holder / "wt" / "notes.txt").unlink()
    assert _entry(canary.gc_disposable_worktrees(repo), holders[0])["action"] == "reclaimed"


def test_worktree_add_hook_inherits_owner_fd_when_parent_is_sigkilled(tmp_path):
    """GIVEN `git worktree add` の post-checkout hook が進行中 (実 hook が block)
    WHEN `_prepare_disposable_worktree` を実行している Python 親だけを SIGKILL する
    THEN git / hook が owner lock fd を継承しているため GC は owner_live で hold し、holder / branch は残る。
         hook が終われば owner は終了済みになり GC が回収できる
    mutation 対象: `git worktree add` へ owner fd を `pass_fds` で渡さない退行
    """
    repo = _make_repo(tmp_path)
    started, release, hook_pid_file = tmp_path / "hook-started", tmp_path / "hook-release", tmp_path / "hook.pid"
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(
        "#!/bin/sh\n"
        f"echo $$ > {hook_pid_file}\n"
        f"touch {started}\n"
        "i=0\n"
        f"while [ ! -e {release} ] && [ $i -lt 600 ]; do sleep 0.1; i=$((i+1)); done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    root = repo / ".claude" / "worktrees"
    parent = subprocess.Popen(
        [sys.executable, "-c", _CHILD_PREPARE, str(CANARY_PY), str(repo)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_for(started.exists), "post-checkout hook did not start"
        hook_pid = int(hook_pid_file.read_text(encoding="utf-8").strip())
        parent.send_signal(signal.SIGKILL)  # Python 親だけを kill (git / hook は生存)
        parent.wait(timeout=30)
        os.kill(hook_pid, 0)
        holders = [p for p in root.iterdir() if p.name.startswith(HOLDER_PREFIX)]
        assert len(holders) == 1
        holder = holders[0]
        branch = BRANCH_PREFIX + holder.name[len(HOLDER_PREFIX):]
        assert branch in _branches(repo)
        before = _snapshot(repo)
        report = canary.gc_disposable_worktrees(repo, allow_legacy=True, legacy_grace_seconds=0.0)
        entry = _entry(report, holder.name)
        assert (entry["action"], entry["reason"]) == ("hold", "owner_live")
        assert _snapshot(repo) == before and holder.is_dir() and branch in _branches(repo)
    finally:
        release.write_text("go", encoding="utf-8")
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=30)
    assert _wait_for(
        lambda: _entry(canary.gc_disposable_worktrees(repo, dry_run=True), holder.name)["reason"] != "owner_live"
    )
    hook.unlink()
    assert _entry(canary.gc_disposable_worktrees(repo), holder.name)["action"] == "reclaimed"
    assert not holder.exists() and branch not in _branches(repo)


def _patch_root_listdir_permission_error(monkeypatch, root: Path):
    real_listdir = os.listdir

    def listdir(path="."):
        if os.path.realpath(path) == str(root):
            raise PermissionError(13, "injected", str(path))
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", listdir)


def test_gc_candidate_discovery_permission_error_never_reclaims(tmp_path, monkeypatch):
    """GIVEN worktrees root の列挙だけが PermissionError になる (live owner の worktree + dead owner の残骸がある)
    WHEN explicit / dry-run / 自動 GC / canary call site を実行する
    THEN holder 不在とは見なさず何も回収しない (outcome は complete にならず理由が残る)。
         自動 GC / canary は raise しない。
         root 不在 (FileNotFoundError) だけが「holder 確認済みで不在」。registration-only の判定でも
         holder の再出現・判定不能は保護側に倒す
    mutation 対象: `_discover_disposable_candidates` が `except OSError: names = []` に戻る退行
    """
    repo = _make_repo(tmp_path)
    live_target, reason = canary._prepare_disposable_worktree(repo)  # この process が owner lock を保持 (live)
    assert live_target is not None and reason is None
    dead_target = _prepare_in_dead_child(repo)
    root = _root(repo)
    before = _snapshot(repo)
    try:
        with monkeypatch.context() as patch:
            _patch_root_listdir_permission_error(patch, root)
            for kwargs in ({}, {"dry_run": True}, {"allow_legacy": True, "legacy_grace_seconds": 0.0}):
                report = canary.gc_disposable_worktrees(repo, **kwargs)
                assert report["outcome"] == "failed" and report["candidates"] == [], report
                assert report["error"] == "candidate_discovery_indeterminate:PermissionError"
            canary._auto_gc_disposable_worktrees(repo)  # raise しない
            launcher = _fake_launcher(tmp_path / "ok-launcher.py", "import sys\nsys.exit(0)\n")
            side, side_reason = canary._run_canonical_workflow_side(
                launcher, repo, canary.canonical_workflow_prompt(), timeout=60.0
            )
            assert side_reason is None and side["launcher_exit_code"] == 0
        assert _snapshot(repo) == before  # live / dead どちらの holder・登録・branch も不変
        assert live_target.is_dir() and dead_target.is_dir()

        # root 不在 (FileNotFoundError) は「holder 確認済みで不在」: 列挙失敗とは区別される。
        ghost_regs: dict = {}
        assert canary._discover_disposable_candidates(root / "does-not-exist", ghost_regs) == {}

        # registration-only 判定: 判定時点で holder が存在する (再出現) なら回収しない。
        regs = canary._list_worktree_registrations(repo)
        registration = regs[os.path.realpath(live_target)]
        verdict, why, fd = canary._evaluate_disposable_candidate(
            root, _suffix(live_target), {"holder": False, "registration": registration}, regs,
            allow_legacy=False, legacy_grace_seconds=0.0, deadline=None,
        )
        assert (verdict, why, fd) == ("hold", "holder_reappeared", None)
    finally:
        canary._remove_disposable_worktree(repo, live_target)
    assert not live_target.parent.exists()


def test_auto_gc_persistent_failures_do_not_starve_healthy_candidates(tmp_path):
    """GIVEN 回収が永続的に失敗する candidate (branch ref の `.lock` で `git branch -D` が失敗) が試行上限より多く、
            その後ろに健全な candidate がある
    WHEN 自動 GC を繰り返し呼ぶ (件数 / 試行数 / 時間は bounded)
    THEN 失敗が先頭の枠を占有し続けず、健全な candidate がいずれ回収される。`.lock` file は触られない
    mutation 対象: 走査開始位置の回転を外す / 失敗した試行を成功件数として数えない上限を外す退行
    """
    repo = _make_repo(tmp_path)
    failing = []
    locks: dict[Path, int] = {}
    for index in range(8):
        suffix = f"fail000{index}"
        holder = _make_dead_marker_holder(repo, suffix)
        lock = repo / ".git" / "refs" / "heads" / f"{BRANCH_PREFIX}{suffix}.lock"
        lock.write_text("", encoding="utf-8")
        locks[lock] = lock.stat().st_mtime_ns
        failing.append(holder)
    healthy = _make_dead_marker_holder(repo, "zzzzzzzz")

    # 先頭固定 (旧挙動相当) では、失敗が試行枠を使い切り後続の健全な candidate は永久に deferred になる。
    for _ in range(3):
        report = canary.gc_disposable_worktrees(
            repo, max_candidates=3, max_attempts=6, time_budget_seconds=60.0, start_offset=0
        )
        reasons = [c["reason"] for c in report["candidates"]]
        assert reasons.count("branch_delete_failed") == 6  # 総試行数は max_attempts で bounded
        assert _entry(report, healthy.name)["action"] == "deferred" and report["truncated"]
    assert healthy.is_dir()

    # 自動 GC (開始位置が回転する) を繰り返せば、健全な candidate はいずれ回収される。
    for _ in range(80):
        canary._auto_gc_disposable_worktrees(repo)
        if not healthy.exists():
            break
    assert not healthy.exists() and f"{BRANCH_PREFIX}zzzzzzzz" not in _branches(repo)
    for holder in failing:  # 失敗した candidate は marker / branch を残し、`.lock` は一切触らない
        assert (holder / canary.DISPOSABLE_OWNER_MARKER_NAME).is_file()
        assert f"{BRANCH_PREFIX}{holder.name[len(HOLDER_PREFIX):]}" in _branches(repo)
    for lock, mtime_ns in locks.items():
        assert lock.is_file() and lock.stat().st_mtime_ns == mtime_ns


def _install_git_shim(tmp_path: Path, monkeypatch, rules: list[dict]) -> tuple[Path, Path]:
    """PATH 先頭に置く `git` shim。rules に一致する subcommand は sleep してから実 git を exec する。
    全 invocation を (開始時刻, args) として log に残す。返り値は (log, rules file)。"""
    shim_dir = tmp_path / "git-shim"
    shim_dir.mkdir()
    log, rules_file = tmp_path / "git-shim.log", tmp_path / "git-shim-rules.json"
    rules_file.write_text(json.dumps(rules), encoding="utf-8")
    real_git = shutil.which("git")
    assert real_git
    shim = shim_dir / "git"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "args = sys.argv[1:]\n"
        f"open({str(log)!r}, 'a').write(json.dumps({{'t': time.time(), 'args': args}}) + '\\n')\n"
        f"for rule in json.load(open({str(rules_file)!r})):\n"
        "    if all(token in args for token in rule['match']):\n"
        "        time.sleep(rule['seconds'])\n"
        "        break\n"
        f"os.execv({real_git!r}, [{real_git!r}] + args)\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return log, rules_file


def _shim_calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def test_gc_time_budget_is_enforced_per_git_call_and_exhaustion_defers(tmp_path, monkeypatch):
    """GIVEN 総時間 budget が短く、実 git (shim 経由) が特定の subcommand で遅延する
    WHEN GC を実行する (identity query 中 / `git worktree remove` 後の branch 検証中に deadline を超える)
    THEN deadline 後に新しい git query / branch 削除を始めず、各 git 呼び出しの timeout は残り時間で bounded
         (identity query も 60 秒ではない)。中断した candidate は marker / branch を残して deferred、
         report は truncated / partial。自動 GC は raise せず、次回の通常 GC が持ち越した分を回収する
    mutation 対象: `_remaining_seconds` が deadline 後に 1 秒の最小値を返す / identity query が既定 60 秒を使う退行
    """
    repo = _make_repo(tmp_path)
    targets = sorted((_prepare_in_dead_child(repo) for _ in range(2)), key=lambda t: t.parent.name)
    first, second = targets
    log, rules_file = _install_git_shim(
        tmp_path, monkeypatch, [{"match": ["rev-parse", "--show-toplevel"], "seconds": 20.0}]
    )
    seen_timeouts: list[tuple[list[str], float]] = []
    after_remove_delay = {"seconds": 0.0}
    real_git = canary._git

    def recording_git(args, *, cwd, timeout=60.0, **kwargs):
        seen_timeouts.append((list(args), timeout))
        result = real_git(args, cwd=cwd, timeout=timeout, **kwargs)
        if list(args)[:2] == ["worktree", "remove"]:
            time.sleep(after_remove_delay["seconds"])  # remove 完了後、次の呼び出しまでに deadline を跨がせる
        return result

    monkeypatch.setattr(canary, "_git", recording_git)

    # --- (1) identity query (`rev-parse --show-toplevel`) が 20 秒止まる。budget 2 秒で打ち切る。
    budget = 2.0
    started = time.time()
    report = canary.gc_disposable_worktrees(
        repo, max_candidates=3, time_budget_seconds=budget, start_offset=0
    )
    elapsed = time.time() - started
    assert elapsed < budget + 4.0, elapsed  # 旧実装は identity query が 60 秒 timeout で 20 秒待つ
    identity_timeouts = [t for args, t in seen_timeouts if args[:2] == ["rev-parse", "--show-toplevel"]]
    assert identity_timeouts and max(identity_timeouts) <= budget + 0.01, identity_timeouts
    assert all(t <= budget + 0.01 for _args, t in seen_timeouts), seen_timeouts
    assert report["truncated"] and report["outcome"] == "partial"
    assert _entry(report, first.parent.name) == {
        "holder": first.parent.name, "action": "deferred", "reason": "gc_budget_exhausted",
    }
    assert _entry(report, second.parent.name)["action"] == "deferred"
    assert all(call["t"] <= started + budget + 0.5 for call in _shim_calls(log))  # deadline 後に新規 git なし
    for target in targets:
        assert target.is_dir() and _branch_of(target) in _branches(repo)
        assert (target.parent / canary.DISPOSABLE_OWNER_MARKER_NAME).is_file()

    # --- (2) `git worktree remove` は deadline 内に完了するが、その後に deadline を超える (operation 開始後の枯渇)。
    #         以降の worktree list / branch 検証 / branch 削除は 1 本も始めてはならない。
    rules_file.write_text(json.dumps([{"match": ["worktree", "remove"], "seconds": 1.0}]), encoding="utf-8")
    after_remove_delay["seconds"] = 3.0
    log.unlink()
    budget = 3.0
    started = time.time()
    report = canary.gc_disposable_worktrees(
        repo, max_candidates=3, time_budget_seconds=budget, start_offset=0
    )
    assert time.time() - started < budget + 4.0
    calls = _shim_calls(log)
    assert all(call["t"] <= started + budget + 0.5 for call in calls)  # deadline 後に新規 git を始めない
    assert [c["args"][:2] for c in calls if c["args"][:2] == ["worktree", "remove"]] == [["worktree", "remove"]]
    assert not [c for c in calls if c["args"][:2] == ["branch", "-D"]]  # branch 削除は始まっていない
    assert _entry(report, first.parent.name)["action"] == "deferred"
    assert _entry(report, second.parent.name)["action"] == "deferred"
    assert report["truncated"] and report["outcome"] == "partial"
    assert (first.parent / canary.DISPOSABLE_OWNER_MARKER_NAME).is_file() and _branch_of(first) in _branches(repo)
    assert not os.path.lexists(first)  # remove は完了済み。残りの資源は次回に持ち越される
    assert second.is_dir() and _branch_of(second) in _branches(repo)

    after_remove_delay["seconds"] = 0.0

    # --- (3) canary-facing の自動 GC は budget 超過でも raise せず、bounded な時間で戻る。
    rules_file.write_text(json.dumps([{"match": ["worktree", "list"], "seconds": 20.0}]), encoding="utf-8")
    monkeypatch.setattr(canary, "DISPOSABLE_AUTO_GC_TIME_BUDGET_SECONDS", 1.5)
    started = time.time()
    canary._auto_gc_disposable_worktrees(repo)
    assert time.time() - started < 1.5 + 4.0
    assert second.is_dir()

    # --- (4) 遅延が解消されれば、持ち越された資源は次回の GC が回収する。
    rules_file.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(canary, "_git", real_git)
    report = canary.gc_disposable_worktrees(repo)
    assert report["outcome"] == "complete"
    for target in targets:
        assert not target.parent.exists() and _branch_of(target) not in _branches(repo)
