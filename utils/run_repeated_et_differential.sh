#!/usr/bin/env bash
set -uo pipefail

PROJECT_DIR=$(realpath "$(dirname "$(realpath "$0")")/..")
NS3_DIR="${PROJECT_DIR}/extern/network_backend/ns-3"
BASE_DIR="${PROJECT_DIR}/artifacts/llama3_70b_b4096_r1056_compact"
DATA_DIR="${PROJECT_DIR}/artifacts/differential_r2"
OUTPUT_DIR="${PROJECT_DIR}/outputs/differential_r2"
BUILD_LOG="${OUTPUT_DIR}/build.log"
MATERIALIZE_LOG="${OUTPUT_DIR}/materialize.log"
COMPARE_LOG="${OUTPUT_DIR}/compare.log"
SUMMARY="${OUTPUT_DIR}/summary.txt"

RANKS="${RANKS:-1056}"
REPEAT_COUNT="${REPEAT_COUNT:-2}"
ID_OFFSET="${ID_OFFSET:-1000000000}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-50400}"

# Optional quick-check knob. Default 0 means compare every node in every rank.
MAX_NODES_PER_RANK="${MAX_NODES_PER_RANK:-0}"

log() {
    printf '[differential] %s\n' "$*"
}

fail() {
    printf 'RESULT=FAIL\nreason=%s\n' "$*" | tee -a "${SUMMARY}"
    exit 1
}

file_size_or_zero() {
    if [[ -f "$1" ]]; then
        stat -c %s "$1"
    else
        printf '0\n'
    fi
}

if pgrep -af AstraSimNetwork | grep -q -- "${DATA_DIR}/"; then
    mkdir -p "${OUTPUT_DIR}"
    : > "${SUMMARY}"
    fail "another differential_r2 NS3 simulation is already running"
fi

rm -rf "${OUTPUT_DIR}"
mkdir -p "${OUTPUT_DIR}"
: > "${SUMMARY}"

printf 'mode=feeder\n' >> "${SUMMARY}"
printf 'ranks=%s\n' "${RANKS}" >> "${SUMMARY}"
printf 'repeat_count=%s\n' "${REPEAT_COUNT}" >> "${SUMMARY}"
printf 'id_offset=%s\n' "${ID_OFFSET}" >> "${SUMMARY}"
printf 'timeout_seconds=%s\n' "${TIMEOUT_SECONDS}" >> "${SUMMARY}"
printf 'max_nodes_per_rank=%s\n' "${MAX_NODES_PER_RANK}" >> "${SUMMARY}"

log "validating ${RANKS} compact base ET files"
for rank in $(seq 0 $((RANKS - 1))); do
    [[ -s "${BASE_DIR}/llama3_70b.${rank}.et" ]] ||
        fail "missing base ET for rank ${rank}"
done
[[ -s "${BASE_DIR}/llama3_70b.json" ]] || fail "missing communicator JSON"

log "building MaterializeRepeatedEt and CompareRepeatedEtFeeder quietly (-j2, nice=10)"
: > "${BUILD_LOG}"
(
    cd "${NS3_DIR}"
    nice -n 10 cmake -S . -B cmake-cache \
        -DASTRA_BUILD_REPEATED_FEEDER_SMOKE_TEST=ON >> "${BUILD_LOG}" 2>&1 &&
    nice -n 10 cmake --build cmake-cache \
        --target MaterializeRepeatedEt CompareRepeatedEtFeeder -j 2 \
        >> "${BUILD_LOG}" 2>&1
)
build_status=$?
printf 'build_status=%s\n' "${build_status}" >> "${SUMMARY}"
if (( build_status != 0 )); then
    tail -n 20 "${BUILD_LOG}" >&2
    fail "build failed; see ${BUILD_LOG}"
fi

MATERIALIZER="${NS3_DIR}/build/MaterializeRepeatedEt"
COMPARATOR="${NS3_DIR}/build/CompareRepeatedEtFeeder"
[[ -x "${MATERIALIZER}" ]] || fail "materializer binary was not produced"
[[ -x "${COMPARATOR}" ]] || fail "comparator binary was not produced"

log "recreating repeat=${REPEAT_COUNT} compact and expanded feeder A/B datasets"
rm -rf "${DATA_DIR}"
mkdir -p "${DATA_DIR}/compact" "${DATA_DIR}/expanded"
for rank in $(seq 0 $((RANKS - 1))); do
    ln "${BASE_DIR}/llama3_70b.${rank}.et" \
        "${DATA_DIR}/compact/llama3_70b.${rank}.et"
    printf '%s %s\n' "${REPEAT_COUNT}" "${ID_OFFSET}" \
        > "${DATA_DIR}/compact/llama3_70b.${rank}.et.repeat"
done
cp "${BASE_DIR}/llama3_70b.json" "${DATA_DIR}/compact/llama3_70b.json"

