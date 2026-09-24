# exp1 workload DAG

Directory layout:

```text
exp1/
├── workload_spec.json       # routinely edited workload DAG
├── run.sh                   # generate/run entry point
├── run_sweep.sh             # compute-duration sweep entry point
├── fixed/                   # stable system/network/topology inputs
├── tools/                   # Python generator, verifier, and reporters
└── log/                     # all generated workloads and run outputs
```

The checked-in `workload_spec.json` uses the unified `nodes` schema:

```text
Ring AllReduce {0,1,4,5} -> COMP {0,1,4,5} --+
                                                  +-> sink
Direct AllToAll {2,3,6,7} ------------------------+
```

`collective` nodes expand to Chakra `COMM_SEND_NODE` / `COMM_RECV_NODE`
pairs. A `compute` node creates one Chakra `COMP_NODE` on each listed rank;
`cycles` is written to Chakra's `duration_micros` runtime field. Dependencies
are rank-local because Chakra uses one ET file per rank. A `join` is emitted as
an instantaneous Chakra `INVALID_NODE` on the union of its parents' ranks.

Object fields beginning with `_` are documentation metadata and are ignored by
the generator. This includes the checked-in `_schema` object.

Generate and archive the default mixed-path workload without running it:

```bash
experiments/exp1/run.sh generate mixed
```

Generate another existing routing case without changing DAG semantics:

```bash
experiments/exp1/run.sh generate path0
experiments/exp1/run.sh generate path1
experiments/exp1/run.sh generate reverse
```

Run the default four-case experiment suite:

```bash
experiments/exp1/run.sh run
```

The four cases run concurrently. Each invocation gets one timestamp directory:

```text
log/<timestamp>/
├── path0/
├── path1/
├── reverse/
├── mixed/
├── log.log
├── err.log
└── comparison.md
```

Every case owns its generated workload, runtime network configuration, NS-3
outputs, and logs, so concurrent runs do not share writable files.

To sweep compute duration, edit the range and parallelism constants at the top of
`run_sweep.sh` and run it:

```bash
experiments/exp1/run_sweep.sh
```

`DURATION_PARALLELISM` duration points run concurrently, and the four routing
cases inside every point also run concurrently. Peak simulator process count is
`DURATION_PARALLELISM * 4`. Results are archived as
`log/<timestamp>/cycles_<N>/<case>/`, with per-point `log.log`, `err.log`, and
`comparison.md`, plus top-level `log.log`, `err.log`, and `sweep_summary.md`.

If the host Python cannot load the bundled Chakra protobuf, run generation in
the project container:

```bash
docker exec -w /app/astra-sim \
  -e PYTHONPATH=extern/graph_frontend/chakra/build/lib \
  astra-sim-latest bash experiments/exp1/run.sh generate mixed
```
