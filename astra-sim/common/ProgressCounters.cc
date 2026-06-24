/******************************************************************************
This source code is licensed under the MIT license found in the
LICENSE file in the root directory of this source tree.
*******************************************************************************/

#include "astra-sim/common/ProgressCounters.hh"

namespace AstraSim {
namespace ProgressCounters {

std::atomic<uint64_t> sys_schedule_calls{0};
std::atomic<uint64_t> sys_handle_event_calls{0};
std::atomic<uint64_t> sys_call_events_calls{0};
std::atomic<uint64_t> sys_events_registered{0};
std::atomic<uint64_t> sys_events_completed{0};
std::atomic<int64_t> sys_events_pending{0};

std::atomic<uint64_t> workload_issue_dep_free_calls{0};
std::atomic<uint64_t> workload_ready_initial_enqueued{0};
std::atomic<uint64_t> workload_ready_released_enqueued{0};
std::atomic<uint64_t> workload_ready_dequeued{0};
std::atomic<int64_t> workload_ready_set_nodes{0};
std::atomic<int64_t> workload_ready_queue_nodes{0};
std::atomic<uint64_t> workload_nodes_total{0};
std::atomic<uint64_t> workload_nodes_finished{0};

std::atomic<uint64_t> workload_issue_total{0};
std::atomic<uint64_t> workload_issue_metadata{0};
std::atomic<uint64_t> workload_issue_mem_load{0};
std::atomic<uint64_t> workload_issue_mem_store{0};
std::atomic<uint64_t> workload_issue_comp{0};
std::atomic<uint64_t> workload_issue_coll{0};
std::atomic<uint64_t> workload_issue_send{0};
std::atomic<uint64_t> workload_issue_recv{0};
std::atomic<uint64_t> workload_issue_invalid{0};
std::atomic<uint64_t> workload_issue_other{0};

}  // namespace ProgressCounters
}  // namespace AstraSim
