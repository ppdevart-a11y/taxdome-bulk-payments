"""Claude Code PreToolUse hook: block anything that creates a commit until make check passes."""

import json
import os
import re
import subprocess
import sys

# Any git command that creates commits, on any line. A false positive costs one extra make check;
# a miss skips the gate.
CREATES_COMMITS = re.compile(
    r"(?:^|[\s;&|(/\"'`])git(?:\s.*)?\s(?:commit|merge|revert|cherry-pick|rebase|am|pull)(?:\s|$)",
    re.MULTILINE,
)


def creates_commits(command: str) -> bool:
    return CREATES_COMMITS.search(command) is not None


def main() -> None:
    command = json.load(sys.stdin)["tool_input"]["command"]
    if not creates_commits(command):
        return
    gate = subprocess.run(
        ["make", "check"],  # noqa: S607
        cwd=os.environ["CLAUDE_PROJECT_DIR"],
        capture_output=True,
        text=True,
        check=False,
    )
    if gate.returncode != 0:
        output = (gate.stdout + gate.stderr)[-4000:]
        sys.stderr.write(f"Commit blocked: make check failed. Fix it and retry.\n\n{output}\n")
        sys.exit(2)


if __name__ == "__main__":
    main()
