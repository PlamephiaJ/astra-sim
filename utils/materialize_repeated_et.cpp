#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>

#include "extern/graph_frontend/chakra/schema/protobuf/et_def.pb.h"

namespace fs = std::filesystem;
using ChakraProtoMsg::GlobalMetadata;
using ChakraProtoMsg::Node;

bool read_varint32(std::istream& input, uint32_t& value) {
  value = 0;
  for (int shift = 0; shift <= 28; shift += 7) {
    uint8_t byte = 0;
    if (!input.read(reinterpret_cast<char*>(&byte), 1))
      return false;
    value |= static_cast<uint32_t>(byte & 0x7f) << shift;
    if ((byte & 0x80) == 0)
      return true;
  }
  throw std::runtime_error("invalid protobuf frame length");
}

void write_varint32(std::ostream& output, uint32_t value) {
  while (value > 0x7f) {
    const uint8_t byte = static_cast<uint8_t>((value & 0x7f) | 0x80);
    output.write(reinterpret_cast<const char*>(&byte), 1);
    value >>= 7;
  }
  const uint8_t byte = static_cast<uint8_t>(value);
  output.write(reinterpret_cast<const char*>(&byte), 1);
}

template <typename Message>
bool read_message(std::istream& input, Message& message) {
  uint32_t size = 0;
  if (!read_varint32(input, size))
    return false;
  std::string bytes(size, '\0');
  if (!input.read(bytes.data(), size))
    throw std::runtime_error("truncated protobuf message");
  if (!message.ParseFromString(bytes))
    throw std::runtime_error("invalid protobuf message");
  return true;
}

template <typename Message>
void write_message(std::ostream& output, const Message& message) {
  const std::string bytes = message.SerializeAsString();
  write_varint32(output, static_cast<uint32_t>(bytes.size()));
  output.write(bytes.data(), bytes.size());
}

void materialize_file(
    const fs::path& source,
    const fs::path& destination,
    uint64_t repeat_count,
    uint64_t id_offset) {
  std::ifstream input(source, std::ios::binary);
  if (!input)
    throw std::runtime_error("cannot open input: " + source.string());
  fs::create_directories(destination.parent_path());
  std::ofstream output(destination, std::ios::binary | std::ios::trunc);
  if (!output)
    throw std::runtime_error("cannot open output: " + destination.string());

  GlobalMetadata metadata;
  if (!read_message(input, metadata))
    throw std::runtime_error("missing metadata: " + source.string());
  write_message(output, metadata);

  Node base;
  while (read_message(input, base)) {
    if (base.id() >= id_offset)
      throw std::runtime_error("input is not a base-pattern ET");
    for (uint64_t replica = 0; replica < repeat_count; ++replica) {
      Node node = base;
      if (replica > 0) {
        const uint64_t delta = replica * id_offset;
        node.set_id(base.id() + delta);
        node.set_name("mb" + std::to_string(replica) + "." + base.name());
        for (auto& dependency : *node.mutable_data_deps())
          dependency += delta;
        for (auto& dependency : *node.mutable_ctrl_deps())
          dependency += delta;
      }
      write_message(output, node);
    }
  }
}

int main(int argc, char** argv) {
  if (argc != 6) {
    std::cerr << "usage: MaterializeRepeatedEt INPUT_PREFIX OUTPUT_PREFIX "
                 "RANKS REPEAT_COUNT ID_OFFSET\n";
    return 2;
  }
  const std::string input_prefix = argv[1];
  const std::string output_prefix = argv[2];
  const int ranks = std::stoi(argv[3]);
  const uint64_t repeat_count = std::stoull(argv[4]);
  const uint64_t id_offset = std::stoull(argv[5]);
  for (int rank = 0; rank < ranks; ++rank) {
    materialize_file(
        input_prefix + "." + std::to_string(rank) + ".et",
        output_prefix + "." + std::to_string(rank) + ".et",
        repeat_count,
        id_offset);
    if ((rank + 1) % 32 == 0 || rank + 1 == ranks)
      std::cout << "materialized " << rank + 1 << "/" << ranks << '\n';
  }
  return 0;
}
