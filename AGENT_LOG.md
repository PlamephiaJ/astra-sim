## Fix Chakra Mixtral ET generation for ASTRA-sim

### Context

While converting the MLSys26 Mixtral-8x7B NeMo Chakra traces under:

```text
datasets/chakra/mlsys26/
```

the generated ET files initially failed ASTRA-sim consumption because some nodes had:

```text
ctrlDeps: ["0"]
```

but the output ET appeared to have no explicit `Node 0` when inspected with `chakra_jsonizer`.

After investigation, this was determined not to be a schema mismatch. The `et_def.proto` used by `datasets/chakra` and the ASTRA-sim Chakra frontend were confirmed to be identical.

### Build workaround

The `mlsys26` branch only contains:

```text
schema/protobuf/et_def.proto
```

so `et_def_pb2.py` was generated manually:

```bash
python -m grpc_tools.protoc \
  -I . \
  --python_out=. \
  schema/protobuf/et_def.proto
```

`setup.py` was also temporarily simplified to avoid the broken `build_grpc` setuptools path, since `et_def_pb2.py` is generated manually.

### Runtime dependency fix

`chakra_trace_link` required `et_replay`, which was present locally under:

```text
datasets/chakra/param/et_replay
```

Installed it into the active Chakra virtualenv:

```bash
uv pip install -e param/et_replay
```

This fixed:

```text
ModuleNotFoundError: No module named 'et_replay'
```

### Converter bug fix

The real ET graph issue was in:

```text
src/converter/pytorch_converter.py
```

Original `remove_dangling_nodes()` only considered `data_deps` when deciding whether a node was dangling. This incorrectly allowed nodes used only as `ctrl_deps` parents to be removed.

That caused the PyTorch profiler process root node, `Node 0`, to be removed from the final ET while thread root nodes still retained:

```text
ctrlDeps: ["0"]
```

Final fix: make dangling-node detection aware of both `data_deps` and `ctrl_deps`.

Final `remove_dangling_nodes()` logic:

```python
def remove_dangling_nodes(self, protobuf_node_map: Dict[int, ChakraNode]) -> Dict[int, ChakraNode]:
    """
    Remove any dangling nodes from the protobuf_node_map dictionary.

    A node is dangling only if:
    1. no other node depends on it via data_deps or ctrl_deps, and
    2. it itself has no data_deps and no ctrl_deps.

    The original implementation only considered data_deps, which can incorrectly
    remove nodes that are valid control-dependency parents, such as node 0 in
    PyTorch execution traces.
    """
    parent_ids = set()
    for node in protobuf_node_map.values():
        parent_ids.update(node.data_deps)
        parent_ids.update(node.ctrl_deps)

    dangling_nodes = [
        node_id
        for node_id, node in protobuf_node_map.items()
        if node_id not in parent_ids and not node.data_deps and not node.ctrl_deps
    ]

    for node_id in dangling_nodes:
        del protobuf_node_map[node_id]

    if dangling_nodes:
        logging.debug(f"Identified and removed {len(dangling_nodes)} dangling nodes:")
        for node_id in dangling_nodes:
            logging.debug(f" - Node ID {node_id}")

    return protobuf_node_map
```

### Important note about jsonizer output

When inspecting ET with `chakra_jsonizer`, `Node 0` may appear without an explicit `"id": "0"` field because protobuf JSON output omits default-valued fields. Since integer field default is `0`, this is expected.

Valid output begins like:

```json
}{
  "name": "[pytorch|profiler|execution_trace|process]",
  "type": "COMP_NODE",
  ...
}{
  "id": "1",
  "name": "[pytorch|profiler|execution_trace|thread]",
  "ctrlDeps": [
    "0"
  ]
}
```

The first node above is `Node 0`, even though `"id": "0"` is omitted.

### Final validation

Rank 0 was regenerated and inspected with:

```bash
chakra_converter PyTorch \
  --input mlsys26/traces/linked/rank0_linked.json \
  --output mlsys26/traces/et/chakra_trace.0.et

chakra_jsonizer \
  --input_filename mlsys26/traces/et/chakra_trace.0.et \
  --output_filename /tmp/chakra_trace.0.final.json

head -80 /tmp/chakra_trace.0.final.json
```

Validation showed:

```text
Node 0: [pytorch|profiler|execution_trace|process]
Node 1: [pytorch|profiler|execution_trace|thread], ctrlDeps = ["0"]
```

This means `ctrlDeps -> 0` is no longer dangling.

### Final generation command

