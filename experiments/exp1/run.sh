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
PYTHON_BIN="${ASTRA_PYTHON:-python3}"

usage() {
    cat <<'EOF'
Usage: experiments/exp1/run.sh [command] [case]

Commands:
  run [mixed|path0|path1|ecmp]       Generate, run, and verify one case (default).
  generate [mixed|path0|path1|ecmp]  Generate Chakra ET files only.
  verify [mixed|path0|path1|ecmp]    Verify an existing case log.
  build                          Build the ASTRA-sim NS-3 backend.
  test                           Build and run path0, path1, mixed, and ECMP tests.
  help                           Show this message.

Shortcuts: mixed, path0, path1, ecmp, and all (same as test).
EOF
}

normalize_case() {
    case "${1:-mixed}" in
        mixed|path0|path1|ecmp) printf '%s\n' "${1:-mixed}" ;;
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
    local args=(--spec "$SPEC" --output-dir "$EXP_DIR/workload")
    case "$case_name" in
        path0) args+=(--path-override 0) ;;
        path1) args+=(--path-override 1) ;;
        ecmp) args+=(--no-path-pinning) ;;
        mixed) ;;
    esac
    if ! PYTHONPATH="$CHAKRA_PYTHON${PYTHONPATH:+:$PYTHONPATH}" \
        "$PYTHON_BIN" -c 'from chakra.schema.protobuf import et_def_pb2' \
        >/dev/null 2>&1; then
        echo "ERROR: $PYTHON_BIN cannot import the bundled Chakra protobuf." >&2
        echo "Set ASTRA_PYTHON to a Python with a compatible protobuf runtime." >&2
        exit 1
    fi
    PYTHONPATH="$CHAKRA_PYTHON${PYTHONPATH:+:$PYTHONPATH}" \
        "$PYTHON_BIN" "$GENERATOR" "${args[@]}"
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
    local case_name="$1"
    local case_dir="$EXP_DIR/ns3_output/$case_name"
    local runtime_config="$case_dir/ns3_config.runtime.txt"
    mkdir -p "$case_dir"

    # Keep the checked-in config readable inside /app/astra-sim containers,
    # while also making this entry point portable to any checkout path.
    sed \
        -e "s#/app/astra-sim#$ROOT#g" \
        -e "s#$EXP_DIR/ns3_output/#$case_dir/#g" \
        "$NETWORK_TEMPLATE" > "$runtime_config"
    printf '%s\n' "$runtime_config"
}

verify_case() {
    local case_name
    case_name="$(normalize_case "$1")"
    local log="$EXP_DIR/ns3_output/$case_name/run.log"
    "$PYTHON_BIN" "$VERIFY" --case "$case_name" --log "$log"
}

run_case() {
    local case_name
    case_name="$(normalize_case "$1")"
    generate_case "$case_name"
    check_inputs

    local case_dir="$EXP_DIR/ns3_output/$case_name"
    local runtime_config
    runtime_config="$(make_runtime_config "$case_name")"
    local log="$case_dir/run.log"

    echo "========================================"
    echo "ASTRA-sim path-pinning experiment"
    echo "CASE: $case_name"
    echo "EXP:  $EXP_DIR"
    echo "BIN:  $BIN"
    echo "LOG:  $log"
    echo "========================================"

    "$BIN" \
        --workload-configuration="$WORKLOAD" \
        --system-configuration="$SYSTEM" \
        --network-configuration="$runtime_config" \
        --remote-memory-configuration="$REMOTE_MEMORY" \
        --logical-topology-configuration="$LOGICAL_TOPOLOGY" \
        2>&1 | tee "$log"

    verify_case "$case_name"
}

build_backend() {
    "$ROOT/build/astra_ns3/build.sh" -c
}

command="${1:-run}"
case "$command" in
    run)
        run_case "${2:-mixed}"
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
        run_case ecmp
        ;;
    mixed|path0|path1|ecmp)
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
