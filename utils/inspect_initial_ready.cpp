#include <algorithm>
#include <cstdint>
#include <iostream>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>

#include "extern/graph_frontend/chakra/src/feeder_v3/et_feeder.h"

using Chakra::FeederV3::ETFeeder;
using Chakra::FeederV3::ETFeederNode;
using Chakra::FeederV3::NodeId;
using ChakraProtoMsg::NodeType;

struct Counts {
  uint64_t metadata = 0;
  uint64_t mem_load = 0;
  uint64_t mem_store = 0;
  uint64_t comp = 0;
  uint64_t coll = 0;
  uint64_t send = 0;
  uint64_t recv = 0;
  uint64_t invalid = 0;
  uint64_t other = 0;

  void add(NodeType type) {
    switch (type) {
      case ChakraProtoMsg::METADATA_NODE:
        ++metadata;
        break;
      case ChakraProtoMsg::MEM_LOAD_NODE:
        ++mem_load;
        break;
      case ChakraProtoMsg::MEM_STORE_NODE:
        ++mem_store;
        break;
      case ChakraProtoMsg::COMP_NODE:
        ++comp;
        break;
      case ChakraProtoMsg::COMM_COLL_NODE:
        ++coll;
        break;
      case ChakraProtoMsg::COMM_SEND_NODE:
        ++send;
        break;
      case ChakraProtoMsg::COMM_RECV_NODE:
        ++recv;
        break;
      case ChakraProtoMsg::INVALID_NODE:
        ++invalid;
        break;
      default:
        ++other;
        break;
    }
  }

  Counts& operator+=(const Counts& rhs) {
    metadata += rhs.metadata;
    mem_load += rhs.mem_load;
    mem_store += rhs.mem_store;
    comp += rhs.comp;
    coll += rhs.coll;
    send += rhs.send;
    recv += rhs.recv;
    invalid += rhs.invalid;
    other += rhs.other;
    return *this;
  }

  uint64_t total() const {
    return metadata + mem_load + mem_store + comp + coll + send + recv +
        invalid + other;
  }
};

std::ostream& operator<<(std::ostream& os, const Counts& c) {
  os << "total=" << c.total()
     << " metadata=" << c.metadata
     << " mem_load=" << c.mem_load
     << " mem_store=" << c.mem_store
     << " comp=" << c.comp
     << " coll=" << c.coll
     << " send=" << c.send
     << " recv=" << c.recv
     << " invalid=" << c.invalid
     << " other=" << c.other;
  return os;
}

std::string type_name(NodeType type) {
  switch (type) {
    case ChakraProtoMsg::METADATA_NODE:
      return "METADATA";
    case ChakraProtoMsg::MEM_LOAD_NODE:
      return "MEM_LOAD";
    case ChakraProtoMsg::MEM_STORE_NODE:
      return "MEM_STORE";
    case ChakraProtoMsg::COMP_NODE:
      return "COMP";
    case ChakraProtoMsg::COMM_COLL_NODE:
      return "COMM_COLL";
    case ChakraProtoMsg::COMM_SEND_NODE:
      return "COMM_SEND";
    case ChakraProtoMsg::COMM_RECV_NODE:
      return "COMM_RECV";
    case ChakraProtoMsg::INVALID_NODE:
      return "INVALID";
    default:
      return "OTHER";
  }
}

struct ResourceState {
  uint64_t cpu = 0;
  uint64_t gpu_comp = 0;
  uint64_t gpu_comm = 0;

  bool is_available(const std::shared_ptr<ETFeederNode>& node) const {
    if (node->is_cpu_op<bool>(false))
      return cpu == 0;
    const auto type = node->type();
    if (type == ChakraProtoMsg::COMP_NODE)
      return gpu_comp == 0;
    if (gpu_comm == 0)
      return true;
    if (type == ChakraProtoMsg::COMM_RECV_NODE)
      return true;
    return false;
  }

