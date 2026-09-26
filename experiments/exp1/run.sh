#!/usr/bin/env bash
set -euo pipefail

EXP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$EXP_DIR/../.." && pwd)"
FIXED_DIR="$EXP_DIR/fixed"
TOOLS_DIR="$EXP_DIR/tools"

BIN="$ROOT/extern/network_backend/ns-3/build/scratch/ns3.42-AstraSimNetwork-default"
GENERATOR="$TOOLS_DIR/generate_workload.py"
CHAKRA_PYTHON="$ROOT/extern/graph_frontend/chakra/build/lib"
SPEC="$EXP_DIR/workload_spec.json"
SYSTEM="$FIXED_DIR/system.json"
NETWORK_TEMPLATE="$FIXED_DIR/ns3_config.txt"
REMOTE_MEMORY="$ROOT/examples/remote_memory/analytical/no_memory_expansion.json"
LOGICAL_TOPOLOGY="$FIXED_DIR/logical_topology.json"
VERIFY="$TOOLS_DIR/verify_paths.py"
COMPARE="$TOOLS_DIR/compare_results.py"
LOG_ROOT="$EXP_DIR/log"
PYTHON_BIN="${ASTRA_PYTHON:-python3}"
DEFAULT_CASES=(path0 path1 reverse mixed)

# Optional environment inputs used by run_sweep.sh:
#   COMPUTE_CYCLES_OVERRIDE=<cycles>  override every compute node
#   EXP1_RUN_ROOT=<directory>         use this exact suite directory

usage() {
    cat <<'EOF'
Usage: experiments/exp1/run.sh [command] [case]

Commands:
  run [mixed|reverse|path0|path1|ecmp]       With no case, run four cases in parallel.
                                              With a case, run only that case.
  generate [mixed|reverse|path0|path1|ecmp]  Archive generated Chakra ET files only.
  verify [mixed|reverse|path0|path1|ecmp]    Verify the newest matching case log.
  build                                      Build the ASTRA-sim NS-3 backend.
  test                                       Build and run all cases, including ECMP.
  help                                       Show this message.

Shortcuts: mixed, reverse, path0, path1, ecmp, and all (same as test).

One invocation is archived as:
  experiments/exp1/log/<timestamp>/path0/
                                  /path1/
                                  /reverse/
                                  /mixed/
                                  /log.log
                                  /err.log
                                  /comparison.md

The four default cases use isolated workloads and output files, so they run
concurrently. Set EXP1_RUN_ROOT and COMPUTE_CYCLES_OVERRIDE only when driving
the runner programmatically (run_sweep.sh does this).
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

allocate_run_root() {
    local run_root
    if [[ -n "${EXP1_RUN_ROOT:-}" ]]; then
        run_root="$EXP1_RUN_ROOT"
    else
        run_root="$LOG_ROOT/$(date '+%Y%m%d_%H%M%S')"
    fi
    if [[ -e "$run_root" ]]; then
        echo "ERROR: experiment archive already exists: $run_root" >&2
        exit 1
    fi
    mkdir -p "$run_root"
    printf '%s\n' "$run_root"
}

enable_run_logging() {
    local run_root="$1"
    # Preserve terminal output while archiving this invocation's stdout/stderr.
    exec > >(tee "$run_root/log.log")
    exec 2> >(tee "$run_root/err.log" >&2)
}

check_chakra_python() {
    if ! PYTHONPATH="$CHAKRA_PYTHON${PYTHONPATH:+:$PYTHONPATH}" \
        "$PYTHON_BIN" -c 'from chakra.schema.protobuf import et_def_pb2' \
        >/dev/null 2>&1; then
        echo "ERROR: $PYTHON_BIN cannot import the bundled Chakra protobuf." >&2
        echo "Set ASTRA_PYTHON to a Python with a compatible protobuf runtime." >&2
        exit 1
    fi
}

generate_case() {
    local case_name
    case_name="$(normalize_case "$1")"
    local output_dir="$2"
    local resolved_spec_output="${3:-}"
    local generator_log="${4:-}"
    local args=(--spec "$SPEC" --output-dir "$output_dir")

    case "$case_name" in
        path0) args+=(--routing-label-override 0) ;;
        path1) args+=(--routing-label-override 1) ;;
        reverse) args+=(--invert-routing-labels) ;;
        ecmp) args+=(--no-routing-label) ;;
        mixed) ;;
    esac
    if [[ -n "${COMPUTE_CYCLES_OVERRIDE:-}" ]]; then
        args+=(--compute-cycles-override "$COMPUTE_CYCLES_OVERRIDE")
    fi
    if [[ -n "$resolved_spec_output" ]]; then
        args+=(--resolved-spec-output "$resolved_spec_output")
    fi

    mkdir -p "$output_dir"
    check_chakra_python
    if [[ -n "$generator_log" ]]; then
        PYTHONPATH="$CHAKRA_PYTHON${PYTHONPATH:+:$PYTHONPATH}" \
            "$PYTHON_BIN" "$GENERATOR" "${args[@]}" 2>&1 | tee "$generator_log"
    else
        PYTHONPATH="$CHAKRA_PYTHON${PYTHONPATH:+:$PYTHONPATH}" \
            "$PYTHON_BIN" "$GENERATOR" "${args[@]}"
    fi
}

