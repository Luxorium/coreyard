#!/usr/bin/env bash
# Back up the parts of this install that cannot be rebuilt from anywhere else.
#
# The code is on GitHub and the catalogue is on Shopify, but three things exist only here:
#
#   coreyard_sync_state.sqlite3   ~26k content fingerprints AND the `retired` table, which
#                                 remembers the status each part held before it was archived.
#                                 Lose it and (a) every part looks new, so the next API run
#                                 rewrites the whole catalogue, and (b) a part that comes back
#                                 to the yard has its stock restored onto a still-ARCHIVED
#                                 product — in stock, invisible to shoppers. Shopify does not
#                                 record the prior status, so that memory is unrecoverable.
#   coreyard_webhook_queue.sqlite3 in-flight order deliveries not yet printed/booked.
#   .env / schema.json            credentials and the hand-built mapping to the source DB.
#
# SQLite is copied with the online-backup API, not `cp`: cron writes to these files every few
# minutes, and a plain copy taken mid-transaction yields a corrupt database that restores
# without complaint and fails later.
#
#   scripts/backup_state.sh                 # -> out/backups/, keeps KEEP days
#   BACKUP_DIR=/mnt/easystore/... scripts/backup_state.sh
set -uo pipefail

# Everything written below holds either customer PII (in-flight order payloads) or
# credentials, so it is created owner-only from the start. `chmod` after the fact still runs
# as a backstop, but it cannot close the window between a file appearing and being narrowed.
umask 077

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${COREYARD_PYTHON:-$REPO/.venv/bin/python}"
DEST="${BACKUP_DIR:-$REPO/out/backups}"
KEEP="${BACKUP_KEEP_DAYS:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$DEST/$STAMP"

mkdir -p "$OUT" || exit 1
status=0

for db in coreyard_sync_state.sqlite3 coreyard_webhook_queue.sqlite3; do
  [ -f "$REPO/$db" ] || continue
  if ! "$PY" - "$REPO/$db" "$OUT/$db" <<'PYEOF'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
# The backup API copies a transactionally consistent snapshot even while writers are active.
with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as s, sqlite3.connect(dst) as d:
    s.backup(d)
# Prove the copy is readable before it is allowed to count as a backup.
with sqlite3.connect(f"file:{dst}?mode=ro", uri=True) as c:
    if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit(f"integrity check failed for {dst}")
PYEOF
  then
    echo "!! backup failed: $db" >&2
    status=1
  fi
done

# Secrets and the schema mapping: small, and neither is in git.
for f in .env schema.json; do
  [ -f "$REPO/$f" ] && cp -p "$REPO/$f" "$OUT/$f"
done
chmod -R go-rwx "$OUT"

if [ "$status" -ne 0 ]; then
  echo "backup INCOMPLETE -> $OUT" >&2
  exit 1
fi

# Only prune once a good backup exists, so a run of failures cannot age out the last copy.
find "$DEST" -mindepth 1 -maxdepth 1 -type d -mtime "+$KEEP" -exec rm -rf {} + 2>/dev/null

# Rotate the job logs. Nothing else does, and the catch-up sync alone appends 288 times a
# day. Keep one previous generation: enough to investigate this morning's failure, bounded
# enough that logs cannot quietly fill the disk the whole pipeline runs on.
for log in "$REPO"/out/*.log; do
  [ -f "$log" ] || continue
  size=$(stat -c %s "$log" 2>/dev/null || echo 0)
  if [ "$size" -gt "${LOG_MAX_BYTES:-5242880}" ]; then
    # copy-then-truncate, not mv-then-create: a 45-minute sync may be holding this file
    # open, and renaming it would leave that job appending to the rotated copy while the
    # live log stayed empty. Truncating the same inode keeps the open handle pointed here.
    cp -f "$log" "$log.1" && : > "$log"
    echo "rotated $(basename "$log") (was $((size/1024)) KB)"
  fi
done

rows=$("$PY" -c "
import sqlite3,sys
c=sqlite3.connect('$OUT/coreyard_sync_state.sqlite3')
print('parts=%d retired=%d' % (
  c.execute('SELECT COUNT(*) FROM parts').fetchone()[0],
  c.execute('SELECT COUNT(*) FROM retired').fetchone()[0]))
" 2>/dev/null || echo "unreadable")
echo "backup OK -> $OUT ($rows, keeping ${KEEP}d)"
