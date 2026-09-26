"""Claude Code PreToolUse hook: block an agent's git commit until make check passes."""

import json
import os
import re
import subprocess
import sys

# Any `git … commit` on a line. A false positive costs one extra make check; a miss skips the gate.
GIT_COMMIT = re.compile(r"(?:^|[\s;&|(/\"'`])git(?:\s.*)?\scommit(?:\s|$)", re.MULTILINE)


def is_commit(command: str) -> bool:
    return GIT_COMMIT.search(command) is not None


def main() -> None:
    command = json.load(sys.stdin)["tool_input"]["command"]
    if not is_commit(command):
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
