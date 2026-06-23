#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>

#include "extern/graph_frontend/chakra/src/feeder_v3/et_feeder.h"

using Chakra::FeederV3::ETFeeder;

std::string node_summary(const ChakraProtoMsg::Node& node) {
  return "id=" + std::to_string(node.id()) + " name=" + node.name() +
      " type=" + std::to_string(static_cast<int>(node.type())) +
      " data_deps=" + std::to_string(node.data_deps_size()) +
      " ctrl_deps=" + std::to_string(node.ctrl_deps_size()) +
      " attrs=" + std::to_string(node.attr_size());
}

uint64_t compare_rank(
    const std::string& compact_prefix,
    const std::string& expanded_prefix,
    int rank,
    uint64_t max_nodes_per_rank) {
  const std::string compact_file =
      compact_prefix + "." + std::to_string(rank) + ".et";
  const std::string expanded_file =
      expanded_prefix + "." + std::to_string(rank) + ".et";

  ETFeeder compact(compact_file);
  ETFeeder expanded(expanded_file);

  if (compact.global_metadata.SerializeAsString() !=
      expanded.global_metadata.SerializeAsString()) {
    throw std::runtime_error(
        "rank " + std::to_string(rank) + ": global metadata mismatch");
  }

  uint64_t compared = 0;
  while (true) {
    const bool compact_has = compact.hasNodesToIssue();
    const bool expanded_has = expanded.hasNodesToIssue();
    if (compact_has != expanded_has) {
      throw std::runtime_error(
          "rank " + std::to_string(rank) +
          ": issuable-node availability mismatch after " +
          std::to_string(compared) + " nodes");
    }
    if (!compact_has)
      break;

    auto compact_node = compact.getNextIssuableNode();
    auto expanded_node = expanded.getNextIssuableNode();
    if (compact_node->id() != expanded_node->id()) {
      throw std::runtime_error(
          "rank " + std::to_string(rank) + ": issue-order mismatch after " +
          std::to_string(compared) + " nodes: compact_id=" +
          std::to_string(compact_node->id()) + " expanded_id=" +
          std::to_string(expanded_node->id()));
    }

    const auto compact_msg = compact.debugGetNodeCopy(compact_node->id());
    const auto expanded_msg = expanded.debugGetNodeCopy(expanded_node->id());
    if (compact_msg.SerializeAsString() != expanded_msg.SerializeAsString()) {
      throw std::runtime_error(
          "rank " + std::to_string(rank) +
          ": materialized protobuf mismatch after " +
          std::to_string(compared) + " nodes\ncompact: " +
          node_summary(compact_msg) + "\nexpanded: " +
          node_summary(expanded_msg));
    }

    compact.freeChildrenNodes(compact_node->id());
    expanded.freeChildrenNodes(expanded_node->id());
    ++compared;

    if (max_nodes_per_rank > 0 && compared >= max_nodes_per_rank)
      break;
  }

  return compared;
}

int main(int argc, char** argv) {
  if (argc != 4 && argc != 5) {
    std::cerr << "usage: CompareRepeatedEtFeeder COMPACT_PREFIX "
                 "EXPANDED_PREFIX RANKS [MAX_NODES_PER_RANK]\n";
    return 2;
  }

  const std::string compact_prefix = argv[1];
  const std::string expanded_prefix = argv[2];
  const int ranks = std::stoi(argv[3]);
  const uint64_t max_nodes_per_rank =
      argc == 5 ? std::stoull(argv[4]) : 0;

  uint64_t total_nodes = 0;
  try {
    for (int rank = 0; rank < ranks; ++rank) {
      const uint64_t rank_nodes = compare_rank(
          compact_prefix, expanded_prefix, rank, max_nodes_per_rank);
      total_nodes += rank_nodes;
      if ((rank + 1) % 32 == 0 || rank + 1 == ranks) {
        std::cout << "compared " << (rank + 1) << "/" << ranks
                  << " ranks, total_nodes=" << total_nodes << '\n';
      }
    }
  } catch (const std::exception& e) {
    std::cerr << "CompareRepeatedEtFeeder failed: " << e.what() << '\n';
    std::cerr << "compared_nodes_before_failure=" << total_nodes << '\n';
    return 1;
  }

  std::cout << "RESULT PASS ranks=" << ranks
            << " compared_nodes=" << total_nodes;
  if (max_nodes_per_rank > 0)
    std::cout << " max_nodes_per_rank=" << max_nodes_per_rank;
  std::cout << '\n';
  return 0;
}
