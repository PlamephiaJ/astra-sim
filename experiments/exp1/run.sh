#!/usr/bin/env bash
set -euo pipefail

EXP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$EXP_DIR/../.." && pwd)"

BIN="$ROOT/extern/network_backend/ns-3/build/scratch/ns3.42-AstraSimNetwork-default"

WORKLOAD="$EXP_DIR/workload/workload"
SYSTEM="$EXP_DIR/system.json"
NETWORK="$EXP_DIR/ns3_config.txt"
REMOTE_MEMORY="$ROOT/examples/remote_memory/analytical/no_memory_expansion.json"
LOGICAL_TOPOLOGY="$EXP_DIR/logical_topology.json"

echo "========================================"
echo "ASTRA-sim experiment"
echo "EXP:  $EXP_DIR"
echo "BIN:  $BIN"
echo "========================================"

# Basic checks
for f in \
    "$BIN" \
    "$SYSTEM" \
    "$NETWORK" \
    "$REMOTE_MEMORY" \
    "$LOGICAL_TOPOLOGY" \
    "$EXP_DIR/workload/workload.0.et"
do
    if [[ ! -e "$f" ]]; then
        echo "ERROR: missing file:"
        echo "  $f"
        exit 1
    fi
done

mkdir -p "$EXP_DIR/ns3_output"

exec "$BIN" \
    --workload-configuration="$WORKLOAD" \
    --system-configuration="$SYSTEM" \
    --network-configuration="$NETWORK" \
    --remote-memory-configuration="$REMOTE_MEMORY" \
    --logical-topology-configuration="$LOGICAL_TOPOLOGY"