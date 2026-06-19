#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/run_mlsys26_astra.sh [options]

Run the MLSys26 Mixtral Chakra ET workload with ASTRA-sim analytical backend.
Run this script inside the ASTRA-sim Docker environment, for example after:

  ./launch_docker.sh

Options:
  --build                         Rebuild analytical congestion-aware backend first.
  --run-name NAME                 Name of the run directory under runs/.
  --run-root DIR                  Directory where run outputs are written.
  --workload-prefix PATH          Chakra ET prefix. Do not include .0.et.
  --system-configuration PATH     ASTRA-sim system config.
  --network-configuration PATH    ASTRA-sim analytical network config.
  --remote-memory-configuration PATH
                                  ASTRA-sim remote memory config.
  --binary PATH                   ASTRA-sim binary to run.
  -h, --help                      Show this help.

Outputs:
  stdout.log                      Full console output.
  summary.txt                     Finished ranks, error scan, and key statistics.
  log/log.log                     ASTRA-sim rotating debug log.
  log/err.log                     ASTRA-sim rotating error log.
EOF
}

SCRIPT_DIR=$(dirname "$(realpath "$0")")
PROJECT_DIR=$(realpath "${SCRIPT_DIR}/..")

BUILD_FIRST=false
RUN_ROOT="${PROJECT_DIR}/runs"
RUN_NAME="mlsys26_mixtral_$(date +%Y%m%d_%H%M%S)"
WORKLOAD_PREFIX="${PROJECT_DIR}/datasets/chakra/mlsys26/traces/et/chakra_trace"
SYSTEM_CONFIGURATION="${PROJECT_DIR}/examples/system/native_collectives/HGX-H100-validated.json"
NETWORK_CONFIGURATION="${PROJECT_DIR}/examples/network/analytical/HGX-H100-validated.yml"
REMOTE_MEMORY_CONFIGURATION="${PROJECT_DIR}/examples/remote_memory/analytical/no_memory_expansion.json"
ASTRA_SIM_BINARY="${PROJECT_DIR}/build/astra_analytical/build/bin/AstraSim_Analytical_Congestion_Aware"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --build)
      BUILD_FIRST=true
      shift
      ;;
    --run-name)
      RUN_NAME="${2:?missing value for --run-name}"
      shift 2
      ;;
    --run-root)
      RUN_ROOT="${2:?missing value for --run-root}"
      shift 2
      ;;
    --workload-prefix)
      WORKLOAD_PREFIX="${2:?missing value for --workload-prefix}"
      shift 2
      ;;
    --system-configuration)
      SYSTEM_CONFIGURATION="${2:?missing value for --system-configuration}"
      shift 2
      ;;
    --network-configuration)
      NETWORK_CONFIGURATION="${2:?missing value for --network-configuration}"
      shift 2
      ;;
    --remote-memory-configuration)
      REMOTE_MEMORY_CONFIGURATION="${2:?missing value for --remote-memory-configuration}"
      shift 2
      ;;
    --binary)
      ASTRA_SIM_BINARY="${2:?missing value for --binary}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "${PROJECT_DIR}" != "/app/astra-sim" ]]; then
  echo "Warning: this script is intended to run inside Docker at /app/astra-sim." >&2
  echo "Current project directory: ${PROJECT_DIR}" >&2
fi

require_file() {
  local path="$1"
  local label="$2"
  if [[ ! -e "${path}" ]]; then
    echo "Missing ${label}: ${path}" >&2
    exit 1
  fi
}

require_file "${WORKLOAD_PREFIX}.0.et" "workload rank 0 ET"
require_file "${SYSTEM_CONFIGURATION}" "system configuration"
require_file "${NETWORK_CONFIGURATION}" "network configuration"
require_file "${REMOTE_MEMORY_CONFIGURATION}" "remote memory configuration"

if [[ "${BUILD_FIRST}" == true ]]; then
  echo "[run] Building analytical congestion-aware backend..."
  "${PROJECT_DIR}/build/astra_analytical/build.sh" -t congestion_aware
fi

require_file "${ASTRA_SIM_BINARY}" "ASTRA-sim binary"

RUN_DIR="${RUN_ROOT}/${RUN_NAME}"
mkdir -p "${RUN_DIR}/log"

echo "[run] Output directory: ${RUN_DIR}"
echo "[run] Workload prefix: ${WORKLOAD_PREFIX}"
echo "[run] Starting ASTRA-sim..."

cd "${RUN_DIR}"
set +e
"${ASTRA_SIM_BINARY}" \
  --workload-configuration="${WORKLOAD_PREFIX}" \
  --system-configuration="${SYSTEM_CONFIGURATION}" \
  --remote-memory-configuration="${REMOTE_MEMORY_CONFIGURATION}" \
  --network-configuration="${NETWORK_CONFIGURATION}" \
  2>&1 | tee stdout.log
SIM_STATUS=${PIPESTATUS[0]}
set -e

{
  echo "# ASTRA-sim MLSys26 Mixtral Run Summary"
  echo
  echo "Run directory: ${RUN_DIR}"
  echo "Exit status: ${SIM_STATUS}"
  echo "Workload prefix: ${WORKLOAD_PREFIX}"
  echo "System configuration: ${SYSTEM_CONFIGURATION}"
  echo "Network configuration: ${NETWORK_CONFIGURATION}"
  echo "Remote memory configuration: ${REMOTE_MEMORY_CONFIGURATION}"
  echo

  echo "## Finished ranks"
  grep "finished," stdout.log || true
  echo

  echo "## Error scan"
  if grep -Eih "critical|error|assert|failed|abort|segmentation" stdout.log log/err.log 2>/dev/null; then
    true
  else
    echo "No critical/error/assert/failure lines found."
  fi
  echo

  echo "## Key statistics"
  grep -E "Wall time|GPU time|CPU time|Comm time|Total compute-communication overlap" stdout.log || true
  echo

  echo "## Max wall time"
  awk '
    /Wall time:/ {
      line = $0
      sub(/^.*sys\[/, "", line)
      sys = line
      sub(/\].*$/, "", sys)

      val = $0
      sub(/^.*Wall time: /, "", val)
      sub(/[^0-9].*$/, "", val)

      if (val + 0 > max) {
        max = val + 0
        maxstr = val
        maxsys = sys
      }
    }
    END {
      if (maxstr != "") {
        printf("sys[%s] %s cycles\n", maxsys, maxstr)
      } else {
        print("not found")
      }
    }
  ' stdout.log
} > summary.txt

echo "[run] Summary written to: ${RUN_DIR}/summary.txt"
cat summary.txt

exit "${SIM_STATUS}"
