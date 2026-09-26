# exp2：可选择的 flow-level routing

`exp2` 的 workload 不为 collective 或展开后的 P2P flow 预设路径。所有 RDMA QP
都以 `label=-1 / route=default` 进入 NS-3，具体路径由运行时选择的 backend routing
strategy 决定。目前支持标准 `ecmp` 和小场景下的 `ugal_l`。

## 选择策略

```bash
# 默认：标准 NS-3 shortest-path ECMP
experiments/exp2/run.sh run
experiments/exp2/run.sh run ecmp

# flow-level UGAL-L
experiments/exp2/run.sh run ugal_l
```

runner 把 case 名直接传给 `--routing-mode`。默认仍为 `ecmp`，因此已有 baseline
行为不变。

## ECMP

NS-3 对每条 P2P flow 使用稳定 tuple：

```text
source IP, destination IP, source port, destination port
```

在 equal-cost next hops 中执行 hash 选择：

```text
next_hop = equal_cost_next_hops[hash(flow_tuple) % path_count]
```

它不读取实时队列或链路负载。同一 flow 的 packet 保持同一 next hop，ACK/NACK
方向使用反向 tuple 独立选择。

## UGAL-L

`ugal_l` 在一条 flow 第一次到达入口交换机时读取两个本地 egress queue 的字节数，
比较：

```text
minimal_cost    = minimal_queue_bytes    * minimal_hops
nonminimal_cost = nonminimal_queue_bytes * nonminimal_hops + bias
```

仅当 `nonminimal_cost < minimal_cost` 时选择 2-hop non-minimal path；相等时选择
minimal path。决定随后按 flow tuple 固定，所以它是适配 exp2 flow-level 目标的
UGAL-L：不同 P2P flow 可以重路由，但同一 flow 内不做 packet spraying。

data 与 ACK/NACK 方向分别作出首包决定。每次决定会把相对 shortest path 增加的
传播与串行化延迟反馈给源 QP；QP 合并正反两个方向，按实际闭环 RTT 更新：

```text
actual_rtt = shortest_rtt + forward_extra_rtt + reverse_extra_rtt
window     = ceil(shortest_window * actual_rtt / shortest_rtt)
```

因此启用 `HAS_WIN` 时，选择 long path 的 flow 不再继续使用 shortest-path BDP。
`NS3_UGAL_QP_UPDATE` 日志给出每次更新后的 `base_rtt_ns` 和 `window_bytes`。

`EXP2_UGAL_BIAS_BYTES` 控制额外的 non-minimal cost，默认为 `0`；值越大，越偏向
1-hop minimal path：

```bash
EXP2_UGAL_BIAS_BYTES=65536 experiments/exp2/run.sh run ugal_l
```

可用 `EXP2_HAS_WIN=0` 关闭 exp2 runtime config 中的 RDMA window，作为控制实验；
默认值为 `1`。队列监控结束时间可用 `EXP2_QLEN_MON_END_NS` 覆盖，默认 100 ms。

## Topology 和延迟

```text
short: switch 8 -------- switch 9       1 hop
long:  switch 8 -- 10 -- switch 9       2 hops
```

标准 ECMP 的路由表由 shortest-hop BFS 生成，因此只会安装 1-hop short path。
UGAL-L 额外从 [fixed/ugal_l_routes.json](fixed/ugal_l_routes.json) 加载 2-hop path；
该配置与 workload 分离。

long path 两段的传播延迟可独立设置：

```bash
EXP2_LONG_HOP1_DELAY=0.050ms \
EXP2_LONG_HOP2_DELAY=0.150ms \
  experiments/exp2/run.sh run ugal_l
```

两段默认均为 `0.100ms`。延迟只改变 runtime topology；当前 UGAL-L 决策成本只使用
本地 queue occupancy 和 hop count，不把传播延迟加入成本。

## Workload 与扩展接口

[workload_spec.json](workload_spec.json) 只描述 DAG、collective 和通信量，不包含
`routing_label` 或 `flow_routing`。生成器仍记录稳定 `flow_id`，但
`routing_label=null`，由 NS-3 策略接管。

后续增加其他方法时，保持 workload 不变，并在三处扩展：

1. `FlowRoutingStrategy` 增加策略枚举及其 per-flow 状态；
2. `AstraSimNetwork.cc` 的 `--routing-mode` dispatch 初始化策略；
3. `run.sh` 的 case 白名单和对应策略配置。

每种策略都输出首个 flow 决策记录：ECMP 使用 `NS3_ECMP`，UGAL-L 使用
`NS3_UGAL`（包含两侧 queue、cost、decision、路径附加 RTT 和 next hop）；UGAL-L
另用 `NS3_UGAL_QP_UPDATE` 记录实际 RTT/BDP 更新。

## 生成与验证

```bash
# 只生成无 routing label 的 workload
experiments/exp2/run.sh generate

# 编译并运行默认 ECMP 回归测试
experiments/exp2/run.sh test

# 验证最近一次归档
experiments/exp2/run.sh verify ecmp
experiments/exp2/run.sh verify ugal_l
```

如果宿主 Python 无法加载 Chakra protobuf，可以在项目容器中执行同样命令：