check_inputs() {
    local workload_prefix="$1"
    local files=(
        "$BIN"
        "$SYSTEM"
        "$NETWORK_TEMPLATE"
        "$REMOTE_MEMORY"
        "$LOGICAL_TOPOLOGY"
        "$workload_prefix.0.et"
    )
    local input_file
    for input_file in "${files[@]}"; do
        if [[ ! -e "$input_file" ]]; then
            echo "ERROR: missing file: $input_file" >&2
            exit 1
        fi
    done
}

make_runtime_config() {
    local run_dir="$1"
    local runtime_config="$run_dir/config/ns3_config.runtime.txt"

    sed \
        -e "s#/app/astra-sim#$ROOT#g" \
        -e "s#__OUTPUT_DIR__#$run_dir#g" \
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
    local latest_entry=""
    if [[ -d "$LOG_ROOT" ]]; then
        latest_entry="$(
            find "$LOG_ROOT" -type f -path "*/${case_name}/run.log" \
                -printf '%T@ %p\n' 2>/dev/null | sort -n | tail -n 1
        )"
    fi

    local log
    if [[ -n "$latest_entry" ]]; then
        log="${latest_entry#* }"
    else
        echo "ERROR: no archived $case_name run found under $LOG_ROOT" >&2
        exit 1
    fi
    echo "Verifying: $log"
    verify_log "$case_name" "$log"
}

snapshot_configs() {
    local run_dir="$1"
    local case_name="$2"
    local runtime_config="$3"
    local workload_prefix="$4"
    local config_dir="$run_dir/config"

    cp "$SPEC" "$config_dir/workload_spec.source.json"
    cp "$SYSTEM" "$config_dir/system.json"
    cp "$LOGICAL_TOPOLOGY" "$config_dir/logical_topology.json"
    cp "$REMOTE_MEMORY" "$config_dir/remote_memory.json"
    cp "$NETWORK_TEMPLATE" "$config_dir/ns3_config.source.txt"
    cp "$FIXED_DIR/physical_topology.txt" "$config_dir/physical_topology.txt"
    cp "$FIXED_DIR/flow.txt" "$config_dir/flow.txt"
    cp "$FIXED_DIR/trace.txt" "$config_dir/trace.txt"

    {
        echo "suite=$(basename "$(dirname "$run_dir")")"
        echo "case=$case_name"
        echo "compute_cycles_override=${COMPUTE_CYCLES_OVERRIDE:-source-spec}"
        echo "repository=$ROOT"
        echo "binary=$BIN"
        echo "runtime_network_config=$runtime_config"
        echo "command=$BIN --routing-mode=workload_label --workload-configuration=$workload_prefix --system-configuration=$SYSTEM --network-configuration=$runtime_config --remote-memory-configuration=$REMOTE_MEMORY --logical-topology-configuration=$LOGICAL_TOPOLOGY"
        git -C "$ROOT" rev-parse HEAD 2>/dev/null | sed 's/^/git_commit=/' || true
    } > "$config_dir/manifest.txt"
}

run_case() {
    local case_name
    case_name="$(normalize_case "$1")"
    local suite_dir="$2"
    local run_dir="$suite_dir/$case_name"
    local config_dir="$run_dir/config"
    local workload_dir="$config_dir/workload"
    local workload_prefix="$workload_dir/workload"

    if [[ -e "$config_dir" || -e "$run_dir/run.log" ]]; then
        echo "ERROR: case archive already exists: $run_dir" >&2
        exit 1
    fi
    mkdir -p "$workload_dir"

    generate_case \
        "$case_name" \
        "$workload_dir" \
        "$config_dir/workload_spec.resolved.json" \
        "$run_dir/generate.log"
    check_inputs "$workload_prefix"

    local runtime_config
    runtime_config="$(make_runtime_config "$run_dir")"
    snapshot_configs "$run_dir" "$case_name" "$runtime_config" "$workload_prefix"
    local log="$run_dir/run.log"

    echo "========================================"
    echo "ASTRA-sim routing-label experiment"
    echo "CASE: $case_name"
    echo "RUN:  $run_dir"
    echo "LOG:  $log"
    echo "========================================"

    "$BIN" \
        --workload-configuration="$workload_prefix" \
        --routing-mode=workload_label \
        --system-configuration="$SYSTEM" \
        --network-configuration="$runtime_config" \
        --remote-memory-configuration="$REMOTE_MEMORY" \
        --logical-topology-configuration="$LOGICAL_TOPOLOGY" \
        2>&1 | tee "$log"

    verify_log "$case_name" "$log" 2>&1 | tee -a "$log"
    echo "Archived experiment: $run_dir"
}

