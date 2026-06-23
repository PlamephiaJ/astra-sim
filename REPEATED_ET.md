# Repeated ET Feeder 设计与实现

## 目标

STAGE 当前会把一个 microbatch 的 Chakra DAG 深拷贝 4096 次，再分别写入
1056 个 rank 的 ET 文件。Llama 3 数据集中，每个 rank 约有 429 万个 node，
单文件约 1.2 GiB，全部 ET 约 1.3 TiB。

本实验只消除同一个 rank 内的 microbatch 重复：

- 每个 rank 保留一份完整的 base microbatch DAG；
- 运行时根据 repeat 配置虚拟恢复其余 4095 份 node；
- 不合并通信流，不减少 NS-3 packet event；
- 不修改原始 ET；
- 不进行跨 rank 去重。不同 rank 的 communicator、peer、TP/PP 位置仍保存在
  各自的 base ET 中。

修改位于独立 worktree 和分支 `repeated-et-feeder`，主工作树中的 feeder 文件
没有被修改。

## 已确认的 STAGE 重复规则

`MicroBatchReplicatorPostProcess` 生成第 `r` 个 microbatch 时使用：

```text
virtual_node_id = base_node_id + r * 1_000_000_000
```

同时执行以下变换：

- node 名称变为 `mb<r>.<base_name>`，其中 replica 0 保持原名称；
- `data_deps` 中每个 ID 增加相同 offset；
- `ctrl_deps` 中每个 ID 增加相同 offset；
- node 的类型、runtime、通信属性、tensor 属性等保持不变；
- 不创建跨 microbatch dependency。

rank 0 的实际检查结果：

```text
4,292,608 expanded nodes = 1,048 base nodes * 4,096 replicas
1,290,412,573-byte ET -> 297,185-byte compact ET
```

## Compact ET 格式

Compact ET 仍是标准 Chakra ET，里面只包含 replica 0 的完整 node。旁边增加
一个纯文本 sidecar：

```text
<rank>.et.repeat
```

内容为两个十进制整数：

```text
repeat_count node_id_offset
```

Llama 3 使用：

```text
4096 1000000000
```

如果 `.et.repeat` 不存在，`ETFeeder` 保持原有行为，读取普通 ET。这样现有
workload 不需要迁移。

## 离线转换工具

实现文件：

- `utils/compact_repeated_et.py`
- `utils/compact_llama3_1056.sh`

转换器不依赖 Python protobuf binding。它直接读取 length-delimited protobuf
frame，只解析 Chakra `Node.id` 的 wire field：

1. 原样复制 GlobalMetadata frame；
2. 顺序扫描所有 node frame；
3. 保留 `node_id < node_id_offset` 的 replica 0 node；
4. 原样写出被保留的 protobuf bytes；
5. 写出对应的 `.et.repeat` sidecar；
6. 使用临时文件和 `os.replace()`，避免中断后留下一个看似完整的 ET。

必须扫描完整的 1.3 TiB 输入，因为 STAGE readout 中 base node 和 replicated
node 并非连续存放。进度条显示的是已读取的原始输入，而不是 compact 输出
大小，包括：百分比、rank、GiB、MiB/s、elapsed 和 ETA。

批量转换：

```bash
cd /home/yuhao/workspace/astra-sim/.worktrees/repeated-feeder
utils/compact_llama3_1056.sh
```

默认输出：

```text
artifacts/llama3_70b_b4096_r1056_compact/
  llama3_70b.0.et
  llama3_70b.0.et.repeat
  ...
  llama3_70b.1055.et
  llama3_70b.1055.et.repeat
  llama3_70b.json
```

## ETFeeder 修改

修改位于独立 Chakra submodule checkout：

- `extern/graph_frontend/chakra/src/feeder_v3/et_feeder.h`
- `extern/graph_frontend/chakra/src/feeder_v3/et_feeder.cpp`

### 初始化

构造 `ETFeeder(file_path)` 时，先尝试读取 `file_path + ".repeat"`。存在时：

