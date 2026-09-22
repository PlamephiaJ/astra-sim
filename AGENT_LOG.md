# AGENT_LOG

本文件用于持续记录仓库中的重要修改。后续每次更新均按以下格式追加：

```text
## 日期：YYYY-MM-DD

### 修改内容：修改主题

修改背景、设计、涉及文件、验证方法和结果。
```

---

## 日期：2026-09-22

### 修改内容：为 exp1 实现显式的逐消息 RDMA 路径固定机制

#### 修改目标

为 `experiments/exp1` 中的 8-rank Chakra/ASTRA-sim NS-3 实验实现最小的
flow/message-level path pinning：

```text
path_id = 0 -> 8 -> 10 -> 9（短路径）
path_id = 1 -> 8 -> 11 -> 9（长路径）
```

反方向使用对应路径：

```text
path_id = 0 -> 9 -> 10 -> 8
path_id = 1 -> 9 -> 11 -> 8
```

本次修改只实现显式路径选择，不包含以下功能：

- slack estimator；
- 动态路由决策；
- 拥塞感知策略；
- ordering lanes；
- path migration；
- packet spraying；
- 两条以上路径的复杂策略。

#### 原有 ECMP 行为分析

NS-3 交换机的转发逻辑位于：

```text
extern/network_backend/ns-3/src/point-to-point/model/switch-node.cc
```

`SwitchNode::GetOutDev()` 使用以下 5-tuple 计算 ECMP hash：

```text
source IP
destination IP
source port
destination port
```

交换机会为每个 packet 重新计算 hash，但同一个 RDMA QP 的所有 packet 具有相同
的 5-tuple，因此原有 ECMP 已经是 flow-stable，不会在同一个 flow 内进行 packet
spraying。

路由表由以下代码安装：

```text
extern/network_backend/ns-3/scratch/common.h
```

`SetRoutingEntries()` 会为每个 destination 安装一个 next-hop vector。本次修改在安装
前按照 next-hop node ID 排序，使 `path_id` 到 next hop 的映射稳定且可重复：

```text
index 0 -> switch 10
index 1 -> switch 11
```

#### path_id 的传递路径

最终的数据路径为：

```text
workload_spec.json 中的 path_id
    -> Chakra SEND/RECV 自定义 attribute
    -> Workload::issue_send_comm()
    -> sim_request.path_id
    -> AstraSimNetwork::sim_send()
    -> send_flow(..., path_id)
    -> RDMA flow 的 destination port
    -> SwitchNode::GetOutDev()
```

在两份 `sim_request` 定义中增加了：

```cpp
int32_t path_id = -1;
```

其中：

```text
-1 -> 不指定路径，保持原有 ECMP 行为
 0 -> 固定到 path 0
 1 -> 固定到 path 1
```

使用默认值 `-1` 可以兼容没有设置 `path_id` 的 native collective 和其他已有调用方。

Chakra 自定义属性通过以下代码读取：

```cpp
node->get_attr<int32_t>("path_id", -1)
```

Chakra node 本身支持任意命名 attribute，因此不需要修改 protobuf schema。

#### RDMA flow-level 编码

新增文件：

```text
extern/network_backend/ns-3/src/point-to-point/model/rdma-path-selection.h
```

路径选择编码到 RDMA UDP destination port：

```text
destination port 100 -> 未固定路径，使用原有 ECMP
destination port 101 -> path_id 0
destination port 102 -> path_id 1
```

`send_flow()` 在创建 RDMA QP 时只选择一次 destination port。同一个 QP 生成的所有
data packet 都携带相同端口，因此整个 ASTRA SEND/message 会保持在同一路径上。

ACK/NACK 会交换原始 flow 的 source/destination port。路径解码逻辑同时检查 source
port 和 destination port，因此反向 ACK/NACK 也会使用对应的中间路径。

当交换机存在多个等 hop next hop 时：

```cpp
next_hop_index = path_id;
```

当交换机只有一个 next hop 时，不执行额外绕路，继续使用唯一的最短路径。因此同侧
通信不会因为 `path_id` 被错误地发送到 switch 10 或 switch 11。

未指定 `path_id` 时，原有 ECMP hash 逻辑保持不变。

#### flow pinning 断言和日志

`SwitchNode` 会记录每个显式固定的 5-tuple 首次选择的 output device。该 flow 的每个
后续 packet 都会检查 output device 是否相同。如果同一个 flow 的 packet 改变路径，
NS-3 assertion 会立即终止模拟。

