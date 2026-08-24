#!/usr/bin/env bash
# AI-tone gate: scans prose files for em-dashes, en-dashes, curly quotes, and
# blocklisted marketing words. Portable to macOS bash 3.2 (no mapfile, no grep -P).
#
# Exit codes: 0 clean, 1 findings, 2+ script/tool error.
# The clean path prints a non-vacuity receipt (count of files actually scanned)
# so a scan of nothing can never render as a scan of everything.
#
# File set: tracked AND untracked-but-not-ignored prose files, so a violation in
# a file that is about to ship is caught before its first commit.

set -u

fail=0
scanned=0

# UTF-8 byte patterns (work in every grep): em dash U+2014, en dash U+2013,
# curly quotes U+2018/U+2019/U+201C/U+201D.
EM_DASH=$(printf '\xe2\x80\x94')
EN_DASH=$(printf '\xe2\x80\x93')
CURLY=$(printf '\xe2\x80\x98\|\xe2\x80\x99\|\xe2\x80\x9c\|\xe2\x80\x9d')

BLOCKLIST='leverage|seamless(ly)?|robust|comprehensive|cutting-edge|revolutionary|streamlin(e|ed|ing)|elevate|empower(ing)?|delve|effortless(ly)?|game-chang(er|ing)|unlock(ed|ing)?'

# Prose surfaces only. Code files are exempt (identifiers may legitimately match).
files=$(git ls-files --cached --others --exclude-standard -- '*.md' '*.mdx' '*.txt' 2>/dev/null) || {
    echo "ERROR: git ls-files failed" >&2
    exit 2
}

for f in $files; do
    [ -f "$f" ] || continue
    scanned=$((scanned + 1))

    rc=0
    out=$(grep -n "$EM_DASH" "$f") || rc=$?
    if [ "$rc" -eq 0 ]; then
        echo "EM-DASH in $f:"; echo "$out"; fail=1
    elif [ "$rc" -ge 2 ]; then
        echo "ERROR: grep failed on $f" >&2; exit 2
    fi

    rc=0
    out=$(grep -n "$EN_DASH" "$f") || rc=$?
    if [ "$rc" -eq 0 ]; then
        echo "EN-DASH in $f:"; echo "$out"; fail=1
    elif [ "$rc" -ge 2 ]; then
        echo "ERROR: grep failed on $f" >&2; exit 2
    fi

    rc=0
    out=$(grep -n "$CURLY" "$f") || rc=$?
    if [ "$rc" -eq 0 ]; then
        echo "CURLY QUOTE in $f:"; echo "$out"; fail=1
    elif [ "$rc" -ge 2 ]; then
        echo "ERROR: grep failed on $f" >&2; exit 2
    fi

    rc=0
    out=$(grep -inE "\\b($BLOCKLIST)\\b" "$f") || rc=$?
    if [ "$rc" -eq 0 ]; then
        echo "BLOCKLIST WORD in $f:"; echo "$out"; fail=1
    elif [ "$rc" -ge 2 ]; then
        echo "ERROR: grep failed on $f" >&2; exit 2
    fi
done

if [ "$scanned" -eq 0 ]; then
    echo "ERROR: scanned zero prose files; refusing to report a vacuous pass" >&2
    exit 2
fi

if [ "$fail" -ne 0 ]; then
    echo "AI-tone gate: FAILED (see findings above; $scanned prose file(s) scanned)"
    exit 1
fi

echo "AI-tone gate: PASSED across $scanned tracked+untracked prose file(s)"
exit 0
