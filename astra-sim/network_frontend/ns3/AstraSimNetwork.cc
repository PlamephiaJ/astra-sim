#include "astra-sim/common/AstraNetworkAPI.hh"
#include "astra-sim/network_frontend/ns3/SlackRoutingPolicy.hh"
#include "astra-sim/system/Sys.hh"
#include "extern/remote_memory_backend/analytical/AnalyticalRemoteMemory.hh"
#include <json/json.hpp>

// monkey patch, the spdlog include <syslog.h> and define these macros, and
// break the ns3 log enum keys
#define NS3_LOG_COMPAT_UNDEF_SYSLOG
#include "astra-sim/network_frontend/ns3/ns3_log_monkey_patch.h"

#include "entry.h"
#include "ns3/applications-module.h"
#include "ns3/core-module.h"
#include "ns3/csma-module.h"
#include "ns3/internet-module.h"
#include "ns3/network-module.h"

#undef NS3_LOG_COMPAT_UNDEF_SYSLOG
#include "astra-sim/network_frontend/ns3/ns3_log_monkey_patch.h"

#include <execinfo.h>
#include <fstream>
#include <iostream>
#include <queue>
#include <stdio.h>
#include <stdexcept>
#include <string>
#include <thread>
#include <unistd.h>
#include <vector>

using namespace std;
using namespace ns3;
using json = nlohmann::json;

extern double comm_scale;

static AstraSim::SlackRoutingPolicy routing_policy;
static bool profile_all_short = false;
static bool use_backend_ecmp = false;
static bool use_workload_label = false;

static uint64_t scale_message_size(uint64_t message_size) {
    uint64_t scaled_message_size =
        static_cast<uint64_t>(message_size * comm_scale);
    if (message_size > 0 && scaled_message_size == 0) {
        scaled_message_size = 1;
    }
    return scaled_message_size;
}

// Return the route selector consumed by NS-3:
//  -1 -> backend-default flow-stable ECMP
//   0 -> short route
//   1 -> long route
static int32_t select_routing_label(
    uint64_t message_bytes, const AstraSim::sim_request* request) {
    (void)message_bytes;
    if (use_backend_ecmp) {
        return ns3::kDefaultRoutingLabel;
    }
    if (profile_all_short) {
        return ns3::kShortRoutingLabel;
    }
    if (request == nullptr) {
        throw std::runtime_error(
            "Routing requires communication request metadata");
    }
    if (use_workload_label) {
        if (request->routing_label != ns3::kShortRoutingLabel &&
            request->routing_label != ns3::kLongRoutingLabel) {
            throw std::runtime_error(
                "workload_label mode requires routing_label 0 or 1");
        }
        return request->routing_label;
    }
    return routing_policy.routing_label_for(request->collective_name);
}

/**
 * @class NS3BackendCompletionTracker
 * @brief Tracks the completion status of ranks in the NS3 backend.
 *
 * This is a hacky approach to track which ranks have completed their workload.
 * The purpose of this class is to end the ns3 simulation once all ranks have
 * completed. The hacky approach is necessary because each ASTRASimNetwork
 * instance only corresponds to one rank. That is, someone needs to keep track
 * of the completion status of all of the ranks. Because there is no exit point
 * once the ns3 simulator has started, we cannot implement such tracker in the
 * main function.
 */
class NS3BackendCompletionTracker {
  public:
    NS3BackendCompletionTracker(int num_ranks) {
        num_unfinished_ranks_ = num_ranks;
        completion_tracker_ = vector<int>(num_ranks, 0);
    }

    void mark_rank_as_finished(int rank) {
        if (completion_tracker_[rank] == 0) {
            completion_tracker_[rank] = 1;
            num_unfinished_ranks_--;
        }
        if (num_unfinished_ranks_ == 0) {
            AstraSim::LoggerFactory::get_logger("network")->debug(
                "All ranks have finished. Exiting simulation.");
            // cout << "All ranks have finished. Exiting simulation.\n";
            Simulator::Stop();
            Simulator::Destroy();
            exit(0);
        }
    }

  private:
    int num_unfinished_ranks_;
    vector<int> completion_tracker_;
};