新增的精简日志格式为：

```text
ASTRA_PATH src=1 dst=4 tag=2 bytes=16777216 path=0 sport=10000 dport=101
NS3_PATH switch=8 src_ip=11.0.1.1 dst_ip=11.0.4.1 sport=10000 dport=101 path=0 out_dev=5 next_hop=10
```

交换机只为一个 pinned flow 的首次 forwarding decision 输出日志，不会为每个 packet
无条件打印。

日志开关位于：

```text
experiments/exp1/ns3_config.txt
```

当前配置：

```text
ENABLE_PATH_LOG 1
```

改为 `0` 即可关闭路径日志。

#### workload 配置和生成器修改

`experiments/exp1/workload_spec.json` 当前配置为：

```json
{
  "name": "ar_0",
  "type": "allreduce",
  "algorithm": "ring",
  "ranks": [0, 1, 4, 5],
  "bytes": 67108864,
  "path_id": 0
}
```

以及：

```json
{
  "name": "a2a_0",
  "type": "alltoall",
  "algorithm": "direct",
  "ranks": [2, 3, 6, 7],
  "bytes": 67108864,
  "path_id": 1
}
```

`generate_workload.py` 会验证 `path_id` 只能是 0 或 1，并把 collective 的
`path_id` 自动复制到每一个 SEND 和对应的 RECV node。

默认 mixed workload 包含：

```text
24 个 path_id 0 的 SEND：Ring AllReduce
12 个 path_id 1 的 SEND：Direct All-to-All
```

生成器还支持以下 oracle/regression test 参数：

```text
--path-override 0
--path-override 1
--no-path-pinning
```

#### 统一实验入口

所有 exp1 操作统一通过以下脚本进入：

```text
experiments/exp1/run.sh
```

支持的命令：

```bash
cd /app/astra-sim

# 编译 NS-3 backend
experiments/exp1/run.sh build

# 只生成 workload
experiments/exp1/run.sh generate mixed

# 运行单个实验并自动验证
experiments/exp1/run.sh mixed
experiments/exp1/run.sh path0
experiments/exp1/run.sh path1
experiments/exp1/run.sh ecmp

# 验证已有日志
experiments/exp1/run.sh verify mixed

# 编译并运行全部测试
experiments/exp1/run.sh test
```

`test` 会执行以下四个 case：

```text
path0
path1
mixed
ecmp
```

`run.sh` 会为每个 case 生成独立的 runtime network config 和输出目录：

```text
experiments/exp1/ns3_output/<case>/
```

这些运行产物目录已加入 `.gitignore`。

新增验证脚本：

```text
experiments/exp1/verify_paths.py
```

验证内容包括：

1. 每个 ASTRA SEND 携带的 `path_id` 是否正确；
2. 跨侧 flow 在正向和反向选择的 middle switch 是否正确；
3. 是否实际存在跨侧 flow；
4. ranks 0 到 7 是否全部 finish；
5. 未固定路径的 ECMP case 是否没有显式 `NS3_PATH` 记录。

#### 修改文件

ASTRA-sim metadata 传递：

```text
astra-sim/common/Common.hh
astra-sim/system/Common.hh
astra-sim/workload/Workload.cc
astra-sim/network_frontend/ns3/AstraSimNetwork.cc
astra-sim/network_frontend/ns3/entry.h
```

NS-3 flow 编码和交换机转发：

```text
extern/network_backend/ns-3/src/point-to-point/model/rdma-path-selection.h
extern/network_backend/ns-3/src/point-to-point/model/switch-node.h
extern/network_backend/ns-3/src/point-to-point/model/switch-node.cc
extern/network_backend/ns-3/src/point-to-point/CMakeLists.txt
extern/network_backend/ns-3/scratch/common.h
```

实验生成、入口和验证：

```text
experiments/exp1/generate_workload.py
experiments/exp1/workload_spec.json
experiments/exp1/ns3_config.txt
experiments/exp1/run.sh
experiments/exp1/verify_paths.py
.gitignore
```

#### 编译与验证结果

宿主机中的 CMake cache 是基于 `/app/astra-sim` 创建的，因此最终编译和模拟在已有的
`astra-sim-latest` 容器中完成。

通过统一入口执行新版 CMake/ns-3 构建：

```bash
cd /app/astra-sim
experiments/exp1/run.sh build
```