Regenerate all Mixtral ET files with:

```bash
cd /home/yuhao/workspace/astra-sim/datasets/chakra
source .venv/bin/activate

rm -rf mlsys26/traces/et
bash mlsys26/convert_traces.sh 4
```

Generated ET files:

```text
mlsys26/traces/et/chakra_trace.0.et
mlsys26/traces/et/chakra_trace.1.et
...
mlsys26/traces/et/chakra_trace.7.et
```

ASTRA-sim workload prefix:

```text
/home/yuhao/workspace/astra-sim/datasets/chakra/mlsys26/traces/et/chakra_trace
```

Do not pass `chakra_trace.0.et` directly; ASTRA-sim expects the prefix and will append `.{rank}.et`.

## ASTRA-sim simulator fix for real Chakra ET consumption

### Context

After the full 8-rank ET directory was available at:

```text
datasets/chakra/mlsys26/traces/et
```

the analytical backend was tested with the workload prefix:

```text
/app/astra-sim/datasets/chakra/mlsys26/traces/et/chakra_trace
```

The first run opened the ET files and initialized the 8-NPU topology, but failed in statistics processing with:

```text
Invalid node_type, node.id=2, node.type=1
```

In Chakra, `node.type=1` is `METADATA_NODE`. This is a valid node type in the protobuf schema and `Workload.cc` already has explicit handling for `METADATA_NODE`. The failure was therefore in ASTRA-sim's statistics layer, not in the generated ET data.

### Simulator fixes

Updated:

```text
astra-sim/workload/Statistics.cc
```

Fixes:

1. Treat `ChakraNodeType::METADATA_NODE` like `INVALID_NODE` in `OperatorStatistics::get_operator_type()`.
   This matches the current workload behavior where metadata nodes are consumed for dependency/proccess-group bookkeeping and then skipped.

2. Make `extract_comp_comm_overlap()` ignore non-GPU/non-COMM types instead of throwing.
   Real system traces can contain CPU and metadata/invalid bookkeeping nodes, so overlap extraction should only consider the GPU and COMM intervals it actually uses.

3. Clamp compute-communication overlap at zero.
   The previous unsigned arithmetic could underflow when `GPU time + Comm time < Wall time`, producing a huge bogus overlap value.

### Docker build and validation

Use the Docker environment from `launch_docker.sh`, because the existing build cache and dependencies are rooted at:

```text
/app/astra-sim
```

The host environment did not have the same Protobuf setup, so native rebuilds outside Docker were not reliable.

Build command used inside Docker:

```bash
cd /app/astra-sim
build/astra_analytical/build.sh -t congestion_aware
```

Smoke test command:

```bash
mkdir -p /tmp/astra-log/log
cd /tmp/astra-log

/app/astra-sim/build/astra_analytical/build/bin/AstraSim_Analytical_Congestion_Aware \
  --workload-configuration=/app/astra-sim/examples/workload/microbenchmarks/all_reduce/8npus_1MB/all_reduce \
  --system-configuration=/app/astra-sim/examples/system/native_collectives/HGX-H100-validated.json \
  --remote-memory-configuration=/app/astra-sim/examples/remote_memory/analytical/no_memory_expansion.json \
  --network-configuration=/app/astra-sim/examples/network/analytical/HGX-H100-validated.yml
```

Full Mixtral ET validation command:

```bash
mkdir -p /tmp/astra-log/log
cd /tmp/astra-log

/app/astra-sim/build/astra_analytical/build/bin/AstraSim_Analytical_Congestion_Aware \
  --workload-configuration=/app/astra-sim/datasets/chakra/mlsys26/traces/et/chakra_trace \
  --system-configuration=/app/astra-sim/examples/system/native_collectives/HGX-H100-validated.json \
  --remote-memory-configuration=/app/astra-sim/examples/remote_memory/analytical/no_memory_expansion.json \
  --network-configuration=/app/astra-sim/examples/network/analytical/HGX-H100-validated.yml
```

Final validation result:

```text
sys[0] finished, 4995551288 cycles
sys[1] finished, 5000876286 cycles
sys[2] finished, 5000874286 cycles
sys[3] finished, 4992532286 cycles
sys[4] finished, 4986685286 cycles
sys[5] finished, 5004958286 cycles
sys[6] finished, 4985627290 cycles
sys[7] finished, 5001823285 cycles
```

The final Docker run exited with status 0. All 8 ranks finished, statistics post-processing completed, and there was no `METADATA_NODE` assertion, overlap extraction exception, or unsigned overlap underflow.
