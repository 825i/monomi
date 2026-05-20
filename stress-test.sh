#!/bin/bash
# monomi stress test — 5 minutes of mixed CPU / disk I/O / RAM / swap
# load to exercise every panel of the dashboard at once.
#
# Run ON THE PI:
#   bash stress-test.sh
#
# Safe-ish: caps total allocation around 7 GB on an 8 GB Pi 5, traps
# Ctrl-C and EXIT to kill all child workloads + delete the test file.

set -u
DURATION=300
POOL=/srv/pool
TEST_FILE="$POOL/.stress-test.bin"
PIDS=()

cleanup() {
  echo
  echo "Cleaning up..."
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null
  done
  pkill -P $$ 2>/dev/null
  rm -f "$TEST_FILE"
  echo "Done."
}
trap cleanup EXIT INT TERM

echo "== pi5 stress test, ${DURATION}s =="

# ── RAM hog: hold 3 GiB of resident pages for the duration ────────
python3 - <<PY &
import time
size = 3 * 1024**3
buf = bytearray(size)
for i in range(0, size, 4096):
    buf[i] = 1                       # touch every page so it's resident
time.sleep($DURATION + 2)
PY
PIDS+=($!)
echo "  - RAM hog (3 GiB) started, pid=${PIDS[-1]}"

# ── Swap pressure: a second block big enough that the kernel has to
#    push pages out to zram swap to make room for the system. ──────
python3 - <<PY &
import time
size = 4 * 1024**3                   # +4 GiB on top of the 3 GiB above
buf = bytearray(size)
for i in range(0, size, 4096):
    buf[i] = 1
time.sleep($DURATION + 2)
PY
PIDS+=($!)
echo "  - swap pressure (~4 GiB extra) started, pid=${PIDS[-1]}"

# ── Disk I/O loop: write + flush + read 512 MiB on /srv/pool, then
#    pause briefly, then repeat. ──────────────────────────────────
(
  while true; do
    dd if=/dev/zero of="$TEST_FILE" bs=4M count=128 conv=fdatasync status=none
    sleep 1
    dd if="$TEST_FILE" of=/dev/null bs=4M status=none
    sleep 2
  done
) &
PIDS+=($!)
echo "  - disk i/o loop on $POOL started, pid=${PIDS[-1]}"

# ── CPU: intermittent 2-core yes burst, 4s on / 3s off. ────────────
(
  while true; do
    yes > /dev/null &
    Y1=$!
    yes > /dev/null &
    Y2=$!
    sleep 4
    kill $Y1 $Y2 2>/dev/null
    sleep 3
  done
) &
PIDS+=($!)
echo "  - cpu burst loop started, pid=${PIDS[-1]}"

echo "All workloads running. Watching the dashboard now..."
sleep "$DURATION"
echo "== done =="
