# Exp2：ECMP 与 UGAL-L bias sweep 对比

日期：2026-09-25

## 最终结论

第一阶段的 bias sweep 暴露出一个实现问题：`HAS_WIN=1` 时，UGAL-L 选中 2-hop long path 后仍使用 shortest-path 的 40.5 KB BDP，导致 8 MiB / 16 MiB flow 出现约 41.6 ms / 83.2 ms 长尾。关闭 window 后，UGAL-L 的 makespan 从 87.260 ms 降到 10.392 ms，并比同配置 ECMP 快 16.1%，验证了根因是 RTT/BDP 不匹配。

第二阶段已让 QP 按实际选中的 data 和 ACK 路径更新闭环 RTT 与 window。`bias=0` 回归中有 15/40 个方向选择 non-minimal，但原有长尾消失：mean FCT 比 ECMP 低 19.1%，P50 低 49.3%；P95 高 3.2%，最大 FCT 为 8.104 ms，最终 makespan 为 12.840 ms，比 ECMP 慢 2.0%。因此路径感知 BDP 修复了正确性/公平性问题，但本 workload 下 `bias=0` 尚未在 makespan 上击败 ECMP。

`bias=128 KiB` 仍把全部决定压回 minimal path，且修复后的 `fct.txt`、`qlen.txt` 与 ECMP byte-identical。当前建议：若以本 workload 的 makespan 为主要指标，仍使用 ECMP；UGAL-L 已具备可公平调参的基础，应在更多 workload/seed 上重新 sweep bias，而不是把 128 KiB 当作自适应路由收益。

## 实验设置

- workload：8 ranks；一个 4-rank ring AllReduce 与一个 4-rank direct All-to-All 并行。
- topology：`switch 8 -- switch 9` 为 1-hop short path；`switch 8 -- 10 -- switch 9` 为 2-hop long path，两段 long link delay 均为 0.100 ms。
- 每组 36 条 data flow；UGAL-L 对跨侧 data/ACK 方向共作出 40 次决策。
- queue monitor：`0–100,000,000 ns`，采样间隔 100 ns；模拟在 workload 完成时提前停止。
- `qlen.txt` 只写出至少 1,000 B 的 egress queue，因此下文平均队列是 recorded lower bound。
- FCT P95 使用 nearest-rank；workload makespan 取 `run.log` 中 8 个 rank 的最大完成时间。
- 第一阶段各组 flow-route verifier 均通过，且 PFC 事件均为 0；window 控制实验的 PFC 单独列于后文。

实验归档：

- [ECMP baseline](log/20260925_223336/ecmp/config/manifest.txt)
- [UGAL-L bias=0](log/20260925_223416/ugal_l/config/manifest.txt)
- [UGAL-L bias=32 KiB](log/20260925_223446/ugal_l/config/manifest.txt)
- [UGAL-L bias=64 KiB](log/20260925_223512/ugal_l/config/manifest.txt)
- [UGAL-L bias=128 KiB](log/20260925_223538/ugal_l/config/manifest.txt)
- [ECMP, HAS_WIN=0](log/20260925_224628/ecmp/config/manifest.txt)
- [UGAL-L bias=0, HAS_WIN=0](log/20260925_224654/ugal_l/config/manifest.txt)
- [ECMP, route-aware 代码回归](log/20260925_232801/ecmp/config/manifest.txt)
- [UGAL-L bias=0, route-aware BDP](log/20260925_232900/ugal_l/config/manifest.txt)
- [UGAL-L bias=128 KiB, route-aware BDP](log/20260925_234700/ugal_l/config/manifest.txt)

## 第一阶段：FCT 与完工时间

| 策略 | nonminimal / 40 | Mean FCT | P50 FCT | P95 FCT | Max FCT | Mean slowdown | Workload makespan | 相对 ECMP |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ECMP | N/A | 2.263 ms | 1.863 ms | 4.696 ms | 4.696 ms | 2.228× | 12.583 ms | 1.00× |
| UGAL-L, bias=0 | 3 | 6.840 ms | **0.734 ms** | 83.177 ms | 83.235 ms | 6.017× | 87.260 ms | 6.93× |
| UGAL-L, bias=32 KiB | 3 | 7.139 ms | **0.760 ms** | 83.150 ms | 83.150 ms | 6.253× | 88.906 ms | 7.07× |
| UGAL-L, bias=64 KiB | 3 | 7.352 ms | **1.461 ms** | 83.143 ms | 83.162 ms | 6.494× | 89.299 ms | 7.10× |
| UGAL-L, bias=128 KiB | 0 | 2.263 ms | 1.863 ms | 4.696 ms | 4.696 ms | 2.228× | 12.583 ms | 1.00× |

低 bias 确实改善了 typical flow：bias=0 的 P50 比 ECMP 低 60.6%。但是每组仅 3 条极端长尾就完全主导了 P95 和 makespan，因此该收益不能转化为 workload 加速。

128 KiB 的结果与 ECMP 完全相同：两组 `fct.txt` 和 `qlen.txt` 均通过 byte-for-byte 比较。这也证明差异来自 non-minimal path，而不是策略名称或额外的路由判断本身。

