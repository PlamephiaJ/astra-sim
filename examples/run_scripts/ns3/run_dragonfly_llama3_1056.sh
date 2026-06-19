#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(dirname "$(realpath "$0")")
PROJECT_DIR=$(realpath "${SCRIPT_DIR}/../../..")
NS3_DIR="${PROJECT_DIR}/extern/network_backend/ns-3"
WORKLOAD="${PROJECT_DIR}/datasets/stage/generated/llama3_70b_b4096_r1056/llama3_70b"
COMM_GROUP="${PROJECT_DIR}/datasets/stage/generated/llama3_70b_b4096_r1056/llama3_70b.json"
SYSTEM="${PROJECT_DIR}/examples/system/native_collectives/Dragonfly_3D_Ring.json"
NETWORK="${PROJECT_DIR}/examples/network/ns3/dragonfly_4_8_4_33_network.txt"
LOGICAL_TOPOLOGY="${PROJECT_DIR}/examples/network/ns3/dragonfly_4_8_4_33_logical.json"
MEMORY="${PROJECT_DIR}/examples/remote_memory/analytical/no_memory_expansion.json"

if [[ -z "${ASTRA_SIM_BINARY:-}" ]]; then
    ASTRA_SIM_BINARY=$(find "${NS3_DIR}/build/scratch" -maxdepth 1 -type f \
        -name 'ns3.*-AstraSimNetwork-default' -print -quit 2>/dev/null || true)
fi

if [[ -z "${ASTRA_SIM_BINARY}" || ! -x "${ASTRA_SIM_BINARY}" ]]; then
    echo "Missing AstraSimNetwork binary; run build/astra_ns3/build.sh first." >&2
    exit 1
fi

for required in \
    "${WORKLOAD}.0.et" \
    "${WORKLOAD}.1055.et" \
    "${COMM_GROUP}" \
    "${SYSTEM}" \
    "${NETWORK}" \
    "${LOGICAL_TOPOLOGY}" \
    "${MEMORY}"; do
    if [[ ! -r "${required}" ]]; then
        echo "Missing required input: ${required}" >&2
        exit 1
    fi
done

cd "${NS3_DIR}/build/scratch"
exec "${ASTRA_SIM_BINARY}" \
    --workload-configuration="${WORKLOAD}" \
    --comm-group-configuration="${COMM_GROUP}" \
    --system-configuration="${SYSTEM}" \
    --network-configuration="${NETWORK}" \
    --logical-topology-configuration="${LOGICAL_TOPOLOGY}" \
    --remote-memory-configuration="${MEMORY}"
