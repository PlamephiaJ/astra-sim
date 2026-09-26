#!/usr/bin/env bash
set -euo pipefail

EXP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$EXP_DIR/../.." && pwd)"
SHARED_FIXED_DIR="$ROOT/experiments/exp1/fixed"
TOPOLOGY_TEMPLATE="$EXP_DIR/fixed/physical_topology.template.txt"
UGAL_ROUTES="$EXP_DIR/fixed/ugal_l_routes.json"
TOOLS_DIR="$EXP_DIR/tools"
BIN="$ROOT/extern/network_backend/ns-3/build/scratch/ns3.42-AstraSimNetwork-default"
GENERATOR="$TOOLS_DIR/generate_workload.py"
VERIFY="$TOOLS_DIR/verify_flows.py"
CHAKRA_PYTHON="$ROOT/extern/graph_frontend/chakra/build/lib"
SPEC="$EXP_DIR/workload_spec.json"
SYSTEM="$SHARED_FIXED_DIR/system.json"
NETWORK_TEMPLATE="$SHARED_FIXED_DIR/ns3_config.txt"
REMOTE_MEMORY="$ROOT/examples/remote_memory/analytical/no_memory_expansion.json"
LOGICAL_TOPOLOGY="$SHARED_FIXED_DIR/logical_topology.json"
LOG_ROOT="$EXP_DIR/log"
PYTHON_BIN="${ASTRA_PYTHON:-python3}"
DEFAULT_CASES=(ecmp)
LONG_HOP1_DELAY="${EXP2_LONG_HOP1_DELAY:-0.100ms}"
LONG_HOP2_DELAY="${EXP2_LONG_HOP2_DELAY:-0.100ms}"
UGAL_BIAS_BYTES="${EXP2_UGAL_BIAS_BYTES:-0}"
QLEN_MON_END_NS="${EXP2_QLEN_MON_END_NS:-100000000}"
HAS_WIN="${EXP2_HAS_WIN:-1}"

usage() {
    cat <<'EOF'
Usage: experiments/exp2/run.sh [command] [case]

Commands:
  run [ecmp|ugal_l]        Run the selected flow-routing strategy.
  generate [case]         Generate and archive ET files only.
  verify [case]           Verify the newest archived run.
  build                   Build the ASTRA-sim NS-3 backend.
  test                    Build and run the default ECMP case.
  help                    Show this message.

Environment:
  EXP2_LONG_HOP1_DELAY     Delay of link 8-10 (default: 0.100ms)
  EXP2_LONG_HOP2_DELAY     Delay of link 10-9 (default: 0.100ms)
  EXP2_UGAL_BIAS_BYTES     Extra non-minimal byte-hop cost (default: 0)
  EXP2_QLEN_MON_END_NS     Queue monitor end time in ns (default: 100000000)
  EXP2_HAS_WIN             Enable RDMA window limiting: 0 or 1 (default: 1)

The direct 8-9 path is one hop. The 8-10-9 path is two hops and is therefore
used only when the ugal_l strategy selects its non-minimal route.
EOF
}

normalize_case() {
    case "${1:-ecmp}" in
        ecmp|ugal_l) printf '%s\n' "${1:-ecmp}" ;;
        *) echo "ERROR: unknown exp2 case: $1" >&2; usage >&2; exit 2 ;;
    esac
}

allocate_run_root() {
    local run_root="$LOG_ROOT/$(date '+%Y%m%d_%H%M%S')"
    if [[ -e "$run_root" ]]; then
        echo "ERROR: archive already exists: $run_root" >&2
        exit 1
    fi
    mkdir -p "$run_root"
    printf '%s\n' "$run_root"
}

check_chakra_python() {
    if ! PYTHONPATH="$CHAKRA_PYTHON${PYTHONPATH:+:$PYTHONPATH}" \
        "$PYTHON_BIN" -c 'from chakra.schema.protobuf import et_def_pb2' \
        >/dev/null 2>&1; then
        echo "ERROR: $PYTHON_BIN cannot import the bundled Chakra protobuf." >&2
        echo "Set ASTRA_PYTHON to a compatible Python interpreter." >&2
        exit 1
    fi
}

generate_case() {
    local case_name="$(normalize_case "$1")"
    local output_dir="$2"
    local resolved_output="$3"
    local args=(--spec "$SPEC" --output-dir "$output_dir" \
        --resolved-spec-output "$resolved_output" --no-routing-label)
    check_chakra_python
    mkdir -p "$output_dir"
    PYTHONPATH="$CHAKRA_PYTHON${PYTHONPATH:+:$PYTHONPATH}" \
        "$PYTHON_BIN" "$GENERATOR" "${args[@]}"
}

validate_delay() {
    local name="$1" value="$2"
    if [[ ! "$value" =~ ^[0-9]+([.][0-9]+)?(ns|us|ms|s)$ ]]; then
        echo "ERROR: $name='$value' must be an NS-3 time such as 0.1ms" >&2
        exit 2
    fi
}

