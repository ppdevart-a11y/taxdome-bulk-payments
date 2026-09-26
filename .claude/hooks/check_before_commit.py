"""Claude Code PreToolUse hook: block anything that creates a commit until make check passes."""

import json
import os
import re
import subprocess
import sys

# A git command that creates commits, on any line: with options, a path, quotes, a line
# continuation, $(which git), or chained with ; && or |. A shell variable or an alias can
# still hide one; CI runs the same gate. A false positive costs one extra make check; a miss
# skips the gate.
CREATES_COMMITS = re.compile(
    r"(?:^|[\s;&|(/\\!\"'`])git[)\"'`]*(?:\s.*)?\s[\"']?"
    r"(?:commit|merge|revert|cherry-pick|rebase|am|pull)[\"']?(?![\w-])",
    re.MULTILINE,
)


def creates_commits(command: str) -> bool:
    # A backslash at the end of a line continues the command on the next one.
    return CREATES_COMMITS.search(command.replace("\\\n", " ")) is not None


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
