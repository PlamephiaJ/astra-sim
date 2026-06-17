CONTAINER_NAME=astra-sim-latest

if docker ps --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
  docker attach "$CONTAINER_NAME"
elif docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
  docker start -ai "$CONTAINER_NAME"
else
  docker run -it --name "$CONTAINER_NAME" \
    --shm-size=8g \
    -v "$PWD":/app/astra-sim \
    astra-sim:latest bash
fi