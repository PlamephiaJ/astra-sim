#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR=$(realpath "$(dirname "$(realpath "$0")")/..")
if [[ -z "${ORIGINAL_PROJECT_DIR:-}" ]]; then
    ORIGINAL_PROJECT_DIR=$(realpath "${PROJECT_DIR}/../..")
fi
SOURCE_DATASET_DIR="${ORIGINAL_PROJECT_DIR}/datasets/stage/generated/llama3_70b_b4096_r1056"
SOURCE_PREFIX="${SOURCE_PREFIX:-${SOURCE_DATASET_DIR}/llama3_70b}"
OUTPUT_DIR="${OUTPUT_DIR:-${PROJECT_DIR}/artifacts/llama3_70b_b4096_r1056_compact}"

mkdir -p "${OUTPUT_DIR}"
python3 "${PROJECT_DIR}/utils/compact_repeated_et.py" \
    --input-prefix "${SOURCE_PREFIX}" \
    --output-prefix "${OUTPUT_DIR}/llama3_70b" \
    --ranks 1056 \
    --repeat-count 4096 \
    --id-offset 1000000000

cp "${SOURCE_DATASET_DIR}/llama3_70b.json" \
    "${OUTPUT_DIR}/llama3_70b.json"