## 完整队列轨迹

| 策略 | 最后 ≥1 KB 记录 | 平均 recorded queue | Peak aggregate queue | Max single-port queue | Aggregate ≥64 KiB 的时间 | PFC |
|---|---:|---:|---:|---:|---:|---:|
| ECMP | 12.529 ms | 96,241 B | 143,728 B | 90,316 B | 11.372 ms | 0 |
| UGAL-L, bias=0 | 87.257 ms | 2,889 B | 102,488 B | 68,252 B | 0.137 ms | 0 |
| UGAL-L, bias=32 KiB | 88.901 ms | 7,311 B | 110,536 B | 84,952 B | 3.654 ms | 0 |
| UGAL-L, bias=64 KiB | 89.295 ms | 9,751 B | 139,392 B | 91,764 B | 7.652 ms | 0 |
| UGAL-L, bias=128 KiB | 12.529 ms | 96,241 B | 143,728 B | 90,316 B | 11.372 ms | 0 |

这次 100 ms 监控排除了“长尾期间持续严重排队”的解释。bias=0 虽运行了 87 ms，但 aggregate queue ≥64 KiB 仅持续约 0.137 ms，且没有 PFC。其较低的时间平均队列主要来自 flow 长时间被 window 限速、网络大部分时间接近空闲，不能解读为更高效率。

## Non-minimal 决策与长尾的一一对应

所有发生 non-minimal 的方向，其 `nonminimal_q_bytes` 都为 0；判断实际上简化为 `minimal_q_bytes > bias`。每一次 non-minimal 决策都对应一条约 1.6 Gb/s 的长尾 data flow，包括 ACK 方向被绕行的情况。

| Bias | 被绕行的方向 | 类型 | 决策时 minimal queue | 对应 data flow | FCT |
|---:|---|---|---:|---|---:|
| 0 | rank 7→2 | data | 1,036 B | rank 7→2, 16 MiB | 83.235 ms |
| 0 | rank 4→1 | ACK | 21,756 B | rank 1→4, 8 MiB | 41.582 ms |
| 0 | rank 2→6 | data | 1,084 B | rank 2→6, 16 MiB | 83.177 ms |
| 32 KiB | rank 4→1 | ACK | 44,548 B | rank 1→4, 8 MiB | 41.697 ms |
| 32 KiB | rank 3→7 | ACK | 43,512 B | rank 7→3, 16 MiB | 83.150 ms |
| 32 KiB | rank 3→6 | ACK | 42,476 B | rank 6→3, 16 MiB | 83.150 ms |
| 64 KiB | rank 0→5 | ACK | 78,008 B | rank 5→0, 8 MiB | 41.579 ms |
| 64 KiB | rank 3→6 | ACK | 84,952 B | rank 6→3, 16 MiB | 83.143 ms |
| 64 KiB | rank 3→7 | ACK | 83,916 B | rank 7→3, 16 MiB | 83.162 ms |
| 128 KiB | — | — | — | — | — |

完整决策日志：

- [bias=0 routing decisions](log/20260925_223416/ugal_l/routing_decisions.log)
- [bias=32 KiB routing decisions](log/20260925_223446/ugal_l/routing_decisions.log)
- [bias=64 KiB routing decisions](log/20260925_223512/ugal_l/routing_decisions.log)
- [bias=128 KiB routing decisions](log/20260925_223538/ugal_l/routing_decisions.log)

## 根因：window/BDP 仍按 shortest path 计算

日志报告 `maxRtt=3240 ns`、`maxBdp=40500 B`。配置启用了 per-pair window（`HAS_WIN=1`, `GLOBAL_T=0`, `VAR_WIN=1`），而发送 QP 使用 `pairBdp[src][dst]` 和 `pairRtt[src][dst]`。这些值在 shortest-path routing table 建好后，由 `pairDelay` 计算，并不知道 UGAL-L 随后可能把某个 flow 固定到 2-hop long path。

一旦 data 或 ACK 方向走 long path，闭环 RTT 约为 202 μs，但 window 仍约为 40.5 KB：

```text
window-limited throughput ≈ 40,500 B × 8 / 202 μs ≈ 1.604 Gb/s
observed 8 MiB / 41.582 ms                      ≈ 1.614 Gb/s
observed 16 MiB / 83.177 ms                    ≈ 1.614 Gb/s
```

理论值与两类长尾的实测吞吐几乎一致。因此根因是 non-minimal path 的实际 RTT 与 QP window/BDP 不匹配，而不是 PFC 或持续拥塞。

相关实现：

