# NS-3 backend 调用链阅读指南

本文对应 worktree：

```text
.worktrees/repeated-feeder
```

目标是把下面这段启动命令之后发生的事情，按接近 Python 调用栈的方式梳理清楚：

```bash
"${ASTRA_SIM_BINARY}" \
    --workload-configuration="${WORKLOAD}" \
    --comm-group-configuration="${COMM_GROUP}" \
    --system-configuration="${SYSTEM}" \
    --network-configuration="${RUN_NETWORK}" \
    --logical-topology-configuration="${LOGICAL_TOPOLOGY}" \
    --remote-memory-configuration="${MEMORY}" \
    > "${RUN_DIR}/stdout.log" \
    2> "${RUN_DIR}/stderr.log"
```

最重要的入口文件是：

```text
astra-sim/network_frontend/ns3/AstraSimNetwork.cc
```

`main()` 就在这里。

## 0. 启动脚本先做了什么

脚本：

```text
examples/run_scripts/ns3/run_dragonfly_llama3_1056_repeated.sh
```

它不是仿真逻辑本身，只负责准备输入路径和输出路径。

关键变量：

```bash
PATTERN_DIR=artifacts/llama3_70b_b4096_r1056_compact
WORKLOAD="${PATTERN_DIR}/llama3_70b"
COMM_GROUP="${PATTERN_DIR}/llama3_70b.json"
SYSTEM="examples/system/native_collectives/Dragonfly_3D_Ring.json"
NETWORK="examples/network/ns3/dragonfly_4_8_4_33_network.txt"
LOGICAL_TOPOLOGY="examples/network/ns3/dragonfly_4_8_4_33_logical.json"
MEMORY="examples/remote_memory/analytical/no_memory_expansion.json"
RUN_DIR="outputs/ns3_dragonfly_llama3_1056_repeated"
```

注意 `WORKLOAD` 不是一个真实文件，而是前缀。rank `r` 实际读：

```text
${WORKLOAD}.${r}.et
${WORKLOAD}.${r}.et.repeat
```

也就是：

```text
artifacts/llama3_70b_b4096_r1056_compact/llama3_70b.0.et
artifacts/llama3_70b_b4096_r1056_compact/llama3_70b.0.et.repeat
...
artifacts/llama3_70b_b4096_r1056_compact/llama3_70b.1055.et
artifacts/llama3_70b_b4096_r1056_compact/llama3_70b.1055.et.repeat
```

脚本还会基于 `examples/network/ns3/dragonfly_4_8_4_33_network.txt` 生成：

```text
${RUN_DIR}/network.txt
```

并把 NS-3 的几个文件输出路径改到 `RUN_DIR`：

```text
TRACE_OUTPUT_FILE -> ${RUN_DIR}/trace.tr
FCT_OUTPUT_FILE   -> ${RUN_DIR}/fct.txt
PFC_OUTPUT_FILE   -> ${RUN_DIR}/pfc.txt
QLEN_MON_FILE     -> ${RUN_DIR}/qlen.txt
```

所以这个 run 的主要产物是：

```text
outputs/ns3_dragonfly_llama3_1056_repeated/
  stdout.log
  stderr.log
  network.txt
  fct.txt
  pfc.txt
  qlen.txt
  trace.tr
```

此外，ASTRA 的 spdlog 默认还会在当前工作目录下创建：

```text
extern/network_backend/ns-3/build/scratch/log/
  log.log
  err.log
```

因为脚本在执行 binary 前 `cd "${NS3_DIR}/build/scratch"`。

## 1. 顶层调用链总览

从 binary 启动到进入 NS-3 event loop，大致是：

```text
AstraSimNetwork.cc::main()
  parse_args()
  LoggerFactory::init()
  read_logical_topo_config()
  new AnalyticalRemoteMemory(memory JSON)
  for each rank:
    new ASTRASimNetwork(rank)
    new Sys(rank, ...)
      Sys::initialize_sys(system JSON)
      new Workload(rank ET prefix, comm group JSON)
        new ETFeeder(rank .et)
          read .et.repeat if present
          scan compact ET
          build dependency graph
        Workload::initialize_comm_groups(comm group JSON)
  setup_ns3_simulation(network.txt)
    ReadConf(network.txt)
    SetConfig()
    SetupNetwork(qp_finish)
      open topology/flow/trace files
      create NS-3 nodes, links, switches
      install RDMA drivers
      build routing and BDP tables
      open fct/pfc/trace/qlen output files
  for each rank:
    systems[i]->workload->fire()
      Workload::call()
      Workload::issue_dep_free_nodes()
      Workload::issue(node)
  Simulator::Run()
```