1. 解析 `repeat_count` 和 `repetition_id_offset`；
2. 调用 `DependancyResolver::enable_repetition()`；
3. 只扫描 compact ET 中的 base node；
4. 只为 base node 建立 `index_map` 和 base dependency graph；
5. 验证所有 base node ID 都小于 offset，否则拒绝加载。

普通 ET 没有 sidecar，`repeat_count` 保持 1，继续走原逻辑。

### 虚拟 node lookup

`lookupNode(virtual_id)` 最终调用 `get_raw_chakra_node(virtual_id)`。重复模式下：

```text
base_id      = virtual_id % repetition_id_offset
repeat_index = virtual_id / repetition_id_offset
```

Feeder 使用 `base_id` 查询文件 offset，只读取 base protobuf。对于 replica > 0，
在内存中的临时 protobuf clone 上修改：

```text
id        = virtual_id
name      = "mb<repeat_index>." + base_name
data_deps = base_data_deps + repeat_index * offset
ctrl_deps = base_ctrl_deps + repeat_index * offset
```

其他 attribute 不修改。生成后的虚拟 protobuf 仍进入原有有限大小 node cache，
不会把所有 replica 永久展开。

虚拟 ID 超出 `[0, repeat_count * offset)` 时会抛出异常。

## Dependency resolver 修改

修改文件：

- `extern/graph_frontend/chakra/src/feeder_v3/dependancy_solver.h`
- `extern/graph_frontend/chakra/src/feeder_v3/dependancy_solver.cpp`

### Base graph

`child_map_parent` 和 `parent_map_child` 只保存 base DAG，不再为 4096 个 replica
分别建立两张 `unordered_map<NodeId, unordered_set<NodeId>>`。

### Ready node

初始化时，把每个 base root 映射到所有 replica：

```text
ready_id = base_root_id + repeat_index * offset
```

ready container 从 `unordered_set` 改为 `std::set`，保持原 Workload 通过 node ID
升序发射的确定性顺序。这里只展开当前 dependency-free root ID，不展开完整 DAG。

### 完成 node 和释放 child

虚拟 node 完成时：

1. 根据 modulo 找到 base node；
2. 在 base `parent_map_child` 中找到 base children；
3. 把 child 映射回同一个 replica；
4. `remaining_parent_count` 只为已经有 parent 完成的虚拟 child 延迟创建计数；
5. 计数归零时把 child 放入 ready set；
6. child ready 后删除临时 remaining-count entry。

因此尚未触达的虚拟 node 不占 dependency state。

### 三层 dependency

原 resolver 同时保存 data、control 和两者 union 的 enabled dependency layer。
重复模式运行时只推进 enabled layer，因为它已经包含调度所需的完整 dependency
并集。data/control layer 只保留 base graph，供诊断和 sanity check 使用，避免再把
状态放大三倍。普通 ET 仍同时推进原来的三层。

## Workload 调度修改

修改文件：`astra-sim/workload/Workload.cc`。

原实现每次 callback 都会：

1. 复制整个 dependency-free `unordered_set`；
2. 再复制到 `std::set` 排序；
3. 遍历并发射可用 node。

重复 trace 初始可能有大量 root，这种每 callback 全量复制会产生明显瞬时内存和
CPU 开销。现在 resolver 本身维护有序 `std::set`，Workload 直接按 iterator 遍历；
在调用 `issue()` 前先推进 iterator，从而允许 `issue()` 安全删除当前 node 并释放
children，不再构造完整 snapshot。

注意：这项 live-set 遍历与旧 snapshot 遍历在同步完成的 metadata/invalid node 上
可能产生同一 tick 内调度顺序差异，是当前尚未宣布端到端完全等价的原因之一。

## 构建和运行

编译应在 Docker 中进行：

```bash
cd /app/astra-sim/.worktrees/repeated-feeder
build/astra_ns3/build.sh
```

运行：

```bash
examples/run_scripts/ns3/run_dragonfly_llama3_1056_repeated.sh
```

默认输出目录：

```text
outputs/ns3_dragonfly_llama3_1056_repeated/
```

运行脚本验证所有 1056 份 `.et` 和 `.et.repeat` 都存在，并把 stdout、stderr、
FCT、PFC、queue length 和 trace 输出写入独立 `RUN_DIR`。