class ASTRASimNetwork : public AstraSim::AstraNetworkAPI {
  public:
    ASTRASimNetwork(int rank, NS3BackendCompletionTracker* completion_tracker)
        : AstraNetworkAPI(rank) {
        completion_tracker_ = completion_tracker;
    }

    ~ASTRASimNetwork() {}

    void sim_notify_finished() {
        // Output to file instead of stdout
        /*
        for (auto it = node_to_bytes_sent_map.begin();
             it != node_to_bytes_sent_map.end(); it++) {
            pair<int, int> p = it->first;
            if (p.second == 0) {
                cout << "All data sent from node " << p.first << " is "
                     << it->second << "\n";
            } else {
                cout << "All data received by node " << p.first << " is "
                     << it->second << "\n";
            }
        }
        */
        completion_tracker_->mark_rank_as_finished(rank);
        return;
    }

    double sim_time_resolution() {
        return 0;
    }

    void handleEvent(int dst, int cnt) {}

    AstraSim::timespec_t sim_get_time() {
        AstraSim::timespec_t timeSpec;
        timeSpec.time_res = AstraSim::NS;
        timeSpec.time_val = Simulator::Now().GetNanoSeconds();
        return timeSpec;
    }

    virtual void sim_schedule(AstraSim::timespec_t delta,
                              void (*fun_ptr)(void* fun_arg),
                              void* fun_arg) {
        Simulator::Schedule(NanoSeconds(delta.time_val), fun_ptr, fun_arg);
        return;
    }

    virtual int sim_send(void* buffer,
                         uint64_t message_size,
                         int type,
                         int dst_id,
                         int tag,
                         AstraSim::sim_request* request,
                         void (*msg_handler)(void* fun_arg),
                         void* fun_arg) {
        int src_id = rank;
        message_size = scale_message_size(message_size);
        const int32_t routing_label =
            select_routing_label(message_size, request);

        // Trigger ns3 to schedule RDMA QP event.
        send_flow(src_id, dst_id, message_size, msg_handler, fun_arg, tag,
                  routing_label, request->flow_id);
        return 0;
    }

    virtual int sim_recv(void* buffer,
                         uint64_t message_size,
                         int type,
                         int src_id,
                         int tag,
                         AstraSim::sim_request* request,
                         void (*msg_handler)(void* fun_arg),
                         void* fun_arg) {
        int dst_id = rank;
        message_size = scale_message_size(message_size);
        MsgEvent recv_event =
            MsgEvent(src_id, dst_id, 1, message_size, fun_arg, msg_handler);
        MsgEventKey recv_event_key =
            make_pair(tag, make_pair(recv_event.src_id, recv_event.dst_id));

        if (received_msg_standby_hash.find(recv_event_key) !=
            received_msg_standby_hash.end()) {
            // 1) ns3 has already received some message before sim_recv is
            // called.
            int received_msg_bytes = received_msg_standby_hash[recv_event_key];
            if (received_msg_bytes == message_size) {
                // 1-1) The received message size is same as what we expect.
                // Exit.
                received_msg_standby_hash.erase(recv_event_key);
                recv_event.callHandler();
            } else if (received_msg_bytes > message_size) {
                // 1-2) The node received more than expected.
                // Do trigger the callback handler for this message, but wait
                // for Sys layer to call sim_recv for more messages.
                received_msg_standby_hash[recv_event_key] =
                    received_msg_bytes - message_size;
                recv_event.callHandler();
            } else {
                // 1-3) The node received less than what we expected.
                // Reduce the number of bytes we are waiting to receive.
                received_msg_standby_hash.erase(recv_event_key);
                recv_event.remaining_msg_bytes -= received_msg_bytes;
                sim_recv_waiting_hash[recv_event_key].push(recv_event);
            }
        } else {
            // 2) ns3 has not yet received anything.
            sim_recv_waiting_hash[recv_event_key].push(recv_event);
        }
        return 0;
    }

  private:
    NS3BackendCompletionTracker* completion_tracker_;
};