进入 `Simulator::Run()` 后，ASTRA 和 NS-3 通过 callback 互相驱动：

```text
Workload issues comm node
  -> Sys::front_end_sim_send/front_end_sim_recv()
  -> Sys::sim_send/sim_recv()
  -> ASTRASimNetwork::sim_send/sim_recv()
  -> entry.h::send_flow() or recv-waiting table
  -> NS-3 RDMA event
  -> entry.h::qp_finish()
  -> notify_sender_sending_finished()
  -> notify_receiver_receive_data()
  -> MsgEvent::callHandler()
  -> Sys::handleEvent()
  -> Workload::call()
  -> dependency finished, issue more nodes
```

## 2. 命令行参数在哪里解析

文件：

```text
astra-sim/network_frontend/ns3/AstraSimNetwork.cc
```

函数：

```cpp
void parse_args(int argc, char* argv[])
```

参数对应关系：

| 命令行参数 | 保存到变量 | 后续谁使用 |
|---|---|---|
| `--workload-configuration` | `workload_configuration` | `Sys` -> `Workload` -> `ETFeeder` |
| `--comm-group-configuration` | `comm_group_configuration` | `Workload::initialize_comm_groups()` |
| `--system-configuration` | `system_configuration` | `Sys::initialize_sys()` |
| `--network-configuration` | `network_configuration` | `setup_ns3_simulation()` -> `ReadConf()` |
| `--logical-topology-configuration` | `logical_topology_configuration` | `read_logical_topo_config()` |
| `--remote-memory-configuration` | `memory_configuration` | `new AnalyticalRemoteMemory()` |
| `--logging-configuration` | `logging_configuration` | `LoggerFactory::init()` |
| `--comm-scale` | `comm_scale` | `ASTRASimNetwork::sim_send/sim_recv()` 中缩放 message size |
| `--injection-scale` | `injection_scale` | `Sys::initialize_sys()` 中缩放 endpoint delay |

当前脚本没有显式传 `--comm-scale`，所以默认：

```cpp
double comm_scale = 1;
```

## 3. logical topology 怎么读

输入：

```text
examples/network/ns3/dragonfly_4_8_4_33_logical.json
```

内容：

```json
{
    "logical-dims": ["33", "8", "4"]
}
```

读取位置：

```text
AstraSimNetwork.cc::read_logical_topo_config()
```

它做两件事：

1. 把 `"33", "8", "4"` 读入 `logical_dims`；
2. 计算总 rank 数：

```text
33 * 8 * 4 = 1056
```

所以 stdout 里会有：

```text
There are 1056 npus: 33,8,4,
```

后面 `main()` 会按这个 `num_npus=1056` 构造 1056 个 `Sys` 和 1056 个 `Workload`。

## 4. 每个 rank 的 ASTRA 系统怎么构造

文件：

```text
astra-sim/network_frontend/ns3/AstraSimNetwork.cc
```

代码位置：

```cpp
for (int npu_id = 0; npu_id < num_npus; npu_id++) {
    networks[npu_id] = new ASTRASimNetwork(npu_id, completion_tracker);
    systems[npu_id] = new AstraSim::Sys(...);
}
```

这里每个 rank 有两个对象：

```text
ASTRASimNetwork(rank)
```

这是 ASTRA 看到的 network backend。它实现了：

```cpp
sim_send()
sim_recv()
sim_schedule()
sim_get_time()
sim_notify_finished()
```

以及：

```text
Sys(rank)
```

这是 ASTRA 的 system layer。它负责：

- 读 system JSON；
- 建 logical topology；
- 建 collective algorithm；
- 建 Workload；
- 把 Workload 发起的通信变成 send/recv；
- 管理 ASTRA 自己的 callback/event queue。

## 5. system JSON 怎么读

输入：

```text
examples/system/native_collectives/Dragonfly_3D_Ring.json
```

读取位置：

```text
astra-sim/system/Sys.cc::Sys()
  -> Sys::initialize_sys(system_configuration)
```

当前关键配置：

```json
{
    "scheduling-policy": "LIFO",
    "endpoint-delay": 10,
    "active-chunks-per-dimension": 1,
    "preferred-dataset-splits": 4,
    "all-reduce-implementation": ["ring", "ring", "ring"],
    "all-gather-implementation": ["ring", "ring", "ring"],
    "reduce-scatter-implementation": ["ring", "ring", "ring"],
    "all-to-all-implementation": ["ring", "ring", "ring"],
    "collective-optimization": "baseline",
    "local-mem-bw": 3350
}
```

