"""Assemble the AgentCore deployment directory for the real Instanter agent.

Same shape as `build_door.py` and for the same reason: what ships is a
directory you can `ls`, assembled from NAMED paths, so research material and
private docs cannot be swept in by a glob.

Unlike the door, this bundle carries real dependencies (Strands, pydantic,
bedrock-agentcore), so it also writes the `pyproject.toml` the AgentCore CLI
reads when it builds the CodeZip.

    .venv/bin/python infra/build_agent_runtime.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
BUILD = ROOT / "infra" / "build" / "agent_runtime"

SOURCES = [
    (ROOT / "infra" / "agent_runtime" / "main.py", BUILD / "main.py"),
    (ROOT / "agent", BUILD / "agent"),
    (ROOT / "engine", BUILD / "engine"),
    (ROOT / "seed" / "synthetic_intake.json", BUILD / "seed" / "synthetic_intake.json"),
]

# Pinned to what this repo resolves and tests against, so the deployed agent
# cannot drift to a different Strands than the 24 adversarial rounds covered.
PYPROJECT = """[project]
name = "instanter-agent-runtime"
version = "0.1.0"
description = "Instanter triage agent on Bedrock AgentCore Runtime"
requires-python = ">=3.12,<3.14"
dependencies = [
    "bedrock-agentcore>=1.22.0",
    "strands-agents>=1.53.0",
    "strands-agents-tools>=0.8.6",
    "pydantic>=2.13.4",
    "boto3>=1.40.0",
]
"""


def main() -> int:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True)

    for source, target in SOURCES:
        if not source.exists():
            print(f"MISSING SOURCE: {source}", file=sys.stderr)
            return 1
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(
                source,
                target,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.jsonl", "*.manifests"),
            )
        else:
            shutil.copy2(source, target)

    (BUILD / "pyproject.toml").write_text(PYPROJECT)

    required = [
        BUILD / "main.py",
        BUILD / "pyproject.toml",
        BUILD / "agent" / "graph.py",
        BUILD / "agent" / "runner.py",
        BUILD / "agent" / "hooks.py",
        BUILD / "agent" / "tools.py",
        BUILD / "agent" / "spans.py",
        BUILD / "agent" / "audit.py",
        BUILD / "engine" / "deadline.py",
        BUILD / "seed" / "synthetic_intake.json",
    ]
    missing = [p for p in required if not p.exists()]
    if missing:
        print("BUILD INCOMPLETE:", *[str(p) for p in missing], sep="\n  ", file=sys.stderr)
        return 1

    # The entrypoint reaches into the runner for the post-graph finish. If that
    # ever stops existing the deploy should fail here, not at the first
    # attorney approval in front of a judge.
    runner = (BUILD / "agent" / "runner.py").read_text()
    for symbol in ("def _finish_live(", "def _load_records("):
        if symbol not in runner:
            print(f"BUNDLED RUNNER IS MISSING {symbol}", file=sys.stderr)
            return 1

    files = sorted(p for p in BUILD.rglob("*") if p.is_file())
    total = sum(p.stat().st_size for p in files)
    print(f"built {BUILD.relative_to(ROOT)}: {len(files)} files, {total / 1024:.1f} KiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