每个 feeder 会保持一个 ET 文件打开以支持随机 lookup，因此 1056-rank 运行至少
需要超过 1056 个 file descriptor。Docker 默认 soft `nofile` 通常是 1024，会在
接近第 1024 个 rank 时导致后续 JSON open 失败，并表现为误导性的
`nlohmann::json unexpected end of input`。运行脚本现在会在启动前把 soft limit
提高到 65536；若容器 hard limit 不允许，会直接报错退出。

## 测试支持和已完成验证

新增：

- `tests/repeated_feeder_smoke.cpp`
- `utils/materialize_repeated_et.cpp`
- `utils/compare_repeated_et_feeder.cpp`
- `utils/run_repeated_et_differential.sh`
- CMake option `ASTRA_BUILD_REPEATED_FEEDER_SMOKE_TEST`

已完成：

- Python converter syntax check；
- shell script syntax check；
- C++ feeder syntax/build check；
- 完整 `AstraSimNetwork` 构建；
- rank 0 全文件转换；
- expanded/base node 数量关系验证；
- virtual ID、virtual name、ready-set、take/push-back smoke test。
- 1056-rank 受控启动 preflight：全部 feeder/Sys 初始化完成并进入 NS-3
  `QP is enabled` network setup 阶段；180 秒后由 timeout 主动终止；stderr 为空；
  容器观测峰值内存约 19.8 GiB。
- 1056-rank feeder-level differential test：为每个 rank 构造
  `compact ET + .repeat` 与 `materialized expanded ET` 两份输入，在 feeder 层完整
  走 `getNextIssuableNode()`、`freeChildrenNodes()` dependency release 流程，并逐
  issued node 比较 materialized protobuf 内容。测试配置为 `repeat_count=2`、
  `id_offset=1000000000`、`timeout_seconds=50400`，结果：

  ```text
  mode=feeder
  ranks=1056
  repeat_count=2
  id_offset=1000000000
  timeout_seconds=50400
  max_nodes_per_rank=0
  build_status=0
  materialize_status=0
  materialize_elapsed_seconds=7
  compact_et_count=1056
  compact_sidecar_count=1056
  expanded_et_count=1056
  compare_status=0
  compare_elapsed_seconds=101
  compared_nodes=2210208
  RESULT=PASS
  ```

  这个测试证明 repeated feeder 在 node materialization、issue order 和 dependency
  release 行为上与显式展开 ET 一致。复现命令：

  ```bash
  cd /app/astra-sim/.worktrees/repeated-feeder
  utils/run_repeated_et_differential.sh
  cat outputs/differential_r2/summary.txt
  ```

尚未完成：

- 完整 1056-rank compact workload 启动；
- 完整训练仿真。

因此当前实现已经完成 feeder 层等价性验证，但尚不能声称完整 NS-3 仿真结果已经
验证为 bitwise 或时序完全等价。

## 内存效果和剩余问题

已经消除的主要内存：

- expanded ET 全文件扫描和 429 万 node/rank 的 index；
- replicated node 的完整 parent/child dependency maps；
- 每次 callback 对整个 ready set 的两次复制。

仍然存在：

- ready root ID 会为所有 replica 实例化；
- 运行中实际触达的 dependency state；
- NS-3 packet/event state；
- ASTRA-sim `Statistics::operator_statistics` 当前按 virtual node ID 保存每个已执行
  node 的统计，完整运行时会持续增长；
- 总 runtime operation 数和 packet event 数没有减少，单线程仿真时间不会因为
  feeder 压缩而按 4096 倍下降。

要实现全程低内存，下一步应把 statistics 改成流式聚合或按 microbatch 回收。

## 合并策略

当前实现应继续保留在独立 worktree，直到完成等价性和 statistics 验证。若最终
采用，需要：

1. 在 Chakra submodule 中提交 feeder/resolver 修改；
2. 在 superproject 中提交新的 submodule commit pointer；
3. 提交 Workload、转换工具、测试、运行脚本和本文档；
4. 不提交 compact ET、构建目录或仿真输出。