主要影响：

- collective 都使用 ring algorithm；
- 三个 logical dimension 都用 ring；
- `preferred-dataset-splits=4` 影响 collective chunk 切分；
- `endpoint-delay` 会进入 ASTRA event 延迟；
- `active-chunks-per-dimension` 影响并发 stream 调度。

collective implementation 的解析在：

```text
astra-sim/system/astraccl/CollectiveImplLookup.cc
```

`Sys::initialize_sys()` 末尾会调用：

```cpp
collective_impl_lookup->setup_collective_impl_from_config(j);
```

## 6. remote memory JSON 怎么读

输入：

```text
examples/remote_memory/analytical/no_memory_expansion.json
```

内容：

```json
{
    "memory-type": "NO_MEMORY_EXPANSION"
}
```

读取位置：

```text
AstraSimNetwork.cc::main()
  -> new Analytical::AnalyticalRemoteMemory(memory_configuration)
```

实现文件：

```text
extern/remote_memory_backend/analytical/AnalyticalRemoteMemory.cc
```

当前配置是 `NO_MEMORY_EXPANSION`。如果 workload 真的发出 remote memory node，会在：

```cpp
AnalyticalRemoteMemory::issue()
```

里报错退出：

```text
Remote memory access is not supported in NO_MEMORY_EXPANSION
```

对当前 Llama3 communication trace，主要路径是 compute/metadata/communication，不应依赖 remote memory expansion。

## 7. workload ET 怎么读

入口：

```text
astra-sim/workload/Workload.cc::Workload()
```

调用链：

```text
Sys::Sys()
  -> new Workload(this, workload_configuration, comm_group_configuration)
       workload_filename = workload_configuration + "." + rank + ".et"
       new ETFeeder(workload_filename)
       initialize_comm_groups(comm_group_configuration)
```

对 rank 0，实际文件是：

```text
artifacts/llama3_70b_b4096_r1056_compact/llama3_70b.0.et
```

repeated feeder 还会自动读 sidecar：

```text
artifacts/llama3_70b_b4096_r1056_compact/llama3_70b.0.et.repeat
```

内容类似：

```text
4096 1000000000
```

读取位置：

```text
extern/graph_frontend/chakra/src/feeder_v3/et_feeder.cpp
```

调用链：

```text
ETFeeder::ETFeeder(file_path)
  -> load_repetition_config(file_path)
       read file_path + ".repeat"
       set repeat_count / repetition_id_offset
       DependancyResolver::enable_repetition()
  -> build_index_dependancy_cache()
       read GlobalMetadata
       sequentially scan compact ET nodes
       build index_map: node_id -> file offset
       DependancyResolver::add_node(node)
       resolve dependency-free roots
  -> graph_sanity_check()
```

重复模式下，ET 文件本身只保存 base microbatch。lookup 其它 microbatch 的 virtual node 时：

```text
base_id      = virtual_id % repetition_id_offset
repeat_index = virtual_id / repetition_id_offset
```

然后在内存里临时 clone protobuf，并改：

```text
id
name
data_deps
ctrl_deps
```

对应函数：

```text
ETFeeder::get_raw_chakra_node()
```

## 8. communicator group JSON 怎么读

输入：

```text
artifacts/llama3_70b_b4096_r1056_compact/llama3_70b.json
```

读取位置：

```text
Workload::initialize_comm_groups()
```

格式大致是：

```json
{
  "1": [0, 1, 2, ...],
  "2": [...]
}
```

每个 key 是 communicator group id，每个 value 是参与该 group 的 rank 列表。

读取后创建：

```cpp
comm_groups[comm_group_id] =
    new CommunicatorGroup(comm_group_id, involved_NPUs, sys);
```

如果 ET node 上没有 `pg_name` 或 `pg_name == "0"`，`Workload::extract_comm_group()` 返回 `nullptr`，表示默认全局 communicator。

如果 ET node 指定了 `pg_name`，会查：

```cpp
comm_groups[comm_group_id]
```

找不到会直接报 critical 并退出。

此外，metadata node 也可能携带 PyTorch process group 信息。处理位置：

```text
Workload::issue_metadata()
  -> Workload::issue_pytorch_pg_metadata()
```

它会从 node inputs 里解析 JSON 并动态更新 `comm_groups`。

## 9. network.txt 怎么读

脚本生成：

```text
outputs/ns3_dragonfly_llama3_1056_repeated/network.txt
```

读取位置：

