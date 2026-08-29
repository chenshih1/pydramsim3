#ifndef PDRAMSIM3_CHANNEL_MAP_HPP
#define PDRAMSIM3_CHANNEL_MAP_HPP

#include <cstdint>
#include <fstream>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>

// Extracts DRAM channel from a physical address using the same mapping
// rules as DRAMsim3's Config::AddressMapping, without including or
// modifying any DRAMsim3 sources.  Used only to shard the software
// frontend queue; admission still goes through MemorySystem.
class ChannelMap {
 public:
  explicit ChannelMap(const std::string& config_file) {
    const Ini ini = parseIni(config_file);

    const std::string protocol =
        upper(ini.get("dram_structure", "protocol", "DDR3"));
    int bankgroups = ini.getInt("dram_structure", "bankgroups", 2);
    int banks_per_group = ini.getInt("dram_structure", "banks_per_group", 2);
    const bool bankgroup_enable =
        ini.getBool("dram_structure", "bankgroup_enable", true);
    if (!bankgroup_enable) {
      banks_per_group *= bankgroups;
      bankgroups = 1;
    }
    const int banks = bankgroups * banks_per_group;
    int rows = ini.getInt("dram_structure", "rows", 1 << 16);
    int columns = ini.getInt("dram_structure", "columns", 1 << 10);
    const int device_width = ini.getInt("dram_structure", "device_width", 8);
    int bl = ini.getInt("dram_structure", "BL", 8);
    channels_ = ini.getInt("system", "channels", 1);
    int channel_size = ini.getInt("system", "channel_size", 1024);
    const int bus_width = ini.getInt("system", "bus_width", 64);
    std::string address_mapping =
        ini.get("system", "address_mapping", "chrobabgraco");

    if (protocol == "HMC") {
      const int block_size = ini.getInt("hmc", "block_size", 64);
      bl = block_size * 8 / device_width;
    }
    if (bl == 0) {
      bl = (protocol == "HBM" || protocol == "HBM2") ? 4 : 8;
    }
    // Match DRAMsim3's physical-column normalization.
    if (protocol == "GDDR5" || protocol == "GDDR5X" || protocol == "GDDR6") {
      columns *= bl;
    } else if (protocol == "HBM" || protocol == "HBM2") {
      columns *= 2;
    }

    // ranks are derived from capacity (Config::CalculateSize), not an
    // ini key — needed so ch_pos stays correct when "ra" sits below "ch"
    // in the address mapping.
    const int devices_per_rank = bus_width / device_width;
    const int page_size = columns * device_width / 8;
    const int megs_per_bank = page_size * (rows / 1024) / 1024;
    const int megs_per_rank = megs_per_bank * banks * devices_per_rank;
    int ranks = 1;
    if (megs_per_rank > 0 && megs_per_rank <= channel_size) {
      ranks = channel_size / megs_per_rank;
    }

    if (channels_ <= 0) {
      throw std::runtime_error("ChannelMap: invalid channels in " + config_file);
    }

    const int request_size_bytes = bus_width / 8 * bl;
    shift_bits_ = logBase2(request_size_bytes);
    const int col_low_bits = logBase2(bl);
    const int actual_col_bits = logBase2(columns) - col_low_bits;

    std::unordered_map<std::string, int> field_widths;
    field_widths["ch"] = logBase2(channels_);
    field_widths["ra"] = logBase2(ranks);
    field_widths["bg"] = logBase2(bankgroups);
    field_widths["ba"] = logBase2(banks_per_group);
    field_widths["ro"] = logBase2(rows);
    field_widths["co"] = actual_col_bits;

    if (address_mapping.size() != 12) {
      throw std::runtime_error(
          "ChannelMap: address_mapping must be 12 chars (6x2) in " +
          config_file);
    }
    std::vector<std::string> fields;
    fields.reserve(6);
    for (size_t i = 0; i < address_mapping.size(); i += 2) {
      fields.push_back(address_mapping.substr(i, 2));
    }

    std::unordered_map<std::string, int> field_pos;
    int pos = 0;
    while (!fields.empty()) {
      const std::string token = fields.back();
      fields.pop_back();
      auto it = field_widths.find(token);
      if (it == field_widths.end()) {
        throw std::runtime_error("ChannelMap: unrecognized mapping field '" +
                                 token + "' in " + config_file);
      }
      field_pos[token] = pos;
      pos += it->second;
    }

    ch_pos_ = field_pos.at("ch");
    const int ch_bits = field_widths.at("ch");
    ch_mask_ = ch_bits >= 31 ? 0x7fffffff : (1 << ch_bits) - 1;
  }