// Command line arguments and default values.
string workload_configuration;
string routing_dag_configuration;
string oracle_timing_configuration;
string flow_routing_configuration;
uint64_t ugal_local_bias_bytes = 0;
string routing_mode = "oracle";
string system_configuration;
string network_configuration;
string memory_configuration;
string comm_group_configuration = "empty";
string logical_topology_configuration;
string logging_configuration = "empty";
int num_queues_per_dim = 1;
double comm_scale = 1;
double injection_scale = 1;
bool rendezvous_protocol = false;
auto logical_dims = vector<int>();
int num_npus = 1;
auto queues_per_dim = vector<int>();

// TODO: Migrate to yaml
void read_logical_topo_config(string network_configuration,
                              vector<int>& logical_dims) {
    ifstream inFile;
    inFile.open(network_configuration);
    if (!inFile) {
        cerr << "Unable to open file: " << network_configuration << endl;
        exit(1);
    }

    // Find the size of each dimension.
    json j;
    inFile >> j;
    if (j.contains("logical-dims")) {
        vector<string> logical_dims_str_vec = j["logical-dims"];
        for (auto logical_dims_str : logical_dims_str_vec) {
            logical_dims.push_back(stoi(logical_dims_str));
        }
    }

    // Find the number of all npus.
    stringstream dimstr;
    for (auto num_npus_per_dim : logical_dims) {
        num_npus *= num_npus_per_dim;
        dimstr << num_npus_per_dim << ",";
    }
    cout << "There are " << num_npus << " npus: " << dimstr.str() << "\n";

    queues_per_dim = vector<int>(logical_dims.size(), num_queues_per_dim);
}

static void update_ugal_l_qp_route(uint32_t sip, uint32_t dip,
                                   uint16_t sport, uint16_t dport,
                                   uint16_t pg, bool reverse_direction,
                                   uint64_t extra_rtt) {
    const uint32_t data_sip = reverse_direction ? dip : sip;
    const uint32_t data_dip = reverse_direction ? sip : dip;
    const uint16_t data_sport = reverse_direction ? dport : sport;
    const uint32_t source_id = ip_to_node_id(Ipv4Address(data_sip));
    if (source_id >= n.GetN()) {
        throw std::runtime_error(
            "UGAL-L route decision references an invalid source rank");
    }

    Ptr<RdmaDriver> driver = n.Get(source_id)->GetObject<RdmaDriver>();
    if (driver == nullptr || driver->m_rdma == nullptr) {
        throw std::runtime_error(
            "UGAL-L route decision cannot find the source RDMA driver");
    }
    Ptr<RdmaQueuePair> qp =
        driver->m_rdma->GetQp(data_dip, data_sport, pg);
    if (qp == nullptr) {
        throw std::runtime_error(
            "UGAL-L route decision cannot find the source queue pair");
    }

    qp->UpdateRouteRtt(reverse_direction, extra_rtt);
    cout << "NS3_UGAL_QP_UPDATE src=" << qp->GetSrc()
         << " dst=" << qp->GetDest()
         << " sport=" << qp->sport
         << " direction=" << (reverse_direction ? "reverse" : "forward")
         << " extra_rtt_ns=" << extra_rtt
         << " base_rtt_ns=" << qp->m_baseRtt
         << " window_bytes=" << qp->m_win << endl;
}

static void set_backend_flow_routing_strategy(
    ns3::FlowRoutingStrategy strategy, uint64_t ugal_bias_bytes) {
    for (uint32_t node_id = 0; node_id < n.GetN(); ++node_id) {
        Ptr<SwitchNode> switch_node = DynamicCast<SwitchNode>(n.Get(node_id));
        if (switch_node != nullptr) {
            switch_node->SetFlowRoutingStrategy(strategy, ugal_bias_bytes);
            switch_node->SetRoutingDecisionLogging(true);
            if (strategy == ns3::FlowRoutingStrategy::UGAL_L) {
                switch_node->SetUgalLRouteDecisionCallback(
                    MakeCallback(&update_ugal_l_qp_route));
            }
        }
    }
}