```text
astra-sim/network_frontend/ns3/entry.h::setup_ns3_simulation()
  -> extern/network_backend/ns-3/scratch/common.h::ReadConf()
```

`ReadConf()` 是简单 key-value parser。重要字段：

| network.txt 字段 | 作用 |
|---|---|
| `TOPOLOGY_FILE` | NS-3 物理拓扑文件 |
| `FLOW_FILE` | 外部 flow 文件；ASTRA 模式一般是 empty flow |
| `TRACE_FILE` | trace input；这里是 empty trace |
| `TRACE_OUTPUT_FILE` | 写 `trace.tr` |
| `FCT_OUTPUT_FILE` | 写 flow completion time 到 `fct.txt` |
| `PFC_OUTPUT_FILE` | 写 PFC event 到 `pfc.txt` |
| `QLEN_MON_FILE` | 写 queue monitor 到 `qlen.txt` |
| `SIMULATOR_STOP_TIME` | NS-3 stop time 配置 |
| `ENABLE_TRACE` | 是否启用 packet trace |
| `CC_MODE` / QCN / PFC 参数 | RDMA congestion control 配置 |
| `KMAX_MAP` / `KMIN_MAP` / `PMAX_MAP` | ECN threshold 配置 |

`ReadConf()` 只把这些字段读进全局变量。真正创建 NS-3 topology 在：

```text
common.h::SetupNetwork()
```

## 10. NS-3 topology 怎么建立

调用链：

```text
AstraSimNetwork.cc::main()
  -> setup_ns3_simulation(network_configuration)
       ReadConf(network.txt)
       SetConfig()
       SetupNetwork(qp_finish)
```

`SetupNetwork()` 文件：

```text
extern/network_backend/ns-3/scratch/common.h
```

主要步骤：

```text
SetupNetwork()
  open topology_file / flow_file / trace_file
  read node_num / switch_num / link_num
  create Node or SwitchNode
  install InternetStack
  assign server IP
  for each physical link:
    qbb.Install(src, dst)
    record nbr2if / delay / bandwidth
    connect PFC trace callback
  configure switch ECN/PFC/buffer
  open fct output
  for each host:
    create RdmaHw
    create RdmaDriver
    connect QpComplete -> qp_finish()
  CalculateRoutes()
  SetRoutingEntries()
  build pairBdp / pairRtt tables
  open trace output
  PopulateRoutingTables()
  initialize host-pair portNumber matrix
  open qlen output and schedule monitor_buffer()
```

关键全局结构：

| 变量 | 含义 |
|---|---|
| `NodeContainer n` | NS-3 中所有 host/switch node |
| `serverAddress` | host id -> IP |
| `portNumber[src][dst]` | host pair 的 RDMA source port 分配 |
| `nbr2if` | link/interface 信息 |
| `pairDelay` / `pairTxDelay` | path delay |
| `pairBw` | path bandwidth |
| `pairBdp` / `pairRtt` | RDMA window/RTT |
| `sender_src_port_map` | source port -> ASTRA tag |
| `sim_send_waiting_hash` | 已发 send，等待 NS-3 QP 完成 |
| `sim_recv_waiting_hash` | ASTRA 已调用 recv，等待 NS-3 message 到达 |
| `received_msg_standby_hash` | NS-3 已收到，但 ASTRA recv 还没发出 |

## 11. workload.fire() 之后发生什么

入口：

```text
AstraSimNetwork.cc::main()
  -> systems[i]->workload->fire()
```

`Workload::fire()` 很薄：

```cpp
void Workload::fire() {
    call(EventType::General, NULL);
}
```

之后：

```text
Workload::call(EventType::General, nullptr)
  -> issue_dep_free_nodes()
       get dependency-free node ids from ETFeeder
       lookupNode(node_id)
       if HardwareResource available:
         issue(node)
```

`Workload::issue()` 根据 Chakra node type 分派：

| Chakra node type | 处理函数 |
|---|---|
| `MEM_LOAD_NODE` / `MEM_STORE_NODE` | `issue_remote_mem()` |
| `COMP_NODE` | `issue_replay()` 或 `issue_comp()` |
| `COMM_COLL_NODE` | `issue_coll_comm()` |
| `COMM_SEND_NODE` | `issue_send_comm()` |
| `COMM_RECV_NODE` | `issue_recv_comm()` |
| `INVALID_NODE` | `skip_invalid()` |
| `METADATA_NODE` | `issue_metadata()` |

每个 node issue 时都会：

```text
DependancyResolver::take_node(node_id)
HardwareResource::occupy(node)
Statistics::record_start()
```

完成后会：