validate_ugal_bias() {
    if [[ ! "$UGAL_BIAS_BYTES" =~ ^[0-9]+$ ]]; then
        echo "ERROR: EXP2_UGAL_BIAS_BYTES must be a non-negative integer" >&2
        exit 2
    fi
}

validate_qlen_mon_end() {
    if [[ ! "$QLEN_MON_END_NS" =~ ^[1-9][0-9]*$ ]]; then
        echo "ERROR: EXP2_QLEN_MON_END_NS must be a positive integer" >&2
        exit 2
    fi
}

validate_has_win() {
    if [[ "$HAS_WIN" != "0" && "$HAS_WIN" != "1" ]]; then
        echo "ERROR: EXP2_HAS_WIN must be 0 or 1" >&2
        exit 2
    fi
}

make_runtime_topology() {
    local run_dir="$1"
    local output="$run_dir/config/physical_topology.txt"
    validate_delay EXP2_LONG_HOP1_DELAY "$LONG_HOP1_DELAY"
    validate_delay EXP2_LONG_HOP2_DELAY "$LONG_HOP2_DELAY"
    sed -e "s#__LONG_HOP1_DELAY__#$LONG_HOP1_DELAY#g" \
        -e "s#__LONG_HOP2_DELAY__#$LONG_HOP2_DELAY#g" \
        "$TOPOLOGY_TEMPLATE" > "$output"
    printf '%s\n' "$output"
}

make_runtime_config() {
    local run_dir="$1"
    local runtime_topology="$2"
    local output="$run_dir/config/ns3_config.runtime.txt"
    sed -e "s#/app/astra-sim#$ROOT#g" \
        -e "s#^TOPOLOGY_FILE .*#TOPOLOGY_FILE $runtime_topology#" \
        -e "s#__OUTPUT_DIR__#$run_dir#g" \
        -e "s#^QLEN_MON_END .*#QLEN_MON_END $QLEN_MON_END_NS#" \
        -e "s#^HAS_WIN .*#HAS_WIN $HAS_WIN#" \
        "$NETWORK_TEMPLATE" > "$output"
    printf '%s\n' "$output"
}

snapshot_inputs() {
    local run_dir="$1"
    local case_name="$2"
    local runtime_config="$3"
    local workload_prefix="$4"
    local routing_mode="$case_name"
    local config_dir="$run_dir/config"
    cp "$SPEC" "$config_dir/workload_spec.source.json"
    cp "$SYSTEM" "$config_dir/system.json"
    cp "$LOGICAL_TOPOLOGY" "$config_dir/logical_topology.json"
    cp "$REMOTE_MEMORY" "$config_dir/remote_memory.json"
    cp "$NETWORK_TEMPLATE" "$config_dir/ns3_config.source.txt"
    cp "$TOPOLOGY_TEMPLATE" "$config_dir/physical_topology.template.txt"
    cp "$UGAL_ROUTES" "$config_dir/ugal_l_routes.json"
    cp "$SHARED_FIXED_DIR/flow.txt" "$config_dir/flow.txt"
    cp "$SHARED_FIXED_DIR/trace.txt" "$config_dir/trace.txt"
    {
        echo "case=$case_name"
        echo "routing_mode=$routing_mode"
        echo "repository=$ROOT"
        echo "binary=$BIN"
        echo "runtime_network_config=$runtime_config"
        echo "long_hop1_delay=$LONG_HOP1_DELAY"
        echo "long_hop2_delay=$LONG_HOP2_DELAY"
        echo "ugal_bias_bytes=$UGAL_BIAS_BYTES"
        echo "qlen_mon_end_ns=$QLEN_MON_END_NS"
        echo "has_win=$HAS_WIN"
        echo "command=$BIN --routing-mode=$routing_mode --flow-routing-configuration=$UGAL_ROUTES --ugal-local-bias-bytes=$UGAL_BIAS_BYTES --workload-configuration=$workload_prefix --system-configuration=$SYSTEM --network-configuration=$runtime_config --remote-memory-configuration=$REMOTE_MEMORY --logical-topology-configuration=$LOGICAL_TOPOLOGY"
        git -C "$ROOT" rev-parse HEAD 2>/dev/null | sed 's/^/git_commit=/' || true
    } > "$config_dir/manifest.txt"
}

