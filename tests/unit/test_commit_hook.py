"""Every command an agent might use to create a commit must run the gate first."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

HOOK = Path(__file__).parents[2] / ".claude" / "hooks" / "check_before_commit.py"


def load_hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_before_commit", HOOK)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = load_hook()


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "Add a thing"',
        'git -C "/Users/me/Taxdome home task" commit -m x',  # a path with spaces
        "/usr/bin/git commit -m x",
        'bash -c "git commit -m x"',
        "git add a.py && git commit -m x",
        "git --no-pager commit --amend",
        "git commit -q -F - -- a.py <<'EOF'\nSubject\nEOF",
        "git merge feature",
        "git revert HEAD",
        "git cherry-pick abc123",
        "git rebase main",
        "git am fix.patch",
        "git pull origin main",
    ],
)
def test_commands_that_create_commits_run_the_gate(command: str) -> None:
    assert hook.creates_commits(command)


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git commit-tree abc123",
        "git merge-base main HEAD",
        "git log --oneline",
        "echo committed",
        "legit commit",
        "uv run pytest",
    ],
)
def test_other_commands_do_not(command: str) -> None:
    assert not hook.creates_commits(command)