编译成功，生成：

```text
extern/network_backend/ns-3/build/scratch/ns3.42-AstraSimNetwork-default
```

测试结果：

| Case | 预期行为 | 结果 |
|---|---|---|
| `path0` | 所有跨侧 flow 使用 switch 10 | 通过 |
| `path1` | 所有跨侧 flow 使用 switch 11 | 通过 |
| `mixed` | Ring 使用 10，All-to-All 使用 11 | 通过 |
| `ecmp` | 不显式固定路径，继续使用原有 ECMP | 通过 |

每个 case 均包含：

```text
36 个 ASTRA SEND flow
20 个跨侧 SEND flow
8/8 ranks finish
```

每个显式路径 case 均验证了 40 条 multipath forwarding 记录，覆盖正向 data 和反向
ACK/NACK。完整模拟期间逐 packet 的 flow-pinning assertion 均未触发，说明同一
SEND/flow 的所有 packet 始终使用同一路径。

未设置 `path_id` 的 `ecmp` case 产生 0 条显式 `NS3_PATH` 记录，并且 8 个 rank
全部完成，验证了原有 ECMP 行为仍然可用。

全部测试结束后重新生成了默认 mixed workload，当前 ET 文件与
`workload_spec.json` 一致，没有停留在全 path 0 或全 path 1 的临时测试状态。

---

## 日期：2026-09-22

### 修改内容：为 exp1 新增 reverse 路径分配实验

#### 实验定义

新增 `reverse` case，把默认 mixed case 的路径分配反转：

```text
Ring AllReduce [0,1,4,5] -> path_id 1 -> long path -> switch 11
Direct All-to-All [2,3,6,7] -> path_id 0 -> short path -> switch 10
```

#### 实现方式

在 `experiments/exp1/generate_workload.py` 中新增：

```text
--reverse-paths
```

该参数不会硬编码 rank group 或 collective 名称，而是反转
`workload_spec.json` 中已有的显式路径：

```text
path_id 0 -> path_id 1
path_id 1 -> path_id 0
```

如果某个 collective 没有配置 `path_id`，`--reverse-paths` 会直接报错，避免静默
生成含义不明确的 workload。

在 `experiments/exp1/run.sh` 中新增统一入口：

```bash
cd /app/astra-sim

# 生成 reverse workload
experiments/exp1/run.sh generate reverse

# 运行并自动验证 reverse 实验
experiments/exp1/run.sh reverse

# 等价写法
experiments/exp1/run.sh run reverse

# 只验证已有 reverse 日志
experiments/exp1/run.sh verify reverse
```

`reverse` 也已加入 `experiments/exp1/run.sh test`，因此完整测试现在依次覆盖：

```text
path0
path1
mixed
reverse
ecmp
```

`experiments/exp1/verify_paths.py` 增加了 `reverse` 期望值：

```text
Ring ranks      -> path 1 / next hop 11
All-to-All ranks -> path 0 / next hop 10
```

#### 验证结果

执行：

```bash
cd /app/astra-sim
experiments/exp1/run.sh reverse
```

验证通过：

```text
24 个 Ring SEND 使用 path_id 1
12 个 All-to-All SEND 使用 path_id 0
36 个 ASTRA SEND flow
20 个跨侧 SEND flow
40 条正向 data/反向 ACK-NACK forwarding 记录
8/8 ranks finish
```

交换机日志确认：

```text
Ring 跨侧流量：path=1, next_hop=11
All-to-All 跨侧流量：path=0, next_hop=10
```

完整模拟期间 flow-pinning assertion 未触发，同一个 SEND 的 packet 没有发生路径
切换或 packet spraying。

reverse 测试完成后，已重新生成默认 mixed workload，因此当前 ET 文件仍保持：

```text
Ring -> path 0
All-to-All -> path 1
```

---

## 日期：2026-09-22

### 修改内容：为 exp1 增加按时间戳保存的实验归档

`experiments/exp1/run.sh` 现在每执行一次仿真实验，都会创建独立目录：

```text
experiments/exp1/log/<YYYYMMDD_HHMMSS>_<case>/
```

例如本次验证生成：

```text
experiments/exp1/log/20260922_232047_reverse/
```

每个实验目录保存以下内容：

