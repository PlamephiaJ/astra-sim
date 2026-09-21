#!/usr/bin/env bash
set -e

CONTAINER_NAME="astra-sim-latest"
IMAGE_NAME="astra-sim:latest"
MOUNT_PATH="/app/astra-sim"

if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    echo "容器 $CONTAINER_NAME 已存在，正在进入……"

    if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME")" = "true" ]; then
        docker exec -it "$CONTAINER_NAME" bash
    else
        docker start -ai "$CONTAINER_NAME"
    fi
else
    echo "容器 $CONTAINER_NAME 不存在，正在创建并启动……"

    docker run -it \
        --name "$CONTAINER_NAME" \
        --shm-size=8g \
        --mount type=bind,source="$(pwd)",target="$MOUNT_PATH" \
        "$IMAGE_NAME" \
        bash
fi