  int channels() const { return channels_; }

  int channelOf(uint64_t addr) const {
    return static_cast<int>(((addr >> shift_bits_) >> ch_pos_) &
                            static_cast<uint64_t>(ch_mask_));
  }

 private:
  struct Ini {
    std::unordered_map<std::string,
                       std::unordered_map<std::string, std::string>>
        sections;

    std::string get(const std::string& sec, const std::string& key,
                    const std::string& def) const {
      auto s = sections.find(sec);
      if (s == sections.end()) {
        return def;
      }
      auto k = s->second.find(key);
      if (k == s->second.end()) {
        return def;
      }
      return k->second;
    }

    int getInt(const std::string& sec, const std::string& key, int def) const {
      const std::string v = get(sec, key, "");
      if (v.empty()) {
        return def;
      }
      return std::stoi(v);
    }

    bool getBool(const std::string& sec, const std::string& key,
                 bool def) const {
      const std::string v = lower(get(sec, key, ""));
      if (v.empty()) {
        return def;
      }
      return v == "1" || v == "true" || v == "yes" || v == "on";
    }
  };

  static std::string trim(std::string s) {
    const auto start = s.find_first_not_of(" \t\r\n");
    if (start == std::string::npos) {
      return "";
    }
    const auto end = s.find_last_not_of(" \t\r\n");
    return s.substr(start, end - start + 1);
  }

  static std::string lower(std::string s) {
    for (char& c : s) {
      if (c >= 'A' && c <= 'Z') {
        c = static_cast<char>(c - 'A' + 'a');
      }
    }
    return s;
  }

  static std::string upper(std::string s) {
    for (char& c : s) {
      if (c >= 'a' && c <= 'z') {
        c = static_cast<char>(c - 'a' + 'A');
      }
    }
    return s;
  }

  static int logBase2(int power_of_two) {
    // Same loop as dramsim3::LogBase2 (non-power-of-two floors).
    int i = 0;
    int n = power_of_two;
    while (n > 1) {
      n /= 2;
      ++i;
    }
    return i;
  }

  static Ini parseIni(const std::string& path) {
    std::ifstream in(path);
    if (!in) {
      throw std::runtime_error("ChannelMap: cannot open config " + path);
    }
    Ini ini;
    std::string section;
    std::string line;
    while (std::getline(in, line)) {
      // Strip inline comments (; or #), matching typical DRAMsim3 configs.
      const auto semi = line.find(';');
      const auto hash = line.find('#');
      size_t cut = std::string::npos;
      if (semi != std::string::npos) {
        cut = semi;
      }
      if (hash != std::string::npos &&
          (cut == std::string::npos || hash < cut)) {
        cut = hash;
      }
      if (cut != std::string::npos) {
        line = line.substr(0, cut);
      }
      line = trim(line);
      if (line.empty()) {
        continue;
      }
      if (line.front() == '[' && line.back() == ']') {
        section = line.substr(1, line.size() - 2);
        continue;
      }
      const auto eq = line.find('=');
      if (eq == std::string::npos || section.empty()) {
        continue;
      }
      const std::string key = trim(line.substr(0, eq));
      const std::string val = trim(line.substr(eq + 1));
      ini.sections[section][key] = val;
    }
    return ini;
  }

  int channels_ = 0;
  int shift_bits_ = 0;
  int ch_pos_ = 0;
  int ch_mask_ = 0;
};

#endif  // PDRAMSIM3_CHANNEL_MAP_HPP
