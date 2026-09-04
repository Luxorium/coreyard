#!/bin/sh
# Detached overnight completion for the September 2026 catalogue cleanup.
#
# The stages are deliberately ordered: generated product copy first, photo alt text second.
# `set -e` prevents stale fitment wording from being copied into photo metadata when the
# catalogue repair has not completed cleanly.

set -eu

# Run from the repository root, wherever this installation keeps it: a hardcoded path
# is one installation's identity, and this tree is generic.
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

repair_attempt=1
while :; do
    echo "=== catalogue repair attempt ${repair_attempt} ==="
    if bin/coreyard --lock catalog-repair repair all --apply --show 8; then
        break
    fi
    if [ "$repair_attempt" -ge 3 ]; then
        echo "Catalogue repair did not complete after three idempotent attempts." >&2
        exit 1
    fi
    repair_attempt=$((repair_attempt + 1))
done

alt_log="out/alt_overwrite_20260902.jsonl"
alt_attempt=1
while :; do
    echo "=== photo alt overwrite attempt ${alt_attempt} ==="
    if [ -s "$alt_log" ]; then
        if bin/coreyard --lock altfix altfix --overwrite --log "$alt_log"; then
            break
        fi
    else
        if bin/coreyard --lock altfix altfix --overwrite --no-resume --log "$alt_log"; then
            break
        fi
    fi
    if [ "$alt_attempt" -ge 3 ]; then
        echo "Photo alt overwrite did not complete after three resumable attempts." >&2
        exit 1
    fi
    alt_attempt=$((alt_attempt + 1))
done

bin/coreyard counts --apply
echo "=== overnight catalogue cleanup complete ==="