```text
DependancyResolver::finish_node(node_id)
HardwareResource::release(node)
Statistics::record_end()
issue_dep_free_nodes()
```

也就是说 workload 是依赖驱动的：某个 node 完成后，释放 children，再继续 issue 新的 dependency-free nodes。

## 12. 点对点 send/recv node 的路径

ET 中如果直接有 `COMM_SEND_NODE`：

```text
Workload::issue_send_comm()
  -> Sys::front_end_sim_send()
       normalize tag range
       if rendezvous disabled:
         Sys::sim_send()
           if delay == 0:
             comm_NI->sim_send()
```

这里 `comm_NI` 是：

```text
ASTRASimNetwork*
```

所以进入：

```text
AstraSimNetwork.cc::ASTRASimNetwork::sim_send()
  -> scale_message_size()
  -> send_flow()
```

`send_flow()` 在：

```text
astra-sim/network_frontend/ns3/entry.h
```

它做：

```text
allocate source port
sender_src_port_map[(port, src, dst)] = tag
sim_send_waiting_hash[(tag, src, dst, port)] = MsgEvent(callback)
RdmaClientHelper(...).Install(src node)
appCon.Start(Time(0))
```

ET 中如果有 `COMM_RECV_NODE`：

```text
Workload::issue_recv_comm()
  -> Sys::front_end_sim_recv()
  -> Sys::sim_recv()
  -> ASTRASimNetwork::sim_recv()
```

`ASTRASimNetwork::sim_recv()` 不直接创建 NS-3 flow。它只登记“我在等这个 message”：

```text
sim_recv_waiting_hash[(tag, src, dst)] = MsgEvent(callback)
```

如果 NS-3 已经先收到，数据会在 `received_msg_standby_hash`，此时 `sim_recv()` 会立即触发 callback。

## 13. collective node 的路径

ET 中如果是 `COMM_COLL_NODE`：

```text
Workload::issue_coll_comm()
  -> extract_comm_group(node)
  -> read comm_type / comm_size / comm_priority / involved dims
  -> Sys::generate_all_reduce / generate_all_gather / generate_reduce_scatter / generate_all_to_all
```

以 all-reduce 为例：

```text
Sys::generate_all_reduce()
  -> Sys::generate_collective()
       determine chunk size
       choose topology dimension order
       generate_collective_phase()
       new StreamBaseline(...)
       insert_into_ready_list()
       schedule()
```

ring collective 的具体收发逻辑在：

```text
astra-sim/system/astraccl/native_collectives/collective_algorithm/Ring.cc
```

典型路径：

```text
Ring::ready()
  -> stream->owner->front_end_sim_send(... COLLECTIVE ...)
  -> stream->owner->front_end_sim_recv(... COLLECTIVE ...)
```

这里 `stream->owner` 就是 `Sys*`。

因此 collective 最后也会回到同一条低层路径：

```text
Sys::front_end_sim_send/recv()
  -> Sys::sim_send/recv()
  -> ASTRASimNetwork::sim_send/recv()
  -> entry.h::send_flow() / recv waiting table
```

区别是 tag range 不同：

```text
NATIVE     [0, 500000000)
COLLECTIVE [500000000, 1000000000)
RENDEZVOUS [1000000000, 2000000000)
```

这个逻辑在：

```text
Sys::front_end_sim_send()
Sys::front_end_sim_recv()
```

## 14. ASTRA event 怎么交给 NS-3 调度

ASTRA 自己也有 event queue：

```text
Sys::event_queue
```

注册事件：

```text
Sys::register_event()
  -> Sys::try_register_event()
       event_queue[event_time].push_back(...)
       comm_NI->sim_schedule(tmp, &Sys::handleEvent, data)
```

NS-3 backend 的 `sim_schedule()` 是：

```text
AstraSimNetwork.cc::ASTRASimNetwork::sim_schedule()
  -> Simulator::Schedule(NanoSeconds(delta.time_val), fun_ptr, fun_arg)
```

所以 ASTRA 的 compute/replay/memory callback 也是通过 NS-3 event queue 驱动。

回调入口统一是：

```text
Sys::handleEvent(void* arg)
```

它根据 `EventType` 分派：

| EventType | 去哪里 |
|---|---|
| `CallEvents` | `Sys::call_events()` |
| `PacketSent` | `SendPacketEventHandlerData.callable->call()`，通常回到 `Workload::call()` |
| `PacketReceived` | `Workload::call()` 或 stream/custom algorithm |
| `CompFinished` / memory | `Workload::call()` |
| rendezvous | 对应 send/recv continuation |

