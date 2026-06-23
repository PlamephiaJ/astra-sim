#include <cassert>
#include <cstdint>
#include <iostream>
#include <string>

#include "extern/graph_frontend/chakra/src/feeder_v3/et_feeder.h"

int main(int argc, char** argv) {
  if (argc != 2) {
    std::cerr << "usage: repeated_feeder_smoke BASE_PATTERN.et\n";
    return 2;
  }

  constexpr uint64_t kIdOffset = 1000000000ULL;
  Chakra::FeederV3::ETFeeder feeder(argv[1]);
  auto& resolver = feeder.getDependancyResolver();
  const auto& ready = resolver.get_dependancy_free_nodes();
  assert(!ready.empty());

  auto repeated = ready.end();
  for (auto it = ready.begin(); it != ready.end(); ++it) {
    if (*it >= kIdOffset) {
      repeated = it;
      break;
    }
  }
  assert(repeated != ready.end());

  const uint64_t virtual_id = *repeated;
  auto node = feeder.lookupNode(virtual_id);
  assert(node->id() == virtual_id);
  assert(node->name().rfind("mb", 0) == 0);

  resolver.take_node(virtual_id);
  resolver.push_back_node(virtual_id);
  assert(resolver.get_dependancy_free_nodes().count(virtual_id) == 1);

  std::cout << "ready=" << ready.size() << " virtual_id=" << virtual_id
            << " name=" << node->name() << '\n';
  return 0;
}
