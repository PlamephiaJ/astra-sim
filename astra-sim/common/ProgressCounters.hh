/******************************************************************************
This source code is licensed under the MIT license found in the
LICENSE file in the root directory of this source tree.
*******************************************************************************/

#ifndef ASTRA_SIM_PROGRESS_COUNTERS_HH
#define ASTRA_SIM_PROGRESS_COUNTERS_HH

#include <atomic>
#include <cstdint>

namespace AstraSim {
namespace ProgressCounters {

extern std::atomic<uint64_t> sys_schedule_calls;
extern std::atomic<uint64_t> sys_handle_event_calls;
extern std::atomic<uint64_t> sys_call_events_calls;
extern std::atomic<uint64_t> sys_events_registered;
extern std::atomic<uint64_t> sys_events_completed;
extern std::atomic<int64_t> sys_events_pending;

extern std::atomic<uint64_t> workload_issue_dep_free_calls;
extern std::atomic<uint64_t> workload_ready_initial_enqueued;
extern std::atomic<uint64_t> workload_ready_released_enqueued;
extern std::atomic<uint64_t> workload_ready_dequeued;
extern std::atomic<int64_t> workload_ready_set_nodes;
extern std::atomic<int64_t> workload_ready_queue_nodes;

extern std::atomic<uint64_t> workload_issue_total;
extern std::atomic<uint64_t> workload_issue_metadata;
extern std::atomic<uint64_t> workload_issue_mem_load;
extern std::atomic<uint64_t> workload_issue_mem_store;
extern std::atomic<uint64_t> workload_issue_comp;
extern std::atomic<uint64_t> workload_issue_coll;
extern std::atomic<uint64_t> workload_issue_send;
extern std::atomic<uint64_t> workload_issue_recv;
extern std::atomic<uint64_t> workload_issue_invalid;
extern std::atomic<uint64_t> workload_issue_other;

}  // namespace ProgressCounters
}  // namespace AstraSim

#endif
