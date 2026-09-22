#!/usr/bin/env bash
set -euo pipefail

EXP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$EXP_DIR/../.." && pwd)"

BIN="$ROOT/extern/network_backend/ns-3/build/scratch/ns3.42-AstraSimNetwork-default"
GENERATOR="$EXP_DIR/generate_workload.py"
CHAKRA_PYTHON="$ROOT/extern/graph_frontend/chakra/build/lib"
SPEC="$EXP_DIR/workload_spec.json"
WORKLOAD="$EXP_DIR/workload/workload"
SYSTEM="$EXP_DIR/system.json"
NETWORK_TEMPLATE="$EXP_DIR/ns3_config.txt"
REMOTE_MEMORY="$ROOT/examples/remote_memory/analytical/no_memory_expansion.json"
LOGICAL_TOPOLOGY="$EXP_DIR/logical_topology.json"
VERIFY="$EXP_DIR/verify_paths.py"
COMPARE="$EXP_DIR/compare_results.py"
LOG_ROOT="$EXP_DIR/log"
PYTHON_BIN="${ASTRA_PYTHON:-python3}"

usage() {
    cat <<'EOF'
Usage: experiments/exp1/run.sh [command] [case]

Commands:
  run [mixed|reverse|path0|path1|ecmp]       With no case, run and compare four cases (default).
                                              With a case, run only that case.
  generate [mixed|reverse|path0|path1|ecmp]  Generate Chakra ET files only.
  verify [mixed|reverse|path0|path1|ecmp]    Verify an existing case log.
  build                          Build the ASTRA-sim NS-3 backend.
  test                           Build and run all path-pinning and ECMP tests.
  help                           Show this message.

Shortcuts: mixed, reverse, path0, path1, ecmp, and all (same as test).
Each run is archived under experiments/exp1/log/<timestamp>_<case>/.
Default four-case reports are written to experiments/exp1/log/comparison_<timestamp>.md.
EOF
}

normalize_case() {
    case "${1:-mixed}" in
        mixed|reverse|path0|path1|ecmp) printf '%s\n' "${1:-mixed}" ;;
        *)
            echo "ERROR: unknown experiment case: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
}

generate_case() {
    local case_name
    case_name="$(normalize_case "$1")"
    local resolved_spec_output="${2:-}"
    local generator_log="${3:-}"
    local args=(--spec "$SPEC" --output-dir "$EXP_DIR/workload")
    case "$case_name" in
        path0) args+=(--path-override 0) ;;
        path1) args+=(--path-override 1) ;;
        reverse) args+=(--reverse-paths) ;;
        ecmp) args+=(--no-path-pinning) ;;
        mixed) ;;
    esac
    if [[ -n "$resolved_spec_output" ]]; then
        args+=(--resolved-spec-output "$resolved_spec_output")
    fi
    if ! PYTHONPATH="$CHAKRA_PYTHON${PYTHONPATH:+:$PYTHONPATH}" \
        "$PYTHON_BIN" -c 'from chakra.schema.protobuf import et_def_pb2' \
        >/dev/null 2>&1; then
        echo "ERROR: $PYTHON_BIN cannot import the bundled Chakra protobuf." >&2
        echo "Set ASTRA_PYTHON to a Python with a compatible protobuf runtime." >&2
        exit 1
    fi
    if [[ -n "$generator_log" ]]; then
        PYTHONPATH="$CHAKRA_PYTHON${PYTHONPATH:+:$PYTHONPATH}" \
            "$PYTHON_BIN" "$GENERATOR" "${args[@]}" 2>&1 | tee "$generator_log"
    else
        PYTHONPATH="$CHAKRA_PYTHON${PYTHONPATH:+:$PYTHONPATH}" \
            "$PYTHON_BIN" "$GENERATOR" "${args[@]}"
    fi
}

check_inputs() {
    local files=(
        "$BIN"
        "$SYSTEM"
        "$NETWORK_TEMPLATE"
        "$REMOTE_MEMORY"
        "$LOGICAL_TOPOLOGY"
        "$EXP_DIR/workload/workload.0.et"
    )
    local f
    for f in "${files[@]}"; do
        if [[ ! -e "$f" ]]; then
            echo "ERROR: missing file: $f" >&2
            exit 1
        fi
    done
}

make_runtime_config() {
    local run_dir="$1"
    local runtime_config="$run_dir/config/ns3_config.runtime.txt"
    mkdir -p "$run_dir/config"

    # Keep the checked-in config readable inside /app/astra-sim containers,
    # while also making this entry point portable to any checkout path.
    sed \
        -e "s#/app/astra-sim#$ROOT#g" \
        -e "s#$EXP_DIR/ns3_output/#$run_dir/#g" \
        "$NETWORK_TEMPLATE" > "$runtime_config"
    printf '%s\n' "$runtime_config"
}

verify_log() {
    local case_name="$1"
    local log="$2"
    "$PYTHON_BIN" "$VERIFY" --case "$case_name" --log "$log"
}