- [`entry.h`](../../astra-sim/network_frontend/ns3/entry.h#L160-L167)：QP 使用 `pairBdp` / `pairRtt`。
- [`common.h`](../../extern/network_backend/ns-3/scratch/common.h#L837-L872)：按已计算的 routing delay 生成 pair BDP/RTT。
- [`rdma-queue-pair.cc`](../../extern/network_backend/ns-3/src/point-to-point/model/rdma-queue-pair.cc#L161-L177)：on-the-fly bytes 达到 window 后停止发送。
- [`README.md`](README.md#ugal-l)：UGAL-L 首包决策及 per-flow 固定路径语义。

## 第一步：关闭 window 的控制实验

保持 workload、topology 和 `bias=0` 不变，只用 `EXP2_HAS_WIN=0` 覆盖 runtime config：

| 策略 | nonminimal / 40 | Mean FCT | P50 FCT | P95 FCT | Max FCT | Flow finish | Workload makespan |
|---|---:|---:|---:|---:|---:|---:|---:|
| ECMP, HAS_WIN=0 | N/A | 1.983 ms | 1.602 ms | 5.091 ms | 5.127 ms | 11.391 ms | 12.391 ms |
| UGAL-L bias=0, HAS_WIN=0 | 16 | **1.691 ms** | **1.424 ms** | **4.168 ms** | **4.323 ms** | **10.013 ms** | **10.392 ms** |

UGAL-L 的 41/83 ms 长尾完全消失，makespan 比同配置 ECMP 低 16.1%。这组干预实验直接验证了 window/BDP 是第一阶段异常长尾的原因，而不是 long path 本身必然很慢。

该结果只用于根因验证，不作为推荐配置：关闭 window 后 ECMP 与 UGAL-L 分别记录了 9,418 和 3,930 条 PFC 事件。两组 flow-route verifier 均通过。

## 第二步：按实际路径动态更新 RTT/BDP

实现现在按每个 UGAL-L flow 的实际选择更新源 QP：

1. 初始化 UGAL-L route 时，计算 non-minimal 相对 minimal path 增加的传播延迟和 packet 串行化延迟。
2. data 与 ACK/NACK 方向首次选路后分别回报附加 RTT；ACK tuple 会还原为原 data QP。
3. QP 合并正反方向，令 `actual_rtt = shortest_rtt + forward_extra + reverse_extra`，再按 RTT 比例放大 shortest-path window。
4. `NS3_UGAL_QP_UPDATE` 记录最终 `base_rtt_ns` 与 `window_bytes`，便于逐 flow 审计。

本 topology 中观察到的更新值与计算一致：

| 实际路径 | base RTT | QP window |
|---|---:|---:|
| data、ACK 都走 minimal | 3,240 ns | 40,500 B |
| 一个方向走 long path | 202,820 ns | 2,535,250 B |
| 两个方向都走 long path | 402,400 ns | 5,030,000 B |

修复后的回归结果：

| 策略 | nonminimal / 40 | Mean FCT | P50 FCT | P95 FCT | Max FCT | Workload makespan | 相对 ECMP |
|---|---:|---:|---:|---:|---:|---:|---:|
| ECMP | N/A | 2.263 ms | 1.863 ms | 4.696 ms | 4.696 ms | **12.583 ms** | 1.000× |
| UGAL-L bias=0，修复前 | 3 | 6.840 ms | 0.734 ms | 83.177 ms | 83.235 ms | 87.260 ms | 6.934× |
| UGAL-L bias=0，route-aware | 15 | **1.832 ms** | **0.944 ms** | 4.848 ms | 8.104 ms | 12.840 ms | 1.020× |
| UGAL-L bias=128 KiB，route-aware | 0 | 2.263 ms | 1.863 ms | 4.696 ms | 4.696 ms | **12.583 ms** | 1.000× |

相对修复前的 `bias=0`，route-aware 版本把 mean FCT 降低 73.2%、最大 FCT 降低 90.3%、makespan 降低 85.3%。non-minimal 数量从 3 增至 15 是合理的连锁效应：修复吞吐后，后续 flow 首包到达时看到的瞬时队列和旧运行不同。

回归还确认：修改前后的 ECMP `fct.txt`、`qlen.txt` byte-identical；修复后的 128 KiB minimal-only UGAL-L 也与 ECMP byte-identical。三组均无 PFC，flow-route verifier 均通过。

## Runner 改动与复现产物

`experiments/exp2/run.sh` 已完成以下改动：

1. 新增 `EXP2_QLEN_MON_END_NS`，默认 `100000000`（100 ms），仅覆盖 exp2 runtime config，不修改共享的 exp1 模板。
2. 新增 `EXP2_HAS_WIN`（`0` 或 `1`，默认 `1`），用于生成隔离的 window 控制实验。
3. 完整 stdout 继续写入 `run.log`；同时将 `NS3_UGAL`、`NS3_UGAL_QP_UPDATE` / `NS3_ECMP` 行提取到独立的 `routing_decisions.log`。
4. manifest 新增 `qlen_mon_end_ns` 与 `has_win`，确保运行参数可审计。

每组实验均保留 `fct.txt`、`qlen.txt`、`pfc.txt`、`run.log`、`routing_decisions.log` 和完整配置快照。

## 后续建议

1. 基于 route-aware BDP 重新 sweep bias；当前只验证了边界点 `0` 与 `128 KiB`，不能据此确定最优阈值。
2. 增加多个 workload/seed，避免把单一流启动顺序对应的阈值当成通用结论。
3. 若扩展到任意动态路径或异构带宽，建议让 route metadata 直接携带完整 forward/reverse RTT 与目标 BDP，而不只携带相对 shortest path 的额外 RTT。