## 15. NS-3 flow 完成后怎么回到 ASTRA

NS-3 RDMA QP 完成后触发：

```text
entry.h::qp_finish(FILE* fct_output, Ptr<RdmaQueuePair> q)
```

这个 callback 是在 `SetupNetwork()` 里注册的：

```cpp
rdma->TraceConnectWithoutContext(
    "QpComplete", MakeBoundCallback(qp_finish, fct_output));
```

`qp_finish()` 做四件事：

1. 写 FCT：

```text
qp_finish_print_log(fct_output, q)
```

输出文件：

```text
${RUN_DIR}/fct.txt
```

2. 删除 receiver 侧 rxQp：

```cpp
rdma->m_rdma->DeleteRxQp(...)
```

3. 根据 source port 找回 ASTRA tag：

```cpp
sender_src_port_map[(q->sport, sid, did)] -> tag
```

4. 分别通知 sender 和 receiver：

```text
notify_sender_sending_finished()
notify_receiver_receive_data()
```

sender 侧：

```text
notify_sender_sending_finished()
  -> find sim_send_waiting_hash[(tag, src, dst, src_port)]
  -> erase waiting entry
  -> MsgEvent::callHandler()
  -> Sys::handleEvent()
  -> Workload::call(EventType::PacketSent, ...)
```

receiver 侧：

```text
notify_receiver_receive_data()
  if sim_recv_waiting_hash has matching recv:
      consume bytes
      if enough bytes:
          MsgEvent::callHandler()
          -> Sys::handleEvent()
          -> Workload::call(EventType::PacketReceived, ...)
  else:
      accumulate in received_msg_standby_hash
```

这就是 send/recv matching 的核心。

## 16. rank 完成和仿真结束

每个 rank 的 workload 在 `Workload::call()` 末尾检查：

```text
dependency-free set empty
ongoing set empty
no in-flight cpu/gpu/comm ops
```

满足后：

```text
Workload::report()
sys->comm_NI->sim_notify_finished()
```

`Workload::report()` 会用 spdlog 打：

```text
sys[<rank>] finished, <cycles> cycles, exposed communication <cycles> cycles.
```

由于 stdout 被脚本重定向，这条会进：

```text
${RUN_DIR}/stdout.log
```

`sim_notify_finished()` 在：

```text
AstraSimNetwork.cc::ASTRASimNetwork::sim_notify_finished()
```

它调用：

```text
NS3BackendCompletionTracker::mark_rank_as_finished(rank)
```

所有 rank 完成后：

```text
Simulator::Stop()
Simulator::Destroy()
exit(0)
```

## 17. 各类输出在哪里写

### stdout.log

来源：

- `std::cout`
- spdlog stdout sink，level >= info
- 我们新增的 `[ns3-progress] ...`
- `Workload::report()` 的 rank finished 信息

路径：

```text
${RUN_DIR}/stdout.log
```

### stderr.log

来源：

- `std::cerr`
- uncaught exception / abort message
- shell redirection `2>`

路径：

```text
${RUN_DIR}/stderr.log
```

### fct.txt

来源：

```text
entry.h::qp_finish_print_log()
```

每个完成的 RDMA QP 写一行：

```text
sip dip sport dport size start_time fct standalone_fct
```

路径来自 `network.txt` 的：

```text
FCT_OUTPUT_FILE
```

脚本会改成：

```text
${RUN_DIR}/fct.txt
```

如果 `fct.txt` 长时间为 0，说明没有任何 RDMA QP 完成。

### pfc.txt

来源：

```text
common.h::get_pfc()
```

在 `SetupNetwork()` 里通过：

```cpp
TraceConnectWithoutContext("QbbPfc", ...)
```

挂到每个 QbbNetDevice。

路径：

```text
${RUN_DIR}/pfc.txt
```

### qlen.txt

来源：

```text
common.h::monitor_buffer()
```

`SetupNetwork()` 末尾调度：

```cpp
Simulator::Schedule(NanoSeconds(qlen_mon_start), &monitor_buffer, ...)
```

路径：

```text
${RUN_DIR}/qlen.txt
```

### trace.tr

来源：

```text
common.h::SetupNetwork()
```

即使 `ENABLE_TRACE=0`，代码仍会写入 `SimSetting`，所以 `trace.tr` 可能不是完全空。

路径：

```text
${RUN_DIR}/trace.tr
```

### log/log.log 和 log/err.log

来源：

```text
astra-sim/common/Logging.cc::LoggerFactory::init_default_components()
```

默认目录是 binary 当前工作目录下的：

```text
log/
```