verify_case() {
    local case_name
    case_name="$(normalize_case "$1")"
    local latest_dir=""
    if [[ -d "$LOG_ROOT" ]]; then
        latest_dir="$(
            find "$LOG_ROOT" -mindepth 1 -maxdepth 1 -type d \
                -name "*_${case_name}" -print 2>/dev/null | sort | tail -n 1
        )"
    fi

    local log
    if [[ -n "$latest_dir" ]]; then
        log="$latest_dir/run.log"
    else
        # Backward-compatible fallback for runs created before timestamped
        # experiment archives were introduced.
        log="$EXP_DIR/ns3_output/$case_name/run.log"
    fi
    echo "Verifying: $log"
    verify_log "$case_name" "$log"
}

snapshot_configs() {
    local run_dir="$1"
    local case_name="$2"
    local runtime_config="$3"
    local config_dir="$run_dir/config"

    cp "$SPEC" "$config_dir/workload_spec.source.json"
    cp "$SYSTEM" "$config_dir/system.json"
    cp "$LOGICAL_TOPOLOGY" "$config_dir/logical_topology.json"
    cp "$REMOTE_MEMORY" "$config_dir/remote_memory.json"
    cp "$NETWORK_TEMPLATE" "$config_dir/ns3_config.source.txt"
    cp "$EXP_DIR/physical_topology.txt" "$config_dir/physical_topology.txt"
    cp "$EXP_DIR/flow.txt" "$config_dir/flow.txt"
    cp "$EXP_DIR/trace.txt" "$config_dir/trace.txt"
    cp "$EXP_DIR/workload/comm_group.json" "$config_dir/comm_group.json"

    mkdir -p "$config_dir/workload"
    cp "$EXP_DIR"/workload/workload.*.et "$config_dir/workload/"

    {
        echo "timestamp=$(basename "$run_dir")"
        echo "case=$case_name"
        echo "repository=$ROOT"
        echo "binary=$BIN"
        echo "runtime_network_config=$runtime_config"
        echo "command=$BIN --workload-configuration=$WORKLOAD --system-configuration=$SYSTEM --network-configuration=$runtime_config --remote-memory-configuration=$REMOTE_MEMORY --logical-topology-configuration=$LOGICAL_TOPOLOGY"
        git -C "$ROOT" rev-parse HEAD 2>/dev/null | sed 's/^/git_commit=/' || true
    } > "$config_dir/manifest.txt"
}

run_case() {
    local case_name
    case_name="$(normalize_case "$1")"
    local timestamp
    timestamp="$(date '+%Y%m%d_%H%M%S')"
    local run_dir="$LOG_ROOT/${timestamp}_${case_name}"
    if [[ -e "$run_dir" ]]; then
        echo "ERROR: experiment archive already exists: $run_dir" >&2
        echo "Wait one second before retrying to avoid overwriting it." >&2
        exit 1
    fi
    mkdir -p "$run_dir/config"

    generate_case \
        "$case_name" \
        "$run_dir/config/workload_spec.resolved.json" \
        "$run_dir/generate.log"
    check_inputs

    local runtime_config
    runtime_config="$(make_runtime_config "$run_dir")"
    snapshot_configs "$run_dir" "$case_name" "$runtime_config"
    local log="$run_dir/run.log"

    echo "========================================"
    echo "ASTRA-sim path-pinning experiment"
    echo "CASE: $case_name"
    echo "EXP:  $EXP_DIR"
    echo "BIN:  $BIN"
    echo "RUN:  $run_dir"
    echo "LOG:  $log"
    echo "========================================"

    "$BIN" \
        --workload-configuration="$WORKLOAD" \
        --system-configuration="$SYSTEM" \
        --network-configuration="$runtime_config" \
        --remote-memory-configuration="$REMOTE_MEMORY" \
        --logical-topology-configuration="$LOGICAL_TOPOLOGY" \
        2>&1 | tee "$log"

    verify_log "$case_name" "$log" 2>&1 | tee -a "$log"
    echo "Archived experiment: $run_dir"
    LAST_RUN_DIR="$run_dir"
}

run_default_suite() {
    local suite_timestamp
    suite_timestamp="$(date '+%Y%m%d_%H%M%S')"
    local run_dirs=()
    local case_name

    # Keep mixed last so the generated workload left in the working directory
    # remains the repository's default configuration after the suite finishes.
    for case_name in path0 path1 reverse mixed; do
        run_case "$case_name"
        run_dirs+=("$LAST_RUN_DIR")
    done

    local comparison="$LOG_ROOT/comparison_${suite_timestamp}.md"
    "$PYTHON_BIN" "$COMPARE" --output "$comparison" "${run_dirs[@]}"
}

build_backend() {
    "$ROOT/build/astra_ns3/build.sh" -c
}

command="${1:-run}"
case "$command" in
    run)
        if [[ $# -ge 2 ]]; then
            run_case "$2"
        else
            run_default_suite
        fi
        ;;
    generate)
        generate_case "${2:-mixed}"
        ;;
    verify)
        verify_case "${2:-mixed}"
        ;;
    build)
        build_backend
        ;;
    test|all)
        build_backend
        run_case path0
        run_case path1
        run_case mixed
        run_case reverse
        run_case ecmp
        ;;
    mixed|reverse|path0|path1|ecmp)
        run_case "$command"
        ;;
    help|-h|--help)
        usage
        ;;
    *)
        echo "ERROR: unknown command: $command" >&2
        usage >&2
        exit 2
        ;;
esac
