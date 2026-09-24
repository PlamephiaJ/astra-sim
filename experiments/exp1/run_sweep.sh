#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Sweep configuration: edit these values before running.
# The range is inclusive: START, START + STEP, ..., <= END.
#
# Unit note: despite the field name, each configured "compute cycle" is written
# directly to Chakra duration_micros, so one unit here means 1 microsecond.
# ASTRA-sim replay mode converts it to 1000 nanosecond-scale simulator ticks,
# which runtime logs report as cycles. For example, 1000 here contributes
# 1,000,000 reported simulator cycles to each COMP node.
# ---------------------------------------------------------------------------
COMPUTE_DURATION_US_START="${COMPUTE_DURATION_US_START:-0}"
COMPUTE_DURATION_US_END="${COMPUTE_DURATION_US_END:-15000}"
COMPUTE_DURATION_US_STEP="${COMPUTE_DURATION_US_STEP:-250}"

# Number of duration points evaluated concurrently. Each point launches four
# routing cases, so peak simulator processes = DURATION_PARALLELISM * 4.
DURATION_PARALLELISM="${DURATION_PARALLELISM:-4}"

EXP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER="$EXP_DIR/run.sh"
SUMMARIZER="$EXP_DIR/tools/compare_sweep.py"
LOG_ROOT="$EXP_DIR/log"
PYTHON_BIN="${ASTRA_PYTHON:-python3}"

validate_range() {
    local value
    for value in \
        "$COMPUTE_DURATION_US_START" \
        "$COMPUTE_DURATION_US_END" \
        "$COMPUTE_DURATION_US_STEP"; do
        if [[ ! "$value" =~ ^[0-9]+$ ]]; then
            echo "ERROR: sweep values must be non-negative integers" >&2
            exit 2
        fi
    done
    if (( COMPUTE_DURATION_US_STEP == 0 )); then
        echo "ERROR: COMPUTE_DURATION_US_STEP must be > 0" >&2
        exit 2
    fi
    if (( COMPUTE_DURATION_US_END < COMPUTE_DURATION_US_START )); then
        echo "ERROR: COMPUTE_DURATION_US_END must be >= COMPUTE_DURATION_US_START" >&2
        exit 2
    fi
    if [[ ! "$DURATION_PARALLELISM" =~ ^[1-9][0-9]*$ ]]; then
        echo "ERROR: DURATION_PARALLELISM must be a positive integer" >&2
        exit 2
    fi
}

validate_range

timestamp="$(date '+%Y%m%d_%H%M%S')"
sweep_dir="${EXP1_SWEEP_ROOT:-$LOG_ROOT/$timestamp}"
if [[ -e "$sweep_dir" ]]; then
    echo "ERROR: sweep archive already exists: $sweep_dir" >&2
    exit 1
fi
mkdir -p "$sweep_dir"

# Archive the complete sweep driver's stdout/stderr while keeping terminal
# output visible. Each cycles_<N>/ child also gets its own log.log/err.log.
exec > >(tee "$sweep_dir/log.log")
exec 2> >(tee "$sweep_dir/err.log" >&2)

{
    echo "created_at=$(date --iso-8601=seconds)"
    echo "kind=compute_duration_us_sweep"
    echo "start=$COMPUTE_DURATION_US_START"
    echo "end=$COMPUTE_DURATION_US_END"
    echo "step=$COMPUTE_DURATION_US_STEP"
    echo "parallel_cases_per_duration=4"
    echo "parallel_durations=$DURATION_PARALLELISM"
    echo "max_parallel_simulators=$((DURATION_PARALLELISM * 4))"
} > "$sweep_dir/manifest.txt"

cycle_dirs=()
batch_pids=()
batch_durations=()

wait_duration_batch() {
    local failed=0
    local index
    for index in "${!batch_pids[@]}"; do
        if wait "${batch_pids[$index]}"; then
            echo "Finished compute-duration point: ${batch_durations[$index]} us"
        else
            echo "FAILED compute-duration point: ${batch_durations[$index]} us" >&2
            failed=1
        fi
    done
    batch_pids=()
    batch_durations=()
    if (( failed != 0 )); then
        return 1
    fi
}

for ((cycles = COMPUTE_DURATION_US_START; cycles <= COMPUTE_DURATION_US_END; cycles += COMPUTE_DURATION_US_STEP)); do
    cycle_dir="$sweep_dir/cycles_$cycles"
    cycle_dirs+=("$cycle_dir")
    echo "Launching compute-duration point $cycles us -> $cycle_dir"

    (
        COMPUTE_CYCLES_OVERRIDE="$cycles" \
            EXP1_RUN_ROOT="$cycle_dir" \
            "$RUNNER" run
    ) &
    batch_pids+=("$!")
    batch_durations+=("$cycles")

    # Execute duration points in bounded parallel batches. Each child run.sh
    # independently launches path0/path1/reverse/mixed in parallel.
    if (( ${#batch_pids[@]} >= DURATION_PARALLELISM )); then
        wait_duration_batch
    fi
done

if (( ${#batch_pids[@]} > 0 )); then
    wait_duration_batch
fi

"$PYTHON_BIN" "$SUMMARIZER" \
    --output "$sweep_dir/sweep_summary.md" \
    "${cycle_dirs[@]}"

echo "Sweep archive: $sweep_dir"
echo "Sweep summary: $sweep_dir/sweep_summary.md"
