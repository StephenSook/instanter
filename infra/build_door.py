"""Assemble the door Lambda's deployment directory.

Explicit rather than clever. CDK points at `infra/build/door/`, and this script
is the only thing that puts anything there, so what ships is inspectable with
`ls` instead of inferred from a bundling image.

The door imports the REAL engine rather than a copy of its arithmetic, so this
copies `engine/` and the seed corpus in beside the handler. The bundle has no
third-party dependencies at all: `engine/` is pure standard library and boto3
is already present in the Lambda runtime.

    .venv/bin/python infra/build_door.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
BUILD = ROOT / "infra" / "build" / "door"

# Every path that ships, named. Nothing is copied by glob from the repo root,
# so research material and private docs cannot be swept in by accident.
SOURCES = [
    (ROOT / "infra" / "door" / "handler.py", BUILD / "handler.py"),
    (ROOT / "engine", BUILD / "engine"),
    (ROOT / "agent" / "__init__.py", BUILD / "agent" / "__init__.py"),
    (ROOT / "agent" / "triage.py", BUILD / "agent" / "triage.py"),
    (ROOT / "seed" / "synthetic_intake.json", BUILD / "seed" / "synthetic_intake.json"),
]


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
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(source, target)

    # Verify what was built, rather than trusting that the copies happened.
    # A bundle missing the engine would deploy happily and fail on the first
    # request, which is the shape this project keeps finding the hard way.
    required = [
        BUILD / "handler.py",
        BUILD / "engine" / "deadline.py",
        BUILD / "engine" / "rules.py",
        BUILD / "engine" / "holidays.py",
        BUILD / "agent" / "triage.py",
        BUILD / "seed" / "synthetic_intake.json",
    ]
    missing = [p for p in required if not p.exists()]
    if missing:
        print("BUILD INCOMPLETE:", *[str(p) for p in missing], sep="\n  ", file=sys.stderr)
        return 1

    files = sorted(p for p in BUILD.rglob("*") if p.is_file())
    total = sum(p.stat().st_size for p in files)
    print(f"built {BUILD.relative_to(ROOT)}: {len(files)} files, {total / 1024:.1f} KiB")
    for p in files:
        print(f"  {p.relative_to(BUILD)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