static void configure_ugal_l() {
    if (flow_routing_configuration.empty()) {
        throw std::runtime_error(
            "ugal_l requires --flow-routing-configuration");
    }

    ifstream input(flow_routing_configuration);
    if (!input) {
        throw std::runtime_error(
            "Unable to open UGAL-L routing configuration: " +
            flow_routing_configuration);
    }

    json config;
    input >> config;
    if (config.value("strategy", "") != "ugal_l") {
        throw std::runtime_error(
            "UGAL-L routing configuration must declare strategy=ugal_l");
    }

    set_backend_flow_routing_strategy(
        ns3::FlowRoutingStrategy::UGAL_L, ugal_local_bias_bytes);

    uint32_t configured_routes = 0;
    for (const auto& route : config.at("routes")) {
        const uint32_t switch_id = route.at("switch").get<uint32_t>();
        const uint32_t next_hop_id =
            route.at("nonminimal_next_hop").get<uint32_t>();
        const uint32_t minimal_hops =
            route.at("minimal_hops").get<uint32_t>();
        const uint32_t nonminimal_hops =
            route.at("nonminimal_hops").get<uint32_t>();

        if (switch_id >= n.GetN() || next_hop_id >= n.GetN() ||
            minimal_hops == 0 || nonminimal_hops <= minimal_hops) {
            throw std::runtime_error("Invalid UGAL-L route metadata");
        }

        Ptr<Node> switch_base = n.Get(switch_id);
        Ptr<Node> next_hop = n.Get(next_hop_id);
        Ptr<SwitchNode> switch_node =
            DynamicCast<SwitchNode>(switch_base);
        if (switch_node == nullptr) {
            throw std::runtime_error(
                "UGAL-L source node is not a switch");
        }

        auto neighbor = nbr2if[switch_base].find(next_hop);
        if (neighbor == nbr2if[switch_base].end() ||
            !neighbor->second.up) {
            throw std::runtime_error(
                "UGAL-L non-minimal next hop is not an active neighbor");
        }

        for (const auto& destination : route.at("destinations")) {
            const uint32_t destination_id = destination.get<uint32_t>();
            if (destination_id >= static_cast<uint32_t>(num_npus)) {
                throw std::runtime_error(
                    "UGAL-L destination is not an active rank");
            }
            Ptr<Node> destination_node = n.Get(destination_id);
            Ipv4Address destination_address =
                destination_node->GetObject<Ipv4>()->GetAddress(1, 0).GetLocal();
            const uint64_t minimal_delay =
                pairDelay.at(switch_base).at(destination_node);
            const uint64_t minimal_tx_delay =
                pairTxDelay.at(switch_base).at(destination_node);
            const uint64_t nonminimal_delay = neighbor->second.delay +
                pairDelay.at(next_hop).at(destination_node);
            const uint64_t first_hop_tx_delay =
                packet_payload_size * 1000000000lu * 8 /
                neighbor->second.bw;
            const uint64_t nonminimal_tx_delay = first_hop_tx_delay +
                pairTxDelay.at(next_hop).at(destination_node);
            if (nonminimal_delay < minimal_delay ||
                nonminimal_tx_delay < minimal_tx_delay) {
                throw std::runtime_error(
                    "UGAL-L non-minimal path is shorter than minimal path");
            }
            const uint64_t nonminimal_extra_rtt =
                nonminimal_delay - minimal_delay +
                nonminimal_tx_delay - minimal_tx_delay;
            switch_node->AddUgalLRoute(
                destination_address, neighbor->second.idx, next_hop_id,
                minimal_hops, nonminimal_hops, nonminimal_extra_rtt);
            ++configured_routes;
        }
    }

    cout << "FLOW_ROUTING strategy=ugal_l routes=" << configured_routes
         << " bias_bytes=" << ugal_local_bias_bytes << endl;
}

