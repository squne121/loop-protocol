"""AC13: repo_instance_key is identical for the main worktree and a linked
worktree of the same repository, and different for a separate clone."""

from __future__ import annotations

import subprocess

import task_context_config as config


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "init"], cwd=path, check=True)


def test_given_main_worktree_and_linked_worktree_when_key_computed_then_identical(tmp_path):
    main_repo = tmp_path / "main-repo"
    _init_repo(main_repo)

    linked_worktree = tmp_path / "linked-worktree"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(linked_worktree)],
        cwd=main_repo,
        check=True,
        capture_output=True,
    )

    main_key = config.repo_instance_key(cwd=main_repo)
    linked_key = config.repo_instance_key(cwd=linked_worktree)
    assert main_key == linked_key


def test_given_main_repo_and_separate_clone_when_key_computed_then_different(tmp_path):
    main_repo = tmp_path / "main-repo-2"
    _init_repo(main_repo)

    clone = tmp_path / "separate-clone"
    subprocess.run(["git", "clone", "--quiet", str(main_repo), str(clone)], check=True, capture_output=True)

    main_key = config.repo_instance_key(cwd=main_repo)
    clone_key = config.repo_instance_key(cwd=clone)
    assert main_key != clone_key


def test_given_same_repo_when_key_computed_twice_then_stable(tmp_path):
    repo = tmp_path / "stable-repo"
    _init_repo(repo)
    assert config.repo_instance_key(cwd=repo) == config.repo_instance_key(cwd=repo)


def test_given_repo_instance_key_when_computed_then_it_is_a_sha256_hex_digest(tmp_path):
    repo = tmp_path / "sha-repo"
    _init_repo(repo)
    key = config.repo_instance_key(cwd=repo)
    assert len(key) == 64
    int(key, 16)  # raises ValueError if not valid hex