log "materializing expanded ET inputs (timeout=${TIMEOUT_SECONDS}s)"
: > "${MATERIALIZE_LOG}"
start=$(date +%s)
nice -n 10 timeout --kill-after=30s "${TIMEOUT_SECONDS}s" \
    "${MATERIALIZER}" \
    "${BASE_DIR}/llama3_70b" \
    "${DATA_DIR}/expanded/llama3_70b" \
    "${RANKS}" "${REPEAT_COUNT}" "${ID_OFFSET}" \
    > "${MATERIALIZE_LOG}" 2>&1
materialize_status=$?
materialize_elapsed=$(($(date +%s) - start))
printf 'materialize_status=%s\n' "${materialize_status}" >> "${SUMMARY}"
printf 'materialize_elapsed_seconds=%s\n' "${materialize_elapsed}" >> "${SUMMARY}"
printf 'materialize_log_bytes=%s\n' \
    "$(file_size_or_zero "${MATERIALIZE_LOG}")" >> "${SUMMARY}"
if (( materialize_status == 124 )); then
    printf 'RESULT=INCONCLUSIVE\nreason=expanded ET materialization timed out\n' \
        >> "${SUMMARY}"
    log "INCONCLUSIVE: expanded ET materialization timed out"
    cat "${SUMMARY}"
    exit 2
elif (( materialize_status != 0 )); then
    tail -n 20 "${MATERIALIZE_LOG}" >&2
    fail "expanded ET materialization failed; see ${MATERIALIZE_LOG}"
fi
cp "${BASE_DIR}/llama3_70b.json" "${DATA_DIR}/expanded/llama3_70b.json"

compact_count=$(find "${DATA_DIR}/compact" -maxdepth 1 -name '*.et' | wc -l)
sidecar_count=$(find "${DATA_DIR}/compact" -maxdepth 1 -name '*.et.repeat' | wc -l)
expanded_count=$(find "${DATA_DIR}/expanded" -maxdepth 1 -name '*.et' | wc -l)
printf 'compact_et_count=%s\n' "${compact_count}" >> "${SUMMARY}"
printf 'compact_sidecar_count=%s\n' "${sidecar_count}" >> "${SUMMARY}"
printf 'expanded_et_count=%s\n' "${expanded_count}" >> "${SUMMARY}"
[[ "${compact_count}" -eq "${RANKS}" ]] || fail "compact ET count is ${compact_count}"
[[ "${sidecar_count}" -eq "${RANKS}" ]] || fail "sidecar count is ${sidecar_count}"
[[ "${expanded_count}" -eq "${RANKS}" ]] || fail "expanded ET count is ${expanded_count}"

log "comparing compact repeated feeder vs materialized expanded feeder (timeout=${TIMEOUT_SECONDS}s)"
: > "${COMPARE_LOG}"
start=$(date +%s)
if [[ "${MAX_NODES_PER_RANK}" == 0 ]]; then
    nice -n 10 timeout --kill-after=30s "${TIMEOUT_SECONDS}s" \
        "${COMPARATOR}" \
        "${DATA_DIR}/compact/llama3_70b" \
        "${DATA_DIR}/expanded/llama3_70b" \
        "${RANKS}" \
        > "${COMPARE_LOG}" 2>&1
else
    nice -n 10 timeout --kill-after=30s "${TIMEOUT_SECONDS}s" \
        "${COMPARATOR}" \
        "${DATA_DIR}/compact/llama3_70b" \
        "${DATA_DIR}/expanded/llama3_70b" \
        "${RANKS}" "${MAX_NODES_PER_RANK}" \
        > "${COMPARE_LOG}" 2>&1
fi
compare_status=$?
compare_elapsed=$(($(date +%s) - start))
printf 'compare_status=%s\n' "${compare_status}" >> "${SUMMARY}"
printf 'compare_elapsed_seconds=%s\n' "${compare_elapsed}" >> "${SUMMARY}"
printf 'compare_log_bytes=%s\n' "$(file_size_or_zero "${COMPARE_LOG}")" \
    >> "${SUMMARY}"

compared_nodes=$(awk '
    /RESULT PASS/ {
        for (i = 1; i <= NF; ++i) {
            if ($i ~ /^compared_nodes=/) {
                split($i, a, "=");
                print a[2];
            }
        }
    }
' "${COMPARE_LOG}" | tail -n 1)
printf 'compared_nodes=%s\n' "${compared_nodes:-0}" >> "${SUMMARY}"

if (( compare_status == 0 )); then
    printf 'RESULT=PASS\n' >> "${SUMMARY}"
    log "PASS: compact repeated feeder and materialized expanded feeder are equivalent"
    cat "${SUMMARY}"
    exit 0
elif (( compare_status == 124 )); then
    printf 'RESULT=INCONCLUSIVE\nreason=feeder comparison timed out\n' \
        >> "${SUMMARY}"
    log "INCONCLUSIVE: feeder comparison timed out"
    tail -n 20 "${COMPARE_LOG}" >&2
    cat "${SUMMARY}"
    exit 2
fi

printf 'RESULT=FAIL\nreason=feeder comparison mismatch or error\n' >> "${SUMMARY}"
log "FAIL: inspect ${COMPARE_LOG}"
tail -n 40 "${COMPARE_LOG}" >&2
cat "${SUMMARY}"
exit 1
