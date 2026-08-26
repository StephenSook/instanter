"""Assemble the door Lambda's deployment directory.

Explicit rather than clever. CDK points at `infra/build/door/`, and this script
is the only thing that puts anything there, so what ships is inspectable with
`ls` instead of inferred from a bundling image.

The door imports the REAL engine rather than a copy of its arithmetic, so this
copies `engine/` and the seed corpus in beside the handler. boto3 is already
in the Lambda runtime. pywebpush is unpacked from manylinux wheels so a Mac
build cannot ship Darwin cryptography.

    .venv/bin/python infra/build_door.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
BUILD = ROOT / "infra" / "build" / "door"

# Every path that ships, named. Nothing is copied by glob from the repo root,
# so research material and private docs cannot be swept in by accident.
SOURCES = [
    (ROOT / "infra" / "door" / "handler.py", BUILD / "handler.py"),
    (ROOT / "infra" / "door" / "lock.py", BUILD / "lock.py"),
    (ROOT / "infra" / "door" / "ocr.py", BUILD / "ocr.py"),
    (ROOT / "infra" / "door" / "push.py", BUILD / "push.py"),
    (ROOT / "engine", BUILD / "engine"),
    (ROOT / "agent" / "__init__.py", BUILD / "agent" / "__init__.py"),
    (ROOT / "agent" / "triage.py", BUILD / "agent" / "triage.py"),
    (ROOT / "seed" / "synthetic_intake.json", BUILD / "seed" / "synthetic_intake.json"),
]


def _vendor_pywebpush(build: Path) -> bool:
    """Unpack manylinux wheels so Lambda can send Web Push.

    pip-installing on a Mac would ship Darwin cryptography, which Lambda
    cannot import. A missing pywebpush at runtime is a silent no-op, so
    the build must fail here rather than deploy a door that cannot ping.
    """
    vendor = build / "_wheels"
    vendor.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "pywebpush",
        "--platform",
        "manylinux2014_x86_64",
        "--python-version",
        "312",
        "--implementation",
        "cp",
        "--only-binary",
        ":all:",
        "-d",
        str(vendor),
    ]
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError:
        print("FAILED to download manylinux pywebpush wheels", file=sys.stderr)
        return False
    wheels = list(vendor.glob("*.whl"))
    if not wheels:
        print("pip download wrote no wheels", file=sys.stderr)
        return False
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as zf:
            zf.extractall(build)
    shutil.rmtree(vendor)
    if not any(build.glob("pywebpush*")):
        print("pywebpush did not unpack into the door bundle", file=sys.stderr)
        return False
    return True


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

    if not _vendor_pywebpush(BUILD):
        return 1

    # Verify what was built, rather than trusting that the copies happened.
    # A bundle missing the engine would deploy happily and fail on the first
    # request, which is the shape this project keeps finding the hard way.
    required = [
        BUILD / "handler.py",
        BUILD / "lock.py",
        BUILD / "ocr.py",
        BUILD / "push.py",
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