// Read command line arguments.
void parse_args(int argc, char* argv[]) {
    CommandLine cmd;
    cmd.AddValue("workload-configuration", "Workload configuration file.",
                 workload_configuration);
    cmd.AddValue("routing-dag-configuration",
                 "High-level workload DAG used by slack routing.",
                 routing_dag_configuration);
    cmd.AddValue("oracle-timing-configuration",
                 "Measured DAG node timings used by oracle routing.",
                 oracle_timing_configuration);
    cmd.AddValue("flow-routing-configuration",
                 "Backend flow-routing strategy configuration.",
                 flow_routing_configuration);
    cmd.AddValue("ugal-local-bias-bytes",
                 "UGAL-L non-minimal-path bias in byte-hop cost units.",
                 ugal_local_bias_bytes);
    cmd.AddValue("routing-mode",
                 "Routing mode: ecmp, ugal_l, workload_label, "
                 "profile_all_short, or oracle.",
                 routing_mode);
    cmd.AddValue("system-configuration", "System configuration file",
                 system_configuration);
    cmd.AddValue("network-configuration", "Network configuration file",
                 network_configuration);
    cmd.AddValue("remote-memory-configuration", "Memory configuration file",
                 memory_configuration);
    cmd.AddValue("comm-group-configuration",
                 "Communicator group configuration file",
                 comm_group_configuration);
    cmd.AddValue("logical-topology-configuration",
                 "Logical topology configuration file",
                 logical_topology_configuration);
    cmd.AddValue("logging-configuration", "Logging configuration file",
                 logging_configuration);

    cmd.AddValue("num-queues-per-dim", "Number of queues per each dimension",
                 num_queues_per_dim);
    cmd.AddValue("comm-scale", "Communication scale", comm_scale);
    cmd.AddValue("injection-scale", "Injection scale", injection_scale);
    cmd.AddValue("rendezvous-protocol", "Whether to enable rendezvous protocol",
                 rendezvous_protocol);

    cmd.Parse(argc, argv);
}

int main(int argc, char* argv[]) {
    LogComponentEnable("OnOffApplication", LOG_INFO);
    LogComponentEnable("PacketSink", LOG_INFO);

    cout << "ASTRA-sim + NS3" << endl;

    // Read network config and find logical dims.
    parse_args(argc, argv);
    AstraSim::Workload::set_oracle_profiling_enabled(
        routing_mode == "profile_all_short");
    AstraSim::LoggerFactory::init(logging_configuration);
    read_logical_topo_config(logical_topology_configuration, logical_dims);

    // Setup network & System layer.
    vector<ASTRASimNetwork*> networks(num_npus, nullptr);
    vector<AstraSim::Sys*> systems(num_npus, nullptr);
    Analytical::AnalyticalRemoteMemory* mem =
        new Analytical::AnalyticalRemoteMemory(memory_configuration);
    NS3BackendCompletionTracker* completion_tracker =
        new NS3BackendCompletionTracker(num_npus);

    for (int npu_id = 0; npu_id < num_npus; npu_id++) {
        networks[npu_id] = new ASTRASimNetwork(npu_id, completion_tracker);
        systems[npu_id] = new AstraSim::Sys(
            npu_id, workload_configuration, comm_group_configuration,
            system_configuration, mem, networks[npu_id], logical_dims,
            queues_per_dim, injection_scale, comm_scale, rendezvous_protocol);
    }

    // Initialize ns3 simulation.
    if (auto ok = setup_ns3_simulation(network_configuration); ok == -1) {
        std::cerr << "Fail to setup ns3 simulation." << std::endl;
        return -1;
    }

    if (routing_mode == "ecmp") {
        use_backend_ecmp = true;
        set_backend_flow_routing_strategy(
            ns3::FlowRoutingStrategy::ECMP, 0);
    } else if (routing_mode == "ugal_l") {
        use_backend_ecmp = true;
        configure_ugal_l();
    } else if (routing_mode == "profile_all_short") {
        profile_all_short = true;
    } else if (routing_mode == "workload_label") {
        use_workload_label = true;
    } else if (routing_mode == "oracle") {
        routing_policy.initialize(routing_dag_configuration,
                                  oracle_timing_configuration);
    } else {
        throw std::runtime_error("Unknown routing mode '" + routing_mode + "'");
    }

    // Tell workload layer to schedule first events.
    for (int i = 0; i < num_npus; i++) {
        systems[i]->workload->fire();
    }

    // Run the simulation by triggering the ns3 event queue.
    Simulator::Run();
    return 0;
}
