#include "astra-sim/network_frontend/ns3/SlackRoutingPolicy.hh"

#include "ns3/rdma-routing-label.h"
#include <json/json.hpp>

#include <algorithm>
#include <fstream>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <unordered_map>
#include <utility>
#include <vector>

namespace {

using json = nlohmann::json;

struct DagNode {
    std::string name;
    std::string type;
    uint64_t time_ns;
    std::vector<std::string> parents;
    std::vector<std::string> children;
};

struct Branch {
    std::vector<std::string> nodes;
    uint64_t total_time_ns;
};

std::unordered_map<std::string, uint64_t> load_oracle_timings(
    const std::string& filename) {
    std::ifstream input(filename);
    if (!input) {
        throw std::runtime_error("Cannot open oracle timings: " + filename);
    }

    std::unordered_map<std::string, uint64_t> timings;
    std::string node_name;
    uint64_t time_ns = 0;
    while (input >> node_name >> time_ns) {
        if (!timings.emplace(node_name, time_ns).second) {
            throw std::runtime_error(
                "Duplicate oracle timing for DAG node '" + node_name + "'");
        }
    }
    if (!input.eof()) {
        throw std::runtime_error("Malformed oracle timings: " + filename);
    }
    return timings;
}

void enumerate_branches(
    const std::string& node_name,
    const std::unordered_map<std::string, DagNode>& nodes,
    std::vector<std::string>& current_nodes,
    uint64_t current_time_ns,
    std::vector<Branch>& branches) {
    const DagNode& node = nodes.at(node_name);
    if (current_time_ns >
        std::numeric_limits<uint64_t>::max() - node.time_ns) {
        throw std::runtime_error("DAG branch time overflow at node '" +
                                 node_name + "'");
    }

    current_nodes.push_back(node_name);
    current_time_ns += node.time_ns;

    if (node.children.empty()) {
        branches.push_back({current_nodes, current_time_ns});
    } else {
        for (const std::string& child : node.children) {
            enumerate_branches(child, nodes, current_nodes, current_time_ns,
                               branches);
        }
    }
    current_nodes.pop_back();
}

std::string format_branch(const std::vector<std::string>& nodes) {
    std::string result;
    for (const std::string& node : nodes) {
        if (!result.empty()) {
            result += "->";
        }
        result += node;
    }
    return result;
}

}  // namespace

namespace AstraSim {

void SlackRoutingPolicy::initialize(
    const std::string& dag_filename,
    const std::string& oracle_timings_filename) {
    std::ifstream input(dag_filename);
    if (!input) {
        throw std::runtime_error("Cannot open routing DAG: " + dag_filename);
    }

    json dag;
    input >> dag;
    const auto& raw_nodes = dag.at("nodes");
    if (!raw_nodes.is_array() || raw_nodes.empty()) {
        throw std::runtime_error("Routing DAG contains no nodes");
    }

    const auto oracle_timings =
        load_oracle_timings(oracle_timings_filename);
    std::unordered_map<std::string, DagNode> nodes;
    std::vector<std::string> node_order;
    node_order.reserve(raw_nodes.size());

    for (const auto& raw_node : raw_nodes) {
        const std::string name = raw_node.at("name").get<std::string>();
        const auto timing_it = oracle_timings.find(name);
        if (timing_it == oracle_timings.end()) {
            throw std::runtime_error(
                "Missing oracle timing for DAG node '" + name + "'");
        }

        DagNode node{
            name,
            raw_node.at("type").get<std::string>(),
            timing_it->second,
            raw_node.value("depends_on", std::vector<std::string>{}),
            {},
        };
        if (!nodes.emplace(name, std::move(node)).second) {
            throw std::runtime_error("Duplicate DAG node name '" + name + "'");
        }
        node_order.push_back(name);
    }

    for (const std::string& name : node_order) {
        for (const std::string& parent : nodes.at(name).parents) {
            auto parent_it = nodes.find(parent);
            if (parent_it == nodes.end()) {
                throw std::runtime_error("DAG node '" + name +
                                         "' has unknown parent '" + parent +
                                         "'");
            }
            parent_it->second.children.push_back(name);
        }
    }

    std::vector<std::string> roots;
    for (const std::string& name : node_order) {
        if (nodes.at(name).parents.empty()) {
            roots.push_back(name);
        }
        std::cout << "SLACK_NODE name=" << name
                  << " t_ns=" << nodes.at(name).time_ns
                  << " source=oracle" << std::endl;
    }
    if (roots.empty()) {
        throw std::runtime_error("Routing DAG has no root node");
    }

    std::vector<Branch> branches;
    std::vector<std::string> current_nodes;
    for (const std::string& root : roots) {
        enumerate_branches(root, nodes, current_nodes, 0, branches);
    }
    if (branches.empty()) {
        throw std::runtime_error("Routing DAG has no root-to-leaf branch");
    }

    uint64_t collective_ref_ns = 0;
    for (const Branch& branch : branches) {
        collective_ref_ns =
            std::max(collective_ref_ns, branch.total_time_ns);
    }

    uint64_t minimum_slack_ns = std::numeric_limits<uint64_t>::max();
    for (const Branch& branch : branches) {
        minimum_slack_ns = std::min(
            minimum_slack_ns,
            collective_ref_ns - branch.total_time_ns);
    }

    std::unordered_map<std::string, uint64_t> collective_slack;
    for (size_t index = 0; index < branches.size(); ++index) {
        const Branch& branch = branches[index];
        const uint64_t slack_ns =
            collective_ref_ns - branch.total_time_ns;
        const int32_t label =
            slack_ns == minimum_slack_ns ? ns3::kShortRoutingLabel
                                         : ns3::kLongRoutingLabel;

        std::cout << "SLACK_BRANCH index=" << index
                  << " total_ns=" << branch.total_time_ns
                  << " ref_ns=" << collective_ref_ns
                  << " slack_ns=" << slack_ns
                  << " label=" << label
                  << " route=" << ns3::RoutingLabelName(label)
                  << " nodes=" << format_branch(branch.nodes) << std::endl;

        for (const std::string& name : branch.nodes) {
            if (nodes.at(name).type != "collective") {
                continue;
            }
            auto [it, inserted] = collective_slack.emplace(name, slack_ns);
            if (!inserted) {
                it->second = std::min(it->second, slack_ns);
            }
        }
    }

    collective_labels_.clear();
    for (const std::string& name : node_order) {
        const auto slack_it = collective_slack.find(name);
        if (slack_it == collective_slack.end()) {
            continue;
        }
        const uint64_t slack_ns = slack_it->second;
        const int32_t label =
            slack_ns == minimum_slack_ns ? ns3::kShortRoutingLabel
                                         : ns3::kLongRoutingLabel;
        collective_labels_.emplace(name, label);
        std::cout << "SLACK_COLLECTIVE name=" << name
                  << " slack_ns=" << slack_ns
                  << " label=" << label
                  << " route=" << ns3::RoutingLabelName(label) << std::endl;
    }
}

int32_t SlackRoutingPolicy::routing_label_for(
    const std::string& collective_name) const {
    const auto it = collective_labels_.find(collective_name);
    if (it == collective_labels_.end()) {
        throw std::runtime_error(
            "No slack routing result for collective '" + collective_name +
            "'");
    }
    return it->second;
}

}  // namespace AstraSim