```text
run.log                         ASTRA-sim/NS-3 输出和路径验证结果
generate.log                    workload 生成日志
fct.txt                         NS-3 flow completion time 输出
pfc.txt                         NS-3 PFC 输出
qlen.txt                        NS-3 queue length 输出
trace.tr                        NS-3 trace 输出
config/manifest.txt             case、命令、二进制路径和 Git commit
config/workload_spec.source.json  原始 workload 配置
config/workload_spec.resolved.json 应用 mixed/reverse/path0/path1/ecmp 后的最终配置
config/ns3_config.source.txt    原始 NS-3 配置
config/ns3_config.runtime.txt   本次运行实际使用的 NS-3 配置
config/system.json              ASTRA-sim system 配置快照
config/logical_topology.json    logical topology 配置快照
config/remote_memory.json       remote memory 配置快照
config/physical_topology.txt    physical topology 配置快照
config/flow.txt                 flow 配置快照
config/trace.txt                trace 配置快照
config/comm_group.json          communicator 元数据快照
config/workload/workload.*.et   本次运行使用的 8 个 Chakra ET 文件
```

新增 `--resolved-spec-output` generator 参数，用于记录所有命令行路径覆盖之后的
最终 workload 配置，确保 `reverse`、`path0`、`path1` 和 `ecmp` 都可以准确复现。

`verify` 命令会自动选择相同 case 的最新时间戳归档；如果没有新式归档，则继续
兼容旧的 `ns3_output/<case>/run.log`：

```bash
experiments/exp1/run.sh verify reverse
```

`experiments/exp1/log/` 已加入 `.gitignore`，避免大量实验输出进入 Git。
如果同一秒内出现同名 case，脚本会报错并保留已有归档，不会覆盖旧实验。

#### 验证结果

实际执行一次 `reverse` 实验，确认归档目录包含全部配置快照、8 个 ET 文件、
生成日志、仿真日志和 4 个 NS-3 输出文件。自动验证最新归档通过：

```text
PATH VERIFY PASSED case=reverse flows=36 cross_side_flows=20 forwarding_records=40 finished_ranks=8
```

验证完成后已重新生成默认 `mixed` workload，当前工作目录中的 ET 文件仍为：

```text
Ring -> path 0
All-to-All -> path 1
```

---

## 日期：2026-09-22

### 修改内容：默认运行四种路径配置并生成结果对比报告

无参数执行 `experiments/exp1/run.sh`，或执行不带 case 的
`experiments/exp1/run.sh run`，现在会依次运行以下四种配置：

```text
path0    Ring -> path 0，All-to-All -> path 0
path1    Ring -> path 1，All-to-All -> path 1
reverse  Ring -> path 1，All-to-All -> path 0
mixed    Ring -> path 0，All-to-All -> path 1
```

默认四组实验不包含用于兼容性验证的 `ecmp`。原有的单 case 入口保持不变，例如：

```bash
experiments/exp1/run.sh run reverse
experiments/exp1/run.sh mixed
```

每组实验仍分别保存到秒级时间戳目录。四组全部完成并通过路径验证后，新增的
`experiments/exp1/compare_results.py` 会在 `log` 根目录生成本轮对比报告：

```text
experiments/exp1/log/comparison_<YYYYMMDD_HHMMSS>.md
```

报告包含：

- 每种 case 的 Ring AllReduce 和 Direct All-to-All 路径分配；
- 最大 rank Wall time，作为整组实验完成周期；
- 相对最快配置的时间差百分比；
- 平均 Wall time 和平均 Comm time；
- flow、跨侧 flow、forwarding record 和完成 rank 数量；
- 8 个 rank 的逐 rank Wall time；
- 本轮四个实验归档目录的链接。

四组的执行顺序把 `mixed` 放在最后，因此默认批量运行结束后，工作目录中的
Chakra ET 自动保持仓库默认配置，不需要额外恢复。

#### 验证结果

实际执行：

```bash
cd /app/astra-sim
experiments/exp1/run.sh
```

四种配置均通过路径校验，报告生成于：

```text
experiments/exp1/log/comparison_20260922_232749.md
```

本轮最大 rank Wall time 对比：

```text
path0    16,957,826 cycles
path1    16,968,533 cycles
mixed    13,540,549 cycles
reverse  13,301,332 cycles
```

本轮 `reverse` 最快；相对 `mixed` 的完成周期约低 1.77%。四组均记录 36 个
ASTRA flow、20 个跨侧 flow、40 条 forwarding record，并完成 8/8 ranks。
