"""Claude Code PreToolUse hook: block an agent's git commit until make check passes."""

import json
import os
import re
import subprocess
import sys

GIT_COMMIT = re.compile(r"(?:^|[\s;&|(])git\s+(?:-\S+\s+(?:\S+\s+)?)*commit(?:\s|$)")

command = json.load(sys.stdin)["tool_input"]["command"]
if not GIT_COMMIT.search(command):
    sys.exit(0)

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
