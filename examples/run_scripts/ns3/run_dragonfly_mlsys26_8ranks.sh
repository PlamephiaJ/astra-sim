#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(dirname "$(realpath "$0")")
PROJECT_DIR=$(realpath "${SCRIPT_DIR}/../../..")
NS3_DIR="${PROJECT_DIR}/extern/network_backend/ns-3"
WORKLOAD="${PROJECT_DIR}/datasets/chakra/mlsys26/traces/et/chakra_trace"
SYSTEM="${PROJECT_DIR}/datasets/chakra/mlsys26/plots/m8x7/system.json"
NETWORK="${PROJECT_DIR}/examples/network/ns3/dragonfly_4_8_4_33_network.txt"
LOGICAL_TOPOLOGY="${PROJECT_DIR}/examples/network/ns3/dragonfly_mlsys26_8ranks_logical.json"
MEMORY="${PROJECT_DIR}/examples/remote_memory/analytical/no_memory_expansion.json"
COMM_SCALE="${COMM_SCALE:-1}"

if [[ -z "${ASTRA_SIM_BINARY:-}" ]]; then
    ASTRA_SIM_BINARY=$(find "${NS3_DIR}/build/scratch" -maxdepth 1 -type f \
        -name 'ns3.*-AstraSimNetwork-default' -print -quit 2>/dev/null || true)
fi

if [[ -z "${ASTRA_SIM_BINARY}" || ! -x "${ASTRA_SIM_BINARY}" ]]; then
    echo "Missing AstraSimNetwork binary; run build/astra_ns3/build.sh first." >&2
    exit 1
fi

for rank in $(seq 0 7); do
    if [[ ! -r "${WORKLOAD}.${rank}.et" ]]; then
        echo "Missing workload rank ${rank}: ${WORKLOAD}.${rank}.et" >&2
        exit 1
    fi
done

RUN_DIR="${PROJECT_DIR}/outputs/ns3_dragonfly_mlsys26_8ranks/comm_scale_${COMM_SCALE}"
mkdir -p "${RUN_DIR}"

cd "${NS3_DIR}/build/scratch"
"${ASTRA_SIM_BINARY}" \
    --workload-configuration="${WORKLOAD}" \
    --system-configuration="${SYSTEM}" \
    --network-configuration="${NETWORK}" \
    --logical-topology-configuration="${LOGICAL_TOPOLOGY}" \
    --remote-memory-configuration="${MEMORY}" \
    --comm-scale="${COMM_SCALE}" \
    2>&1 | tee "${RUN_DIR}/stdout.log"

cp "${NS3_DIR}/scratch/output/dragonfly_4_8_4_33_fct.txt" "${RUN_DIR}/fct.txt"
cp "${NS3_DIR}/scratch/output/dragonfly_4_8_4_33_pfc.txt" "${RUN_DIR}/pfc.txt"
cp "${NS3_DIR}/scratch/output/dragonfly_4_8_4_33_qlen.txt" "${RUN_DIR}/qlen.txt"