write_suite_manifest() {
    local suite_dir="$1"
    shift
    {
        echo "created_at=$(date --iso-8601=seconds)"
        echo "cases=$*"
        echo "parallel_cases=$#"
        echo "compute_cycles_override=${COMPUTE_CYCLES_OVERRIDE:-source-spec}"
    } > "$suite_dir/manifest.txt"
}

run_cases_parallel() {
    local suite_dir="$1"
    shift
    local cases=("$@")
    local pids=()
    local case_name

    write_suite_manifest "$suite_dir" "${cases[@]}"
    for case_name in "${cases[@]}"; do
        mkdir -p "$suite_dir/$case_name"
        echo "Launching $case_name -> $suite_dir/$case_name"
        (
            run_case "$case_name" "$suite_dir" \
                > "$suite_dir/$case_name/console.log" 2>&1
        ) &
        pids+=("$!")
    done

    local failed=0
    local index
    for index in "${!pids[@]}"; do
        case_name="${cases[$index]}"
        if wait "${pids[$index]}"; then
            echo "Finished $case_name"
        else
            echo "FAILED $case_name; tail of console.log:" >&2
            tail -n 40 "$suite_dir/$case_name/console.log" >&2 || true
            failed=1
        fi
    done
    if (( failed != 0 )); then
        return 1
    fi
}

create_comparison() {
    local suite_dir="$1"
    "$PYTHON_BIN" "$COMPARE" \
        --output "$suite_dir/comparison.md" \
        "$suite_dir/path0" \
        "$suite_dir/path1" \
        "$suite_dir/reverse" \
        "$suite_dir/mixed"
}

run_default_suite() {
    local suite_dir
    suite_dir="$(allocate_run_root)"
    enable_run_logging "$suite_dir"
    run_cases_parallel "$suite_dir" "${DEFAULT_CASES[@]}"
    create_comparison "$suite_dir"
    echo "Suite archive: $suite_dir"
}

run_single_case() {
    local case_name
    case_name="$(normalize_case "$1")"
    local suite_dir
    suite_dir="$(allocate_run_root)"
    enable_run_logging "$suite_dir"
    write_suite_manifest "$suite_dir" "$case_name"
    run_case "$case_name" "$suite_dir"
    echo "Suite archive: $suite_dir"
}

generate_only() {
    local case_name
    case_name="$(normalize_case "$1")"
    local suite_dir
    suite_dir="$(allocate_run_root)"
    enable_run_logging "$suite_dir"
    local run_dir="$suite_dir/$case_name"
    local config_dir="$run_dir/config"
    local workload_dir="$config_dir/workload"

    mkdir -p "$workload_dir"
    write_suite_manifest "$suite_dir" "$case_name"
    generate_case \
        "$case_name" \
        "$workload_dir" \
        "$config_dir/workload_spec.resolved.json" \
        "$run_dir/generate.log"
    cp "$SPEC" "$config_dir/workload_spec.source.json"
    echo "Generated workload archive: $run_dir"
}

build_backend() {
    "$ROOT/build/astra_ns3/build.sh" -c
}

build_only() {
    local suite_dir
    suite_dir="$(allocate_run_root)"
    enable_run_logging "$suite_dir"
    write_suite_manifest "$suite_dir" build
    build_backend
    echo "Build archive: $suite_dir"
}

command="${1:-run}"
case "$command" in
    run)
        if [[ $# -ge 2 ]]; then
            run_single_case "$2"
        else
            run_default_suite
        fi
        ;;
    generate)
        generate_only "${2:-mixed}"
        ;;
    verify)
        verify_case "${2:-mixed}"
        ;;
    build)
        build_only
        ;;
    test|all)
        suite_dir="$(allocate_run_root)"
        enable_run_logging "$suite_dir"
        write_suite_manifest "$suite_dir" build "${DEFAULT_CASES[@]}" ecmp
        build_backend
        run_cases_parallel "$suite_dir" "${DEFAULT_CASES[@]}" ecmp
        create_comparison "$suite_dir"
        echo "Suite archive: $suite_dir"
        ;;
    mixed|reverse|path0|path1|ecmp)
        run_single_case "$command"
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