  void occupy(const std::shared_ptr<ETFeederNode>& node) {
    if (node->is_cpu_op<bool>(false)) {
      ++cpu;
      return;
    }
    const auto type = node->type();
    if (type == ChakraProtoMsg::COMP_NODE) {
      ++gpu_comp;
      return;
    }
    if (type == ChakraProtoMsg::COMM_RECV_NODE)
      return;
    ++gpu_comm;
  }
};

void print_sample(
    int rank,
    uint64_t index,
    const std::shared_ptr<ETFeederNode>& node,
    const ChakraProtoMsg::Node& msg) {
  std::cout << "sample rank=" << rank
            << " index=" << index
            << " id=" << node->id()
            << " type=" << type_name(node->type())
            << " name=" << node->name()
            << " data_deps=" << msg.data_deps_size()
            << " ctrl_deps=" << msg.ctrl_deps_size();
  if (node->type() == ChakraProtoMsg::COMM_COLL_NODE) {
    std::cout << " comm_type=" << node->comm_type<uint64_t>()
              << " comm_size=" << node->comm_size<uint64_t>();
    if (node->has_attr("involved_dim")) {
      const auto attr = node->get_attr_msg("involved_dim");
      if (attr.has_bool_list()) {
        std::cout << " involved_dim=";
        for (int i = 0; i < attr.bool_list().values_size(); ++i)
          std::cout << (attr.bool_list().values(i) ? "1" : "0");
      }
    }
  }
  if (node->type() == ChakraProtoMsg::COMM_SEND_NODE ||
      node->type() == ChakraProtoMsg::COMM_RECV_NODE) {
    std::cout << " src=" << node->comm_src<uint32_t>(rank)
              << " dst=" << node->comm_dst<uint32_t>(rank)
              << " tag=" << node->comm_tag<uint32_t>()
              << " size=" << node->comm_size<uint64_t>();
  }
  std::cout << '\n';
}

int main(int argc, char** argv) {
  if (argc < 3 || argc > 5) {
    std::cerr << "usage: InspectInitialReady ET_PREFIX RANKS "
                 "[MAX_RANKS=RANKS] [SAMPLES_PER_RANK=3]\n";
    return 2;
  }

  const std::string prefix = argv[1];
  const int ranks = std::stoi(argv[2]);
  const int max_ranks = argc >= 4 ? std::stoi(argv[3]) : ranks;
  const uint64_t samples_per_rank = argc >= 5 ? std::stoull(argv[4]) : 3;
  const int limit = std::min(ranks, max_ranks);

  Counts ready_total;
  Counts issue_total;

  for (int rank = 0; rank < limit; ++rank) {
    const std::string file = prefix + "." + std::to_string(rank) + ".et";
    ETFeeder feeder(file);
    const auto& ready_ids =
        feeder.getDependancyResolver().get_dependancy_free_nodes();
    Counts ready_counts;
    Counts issue_counts;
    ResourceState resources;

    uint64_t sample_count = 0;
    uint64_t index = 0;
    for (NodeId node_id : ready_ids) {
      auto node = feeder.lookupNode(node_id);
      ready_counts.add(node->type());
      if (resources.is_available(node)) {
        resources.occupy(node);
        issue_counts.add(node->type());
      }
      if (sample_count < samples_per_rank) {
        const auto msg = feeder.debugGetNodeCopy(node_id);
        print_sample(rank, index, node, msg);
        ++sample_count;
      }
      ++index;
    }

    ready_total += ready_counts;
    issue_total += issue_counts;
    std::cout << "rank=" << rank
              << " ready{" << ready_counts << "}"
              << " would_issue{" << issue_counts << "}"
              << " final_resource_state{cpu=" << resources.cpu
              << " gpu_comp=" << resources.gpu_comp
              << " gpu_comm=" << resources.gpu_comm << "}\n";
  }

  std::cout << "AGGREGATE ranks=" << limit
            << " ready{" << ready_total << "}"
            << " would_issue{" << issue_total << "}\n";
  return 0;
}