脚本执行前 `cd extern/network_backend/ns-3/build/scratch`，所以路径通常是：

```text
extern/network_backend/ns-3/build/scratch/log/log.log
extern/network_backend/ns-3/build/scratch/log/err.log
```

## 18. 新增 progress log 怎么读

我在 repeated-feeder worktree 里加了显式 progress log。重编译后，新 run 的 stdout 会出现：

```text
[2026-06-23 17:01:18.210] [ns3-progress] stage=construct-systems detail="allocating ASTRA systems"
[2026-06-23 17:01:20.341] [ns3-progress] constructed systems 32/1056
...
[2026-06-23 17:02:01.012] [ns3-progress] stage=setup-network detail="ReadConf and SetConfig"
[2026-06-23 17:02:01.033] [ns3-progress] SetupNetwork: parsed topology node_num=... switch_num=... link_num=...
[2026-06-23 17:02:03.517] [ns3-progress] SetupNetwork: created channels 1000/...
...
[2026-06-23 17:04:29.120] [ns3-progress] stage=workload-fire detail="initial workload fire"
[2026-06-23 17:04:31.455] [ns3-progress] workload fire 32/1056
...
[2026-06-23 17:06:10.001] [ns3-progress] stage=simulator-run detail="enter Simulator::Run"
[2026-06-23 17:07:10.002] [ns3-progress] heartbeat=wall elapsed_s=60 stage=simulator-run finished_ranks=0 sim_send=... sim_recv=... send_flow=... qp_finish=... callbacks=...
```

heartbeat 字段含义：

| 字段 | 含义 |
|---|---|
| `elapsed_s` | 进入 `Simulator::Run()` 后 wall-clock 秒数 |
| `stage` | 当前粗粒度阶段 |
| `finished_ranks` | 已完成 workload 的 rank 数 |
| `sim_send` | ASTRA 调到 backend `sim_send()` 的次数 |
| `sim_recv` | ASTRA 调到 backend `sim_recv()` 的次数 |
| `send_flow` | `send_flow()` 创建 NS-3 RDMA flow 的次数 |
| `qp_finish` | NS-3 QP 完成次数，也对应 FCT 行数趋势 |
| `callbacks` | `MsgEvent::callHandler()` 被触发次数 |
| `send_callbacks` | send 完成 callback 次数 |
| `recv_callbacks` | recv 完成 callback 次数 |
| `send_bytes` / `recv_bytes` | ASTRA backend 层看到的 message bytes 总量，受 `comm_scale` 影响 |

判断卡点时优先看：

```text
sim_send / sim_recv / send_flow / qp_finish / callbacks / finished_ranks
```

典型情况：

| 现象 | 说明 |
|---|---|
| `sim_send=0 sim_recv=0` | workload 没真正发出网络操作 |
| `sim_send/send_flow` 增长，`qp_finish=0` | NS-3 正在处理或卡在网络 flow 完成前 |
| `qp_finish` 增长，`callbacks` 不增长 | QP 完成后 sender/receiver matching 或 callback 有问题 |
| `callbacks` 增长，`finished_ranks=0` | workload 在推进，但 rank 还没完整结束 |
| `finished_ranks` 增长 | 全局确实在前进 |

heartbeat 默认 60 秒。这个值落在 run script 里：

```text
examples/run_scripts/ns3/run_dragonfly_llama3_1056_repeated.sh
  NS3_PROGRESS_INTERVAL_SECONDS="${NS3_PROGRESS_INTERVAL_SECONDS:-60}"
```

如果希望改成 30 秒，直接改脚本里的默认值，不需要运行时额外传命令行参数。

注意：heartbeat 是单独 wall-clock 线程，只读 atomic counters；它不会向 NS-3 event queue 插入周期事件，避免改变仿真事件队列行为。

## 19. 当前代码最难读的几个点

### 19.1 `entry.h` 不是普通 header

虽然叫：

```text
astra-sim/network_frontend/ns3/entry.h
```

但它里面定义了大量全局变量和函数：

- `send_flow()`
- `qp_finish()`
- `notify_sender_sending_finished()`
- `notify_receiver_receive_data()`
- `setup_ns3_simulation()`
- send/recv waiting maps

它被 `AstraSimNetwork.cc` include，相当于把这些实现直接塞进同一个 translation unit。

阅读时不要把它当普通声明文件，它是 NS-3 backend glue code 的核心实现文件。

### 19.2 ASTRA 和 NS-3 通过 callback 双向驱动

ASTRA 发网络：

```text
Workload -> Sys -> ASTRASimNetwork -> entry.h -> NS-3
```

