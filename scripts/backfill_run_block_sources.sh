#!/bin/bash
# HISTORICAL BACKFILL -- runs all 9 per-source extraction scripts for one
# block, SEQUENTIALLY (avoids oversubscribing this machine's ~12 cores --
# each source's MAX_WORKERS was benchmarked assuming exclusive access).
# Cheap/fast sources first (fast feedback), largest last. Each source is
# independently checkpointed/resumable, so a crash mid-way loses no more
# than that source's own already-flushed progress; simply rerun this script
# (or the single failed source's script) to resume.
set -uo pipefail
BLOCK="$1"
cd "$(dirname "$0")/.."
LOG_DIR="data/processed/backfill/$BLOCK/logs"
mkdir -p "$LOG_DIR"

run_source() {
    local script="$1"
    local name="$2"
    echo "=== [$BLOCK] STARTING $name at $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
    python "scripts/$script" --block "$BLOCK" > "$LOG_DIR/$name.log" 2>&1
    local rc=$?
    echo "=== [$BLOCK] FINISHED $name at $(date -u +%Y-%m-%dT%H:%M:%SZ) rc=$rc ==="
    return $rc
}

run_source backfill_observations.py observations
run_source backfill_afd.py afd
run_source backfill_ecmwf.py ecmwf
run_source backfill_gfs.py gfs
run_source backfill_hrrr_apcp_1h.py hrrr_apcp_1h
run_source backfill_hrrr_rh.py hrrr_rh
run_source backfill_hrrr.py hrrr
run_source backfill_nbm.py nbm
run_source backfill_gefs.py gefs

echo "=== [$BLOCK] ALL SOURCES COMPLETE at $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
