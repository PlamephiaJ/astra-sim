#ifndef ASTRA_SIM_NETWORK_FRONTEND_NS3_SLACK_ROUTING_POLICY_HH
#define ASTRA_SIM_NETWORK_FRONTEND_NS3_SLACK_ROUTING_POLICY_HH

#include <cstdint>
#include <string>
#include <unordered_map>

namespace AstraSim {

class SlackRoutingPolicy {
  public:
    void initialize(const std::string& dag_filename,
                    const std::string& oracle_timings_filename);

    int32_t routing_label_for(const std::string& collective_name) const;

  private:
    std::unordered_map<std::string, int32_t> collective_labels_;
};

}  // namespace AstraSim

#endif