NS-3 完成网络：

```text
NS-3 -> qp_finish -> MsgEvent callback -> Sys::handleEvent -> Workload
```

所以调用链不是单向函数调用，而是事件驱动循环。

### 19.3 collective 会变成很多 send/recv

ET 里一个 `COMM_COLL_NODE` 不会直接创建一个 NS-3 flow。它先在 ASTRA system layer 展开成 collective algorithm stream，例如 ring。ring 再逐步调用：

```text
front_end_sim_send()
front_end_sim_recv()
```

最后才进入 NS-3 flow。

### 19.4 `WORKLOAD` 是 prefix，不是文件

命令行传：

```text
--workload-configuration=/.../llama3_70b
```

但 `Workload` 会自己拼：

```text
llama3_70b.<rank>.et
```

repeated feeder 又会自己尝试读：

```text
llama3_70b.<rank>.et.repeat
```

### 19.5 `stdout.log` 和 ASTRA spdlog 不是同一个概念

脚本重定向的：

```text
stdout.log
stderr.log
```

是进程级 stdout/stderr。

ASTRA `LoggerFactory` 还会创建：

```text
build/scratch/log/log.log
build/scratch/log/err.log
```

同时 spdlog info 也会通过 stdout sink 进 `stdout.log`。

## 20. 推荐阅读顺序

如果从零开始读，建议按这个顺序：

1. `examples/run_scripts/ns3/run_dragonfly_llama3_1056_repeated.sh`
   - 搞清楚所有输入、输出路径。
2. `astra-sim/network_frontend/ns3/AstraSimNetwork.cc`
   - 看 `main()`、`parse_args()`、`read_logical_topo_config()`、`ASTRASimNetwork::sim_send/recv()`。
3. `astra-sim/network_frontend/ns3/entry.h`
   - 看 `send_flow()`、`qp_finish()`、send/recv waiting maps。
4. `extern/network_backend/ns-3/scratch/common.h`
   - 看 `ReadConf()`、`SetupNetwork()`、`monitor_buffer()`、`get_pfc()`。
5. `astra-sim/system/Sys.cc`
   - 看 `Sys::Sys()`、`initialize_sys()`、`generate_collective()`、`front_end_sim_send/recv()`、`handleEvent()`。
6. `astra-sim/workload/Workload.cc`
   - 看 `Workload::Workload()`、`fire()`、`issue_dep_free_nodes()`、`issue()`、`issue_comm()`、`call()`。
7. `extern/graph_frontend/chakra/src/feeder_v3/et_feeder.cpp`
   - 看 compact/repeated ET 怎么读，virtual node 怎么 materialize。
8. `astra-sim/system/astraccl/native_collectives/collective_algorithm/Ring.cc`
   - 看 ring collective 怎么变成 send/recv。

## 21. 一条完整通信的最小调用链

点对点 send/recv：

```text
Workload::issue_send_comm()
  -> Sys::front_end_sim_send()
  -> Sys::sim_send()
  -> ASTRASimNetwork::sim_send()
  -> send_flow()
  -> RdmaClientHelper::Install()
  -> NS-3 events
  -> qp_finish()
  -> notify_sender_sending_finished()
  -> MsgEvent::callHandler()
  -> Sys::handleEvent()
  -> Workload::call(PacketSent)
  -> DependancyResolver::finish_node()
  -> Workload::issue_dep_free_nodes()
```

点对点 recv 对称路径：

```text
Workload::issue_recv_comm()
  -> Sys::front_end_sim_recv()
  -> Sys::sim_recv()
  -> ASTRASimNetwork::sim_recv()
  -> sim_recv_waiting_hash[...] = MsgEvent
  -> NS-3 qp_finish later
  -> notify_receiver_receive_data()
  -> MsgEvent::callHandler()
  -> Sys::handleEvent()
  -> Workload::call(PacketReceived)
  -> DependancyResolver::finish_node()
```

collective ring：

```text
Workload::issue_coll_comm()
  -> Sys::generate_all_reduce()
  -> Sys::generate_collective()
  -> StreamBaseline / Ring phase
  -> Ring::ready()
  -> Sys::front_end_sim_send()
  -> Sys::front_end_sim_recv()
  -> same NS-3 send/recv path
  -> PacketReceived callback
  -> Ring::run(PacketReceived)
  -> eventually stream completes
  -> Workload::call(CollectiveCommunicationFinished)
  -> DependancyResolver::finish_node()
```

这就是从 ET node 到 NS-3 packet event，再回到 ET dependency graph 的闭环。