run_case() {
    local case_name="$(normalize_case "$1")"
    local suite_dir="$2"
    local run_dir="$suite_dir/$case_name"
    local config_dir="$run_dir/config"
    local workload_dir="$config_dir/workload"
    local workload_prefix="$workload_dir/workload"
    local routing_mode="$case_name"
    validate_ugal_bias
    validate_qlen_mon_end
    validate_has_win
    mkdir -p "$workload_dir"
    generate_case "$case_name" "$workload_dir" \
        "$config_dir/workload_spec.resolved.json" > "$run_dir/generate.log" 2>&1
    local runtime_topology="$(make_runtime_topology "$run_dir")"
    local runtime_config="$(make_runtime_config "$run_dir" "$runtime_topology")"
    snapshot_inputs "$run_dir" "$case_name" "$runtime_config" "$workload_prefix"
    "$BIN" \
        --routing-mode="$routing_mode" \
        --flow-routing-configuration="$UGAL_ROUTES" \
        --ugal-local-bias-bytes="$UGAL_BIAS_BYTES" \
        --workload-configuration="$workload_prefix" \
        --system-configuration="$SYSTEM" \
        --network-configuration="$runtime_config" \
        --remote-memory-configuration="$REMOTE_MEMORY" \
        --logical-topology-configuration="$LOGICAL_TOPOLOGY" \
        2>&1 | tee "$run_dir/run.log"
    awk '/^(NS3_UGAL|NS3_ECMP)/' "$run_dir/run.log" \
        > "$run_dir/routing_decisions.log"
    "$PYTHON_BIN" "$VERIFY" --case "$case_name" \
        --log "$run_dir/run.log" --flow-plan "$workload_dir/flow_plan.json" \
        2>&1 | tee -a "$run_dir/run.log"
}

write_suite_manifest() {
    local suite_dir="$1"; shift
    {
        echo "created_at=$(date --iso-8601=seconds)"
        echo "cases=$*"
        echo "parallel_cases=$#"
    } > "$suite_dir/manifest.txt"
}

run_cases() {
    local suite_dir="$1"; shift
    local cases=("$@") pids=() case_name
    write_suite_manifest "$suite_dir" "${cases[@]}"
    for case_name in "${cases[@]}"; do
        mkdir -p "$suite_dir/$case_name"
        echo "Launching $case_name -> $suite_dir/$case_name"
        (run_case "$case_name" "$suite_dir" > "$suite_dir/$case_name/console.log" 2>&1) &
        pids+=("$!")
    done
    local failed=0 index
    for index in "${!pids[@]}"; do
        case_name="${cases[$index]}"
        if wait "${pids[$index]}"; then
            echo "Finished $case_name"
        else
            echo "FAILED $case_name" >&2
            tail -n 60 "$suite_dir/$case_name/console.log" >&2 || true
            failed=1
        fi
    done
    return "$failed"
}

run_command() {
    local suite_dir="$(allocate_run_root)"
    if [[ $# -eq 0 ]]; then
        run_cases "$suite_dir" "${DEFAULT_CASES[@]}"
    else
        local case_name="$(normalize_case "$1")"
        mkdir -p "$suite_dir/$case_name"
        write_suite_manifest "$suite_dir" "$case_name"
        run_case "$case_name" "$suite_dir"
    fi
    echo "Suite archive: $suite_dir"
}

generate_command() {
    local case_name="$(normalize_case "${1:-ecmp}")"
    local suite_dir="$(allocate_run_root)"
    local run_dir="$suite_dir/$case_name"
    local workload_dir="$run_dir/config/workload"
    mkdir -p "$workload_dir"
    write_suite_manifest "$suite_dir" "$case_name"
    generate_case "$case_name" "$workload_dir" \
        "$run_dir/config/workload_spec.resolved.json" | tee "$run_dir/generate.log"
    cp "$SPEC" "$run_dir/config/workload_spec.source.json"
    echo "Generated workload archive: $run_dir"
}

verify_command() {
    local case_name="$(normalize_case "${1:-ecmp}")"
    local run_log=""
    run_log="$(find "$LOG_ROOT" -type f -path "*/$case_name/run.log" \
        -printf '%T@ %p\n' 2>/dev/null | sort -n | tail -n 1 | cut -d' ' -f2-)"
    if [[ -z "$run_log" ]]; then
        echo "ERROR: no archived $case_name run under $LOG_ROOT" >&2
        exit 1
    fi
    local run_dir="$(dirname "$run_log")"
    "$PYTHON_BIN" "$VERIFY" --case "$case_name" --log "$run_log" \
        --flow-plan "$run_dir/config/workload/flow_plan.json"
}

command="${1:-run}"
case "$command" in
    run)
        if [[ $# -ge 2 ]]; then run_command "$2"; else run_command; fi
        ;;
    generate) generate_command "${2:-ecmp}" ;;
    verify) verify_command "${2:-ecmp}" ;;
    build) "$ROOT/build/astra_ns3/build.sh" -c ;;
    test)
        "$ROOT/build/astra_ns3/build.sh" -c
        run_command
        ;;
    ecmp|ugal_l) run_command "$command" ;;
    help|-h|--help) usage ;;
    *) echo "ERROR: unknown command: $command" >&2; usage >&2; exit 2 ;;
esac
