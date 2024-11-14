import json
import telnetlib
import os
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import logging
from typing import Optional, Dict, Any, List, Tuple
import re
import ipaddress  # Import for network calculations
from contextlib import closing


def normalize_interface_name(interface_name: str) -> str:
    """
    标准化接口名称，将不同格式（如'e1/0/0'、'E1/0/0'、'Eth1/0/0'、'Ethernet1/0/0'、'Ethernet 1/0/0'）转换为统一格式'Ethernet1/0/0'。

    :param interface_name: 原始接口名称。
    :return: 标准化后的接口名称。
    """
    interface_name = interface_name.strip()
    original_name = interface_name  # 保存原始名称用于日志
    normalized_name = interface_name  # 默认保持原样

    # 处理以 'e' 或 'E' 开头的接口名称，如 'e1/0/0' 或 'E1/0/0'
    if re.match(r'^[eE]\d+/\d+/\d+$', interface_name):
        iface_number = interface_name[1:]  # 移除 'e' 或 'E' 前缀
        normalized_name = f"Ethernet{iface_number}"
    # 处理以 'Eth' 开头的接口名称，如 'Eth1/0/0'
    elif re.match(r'^Eth\d+/\d+/\d+$', interface_name, re.IGNORECASE):
        iface_number = re.sub(r'^Eth', '', interface_name, flags=re.IGNORECASE)
        normalized_name = f"Ethernet{iface_number}"
    # 处理带有空格的 'Ethernet' 接口名称，如 'Ethernet 1/0/0'
    elif re.match(r'^Ethernet\s*\d+/\d+/\d+$', interface_name, re.IGNORECASE):
        # 移除 'Ethernet' 后的空格
        iface_number = re.sub(r'^Ethernet\s*', '', interface_name, flags=re.IGNORECASE)
        normalized_name = f"Ethernet{iface_number}"
    # 如果已经是 'Ethernet' 开头且无空格，如 'Ethernet1/0/0'
    elif re.match(r'^Ethernet\d+/\d+/\d+$', interface_name, re.IGNORECASE):
        # 确保 'Ethernet' 首字母大写，其余保持不变
        normalized_name = 'Ethernet' + interface_name[8:]
    else:
        # 对于其他接口类型，保持原样或根据需要进行其他处理
        normalized_name = interface_name.capitalize()

    logging.debug(f"Normalizing interface name: '{original_name}' -> '{normalized_name}'")
    return normalized_name


class RouterManager:
    def __init__(self, telnet_info: Dict[str, Any], max_workers: int = 20):
        self.telnet_info = telnet_info
        self.telnet_sysnames: Dict[str, str] = {}
        self.telnet_configurations: Dict[str, List[Dict[str, Any]]] = {}  # Structured interface info
        self.telnet_ospf_interfaces: Dict[str, List[str]] = {}  # Interfaces configured with OSPF
        self.telnet_router_ids: Dict[str, str] = {}  # Router IDs per device
        self.telnet_ospf_peers: Dict[str, List[str]] = {}  # OSPF neighbor Router IDs per device
        self.telnet_ospf_neighbors_info: Dict[str, List[Dict[str, Any]]] = {}  # Detailed OSPF neighbor info per device
        self.telnet_isis_interfaces: Dict[str, List[str]] = {}  # Interfaces configured with ISIS
        self.telnet_isis_peers_count: Dict[str, int] = {}  # ISIS peer counts per device
        self.telnet_bgp_info: Dict[str, Dict[str, Any]] = {}  # BGP info per device
        self.max_workers = max_workers
        self.telnet_lock = Lock()
        # Define command sequences for different device types
        self.commands_map = {
            "huaweine40": (
                [
                    'scr 0 t',
                    'display ip interface brief',
                    'display ospf interface',
                    'display ospf peer',
                    'display isis interface',
                    'display isis peer',
                    'display bgp all summary'
                ],
                b'q\n'
            )
        }

    def execute_telnet_commands(self, tn: telnetlib.Telnet, commands: List[str], quit_cmd: bytes) -> Dict[str, str]:
        """
        Execute a series of Telnet commands and return their outputs.

        :param tn: Telnet connection object.
        :param commands: List of commands to execute.
        :param quit_cmd: Command to exit the Telnet session.
        :return: Dictionary with commands as keys and their outputs as values.
        """
        try:
            tn.write(b'\n')
            time.sleep(1)
            initial_output = tn.read_very_eager().decode('ascii', errors='ignore')
            logging.debug(f"[{tn.host}:{tn.port}] Initial Telnet output:\n{initial_output}")

            # Detect prompt type
            prompt_type = None
            # 试图匹配以 '<' 开头和 '>' 结尾的提示符，或以 '[' 开头和 ']' 结尾的提示符
            prompt_match = re.search(r'([<\[]).*?([>\]])', initial_output.strip())
            if prompt_match:
                start_char, end_char = prompt_match.groups()
                if start_char == '<' and end_char == '>':
                    prompt_type = 'angle'
                elif start_char == '[' and end_char == ']':
                    prompt_type = 'bracket'
            logging.debug(f"[{tn.host}:{tn.port}] Detected prompt type: {prompt_type}")

            # Adjust command sequence based on prompt type
            modified_commands = []
            if prompt_type == 'bracket':
                # If prompt is in [], send 'q' first
                modified_commands.append('q')
                logging.debug(f"[{tn.host}:{tn.port}] Added 'q' to command sequence for 'bracket' prompt.")
            # Always append the main commands
            modified_commands.extend(commands)

            command_outputs = {}

            for cmd in modified_commands:
                tn.write(cmd.encode('ascii') + b'\n')
                logging.info(f"[{tn.host}:{tn.port}] Sending command: {cmd}")
                time.sleep(2)  # Increased wait time to ensure complete command output
                cmd_output = tn.read_very_eager().decode('ascii', errors='ignore')
                command_outputs[cmd] = cmd_output
                logging.debug(f"[{tn.host}:{tn.port}] Output for '{cmd}':\n{cmd_output}")

            # After executing commands, check if we need to send quit command
            prompt = self.get_prompt(tn)
            if prompt and not (prompt.startswith('<') and prompt.endswith('>')):
                tn.write(quit_cmd)
                logging.info(f"[{tn.host}:{tn.port}] Sending quit command.")
                time.sleep(1)
                cmd_output = tn.read_very_eager().decode('ascii', errors='ignore')
                command_outputs['quit'] = cmd_output

            return command_outputs
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] Telnet Error: {e}")
            return {}

    def get_prompt(self, tn: telnetlib.Telnet) -> Optional[str]:
        """
        Retrieve the current prompt from the Telnet session.

        :param tn: Telnet connection object.
        :return: The prompt string if found, else None.
        """
        try:
            time.sleep(1)
            output = tn.read_very_eager().decode('ascii', errors='ignore')
            lines = output.splitlines()
            prompt = lines[-1].strip() if lines else None
            logging.debug(f"[{tn.host}:{tn.port}] Detected prompt: {prompt}")
            return prompt
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] Error getting prompt: {e}")
            return None

    def get_sysname_via_telnet(self, tn: telnetlib.Telnet) -> Optional[str]:
        """
        Retrieve the system name (sysname) from the Telnet session.

        :param tn: Telnet connection object.
        :return: The sysname if found, else None.
        """
        try:
            tn.write(b'\n')
            time.sleep(1)
            output = tn.read_very_eager().decode('ascii', errors='ignore')
            for line in output.splitlines():
                if line.startswith('<') and line.endswith('>'):
                    sysname = line.strip('<> ').strip()
                    logging.info(f"[{tn.host}:{tn.port}] Detected sysname: {sysname}")
                    return sysname
            logging.warning(f"[{tn.host}:{tn.port}] No sysname detected.")
            return None
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] Telnet Error while getting sysname: {e}")
            return None

    def get_configuration_via_telnet(self, tn: telnetlib.Telnet, image_type: str) -> Optional[Dict[str, str]]:
        """
        Retrieve and parse configurations from the router via Telnet.

        :param tn: Telnet connection object.
        :param image_type: The image type of the device to determine command sequences.
        :return: The command outputs if successful, else None.
        """
        # Find matching device type based on partial image_type
        matched_key = next((key for key in self.commands_map if key in image_type), None)
        if not matched_key:
            logging.warning(f"[{tn.host}:{tn.port}] Unsupported image_type '{image_type}'. Skipping.")
            return None

        commands, quit_cmd = self.commands_map[matched_key]
        command_outputs = self.execute_telnet_commands(tn, commands, quit_cmd)
        if command_outputs:
            sysname = self.get_sysname_via_telnet(tn)
            if sysname:
                key = f"{tn.host}:{tn.port}"
                with self.telnet_lock:
                    self.telnet_sysnames[key] = sysname
                    # Parse 'display ip interface brief'
                    if 'display ip interface brief' in command_outputs:
                        parsed_interfaces = self.parse_display_ip_interface_brief(command_outputs['display ip interface brief'])
                        self.telnet_configurations[key] = parsed_interfaces
                    else:
                        logging.warning(f"[{tn.host}:{tn.port}] Missing 'display ip interface brief' output.")

                    # Parse 'display ospf interface'
                    if 'display ospf interface' in command_outputs:
                        ospf_interfaces = self.parse_display_ospf_interface(command_outputs['display ospf interface'])
                        self.telnet_ospf_interfaces[key] = ospf_interfaces
                    else:
                        logging.warning(f"[{tn.host}:{tn.port}] Missing 'display ospf interface' output.")

                    # Parse 'display ospf peer'
                    if 'display ospf peer' in command_outputs:
                        ospf_peer_output = command_outputs['display ospf peer']
                        router_id, neighbors_info = self.parse_display_ospf_peer(ospf_peer_output)
                        if router_id:
                            self.telnet_router_ids[key] = router_id
                        if neighbors_info is not None:
                            self.telnet_ospf_peers[key] = [neighbor['router_id'] for neighbor in neighbors_info]
                            self.telnet_ospf_neighbors_info[key] = neighbors_info
                    else:
                        logging.warning(f"[{tn.host}:{tn.port}] Missing 'display ospf peer' output.")

                    # Parse 'display isis interface'
                    if 'display isis interface' in command_outputs:
                        isis_output = command_outputs['display isis interface']
                        isis_interfaces = self.parse_display_isis_interface(isis_output)
                        self.telnet_isis_interfaces[key] = isis_interfaces
                    else:
                        logging.warning(f"[{tn.host}:{tn.port}] Missing 'display isis interface' output.")

                    # Parse 'display isis peer'
                    if 'display isis peer' in command_outputs:
                        isis_peer_output = command_outputs['display isis peer']
                        isis_peers = self.parse_display_isis_peer(isis_peer_output)
                        if isis_peers is not None:
                            self.telnet_isis_peers_count[key] = isis_peers
                    else:
                        logging.warning(f"[{tn.host}:{tn.port}] Missing 'display isis peer' output.")

                    # Parse 'display bgp all summary'
                    if 'display bgp all summary' in command_outputs:
                        bgp_output = command_outputs['display bgp all summary']
                        bgp_info = self.parse_display_bgp_all_summary(bgp_output)
                        if bgp_info:
                            self.telnet_bgp_info[key] = bgp_info
                    else:
                        logging.warning(f"[{tn.host}:{tn.port}] Missing 'display bgp all summary' output.")
        else:
            logging.warning(f"[{tn.host}:{tn.port}] No output received from Telnet commands.")
        return command_outputs

    def parse_display_ospf_peer(self, output: str) -> Tuple[Optional[str], Optional[List[Dict[str, Any]]]]:
        """
        Parse the output of 'display ospf peer' to extract the current node's Router ID and detailed neighbor information.

        :param output: Output of the command.
        :return: Tuple containing Router ID and list of neighbor information dictionaries. Returns (None, None) if no relevant output.
        """
        router_id = None
        neighbors_info = []
        lines = output.splitlines()
        logging.debug("Parsing 'display ospf peer' output.")

        # Define regex patterns
        ospf_process_regex = re.compile(r'OSPF Process \d+ with Router ID (\d+\.\d+\.\d+\.\d+)')
        # 修改正则表达式以捕获括号内的接口名称
        neighbor_interface_regex = re.compile(r'Area \d+\.\d+\.\d+\.\d+ interface \S+ \((\S+)\)\'s neighbors')
        router_id_regex = re.compile(r'Router ID:\s+(\d+\.\d+\.\d+\.\d+)')
        address_regex = re.compile(r'Address:\s+(\d+\.\d+\.\d+\.\d+)')
        state_regex = re.compile(r'State:\s+(\w+)')

        current_interface = None
        current_neighbor_info = {}

        for i, line in enumerate(lines):
            # Detect OSPF Process line
            ospf_process_match = ospf_process_regex.search(line)
            if ospf_process_match:
                router_id = ospf_process_match.group(1)
                logging.debug(f"Detected OSPF Router ID: {router_id}")
                continue

            # Detect interface line
            neighbor_interface_match = neighbor_interface_regex.search(line)
            if neighbor_interface_match:
                # If there's an existing neighbor being processed, check if it's complete
                if current_interface and current_neighbor_info:
                    if 'router_id' in current_neighbor_info and 'address' in current_neighbor_info and 'state' in current_neighbor_info:
                        is_problematic = current_neighbor_info['state'].lower() != 'full'
                        neighbor_info = {
                            "interface": current_interface,
                            "router_id": current_neighbor_info['router_id'],
                            "address": current_neighbor_info['address'],
                            "state": current_neighbor_info['state'],
                            "is_problematic": is_problematic
                        }
                        neighbors_info.append(neighbor_info)
                        logging.info(f"Extracted Neighbor Info: {neighbor_info}")
                    else:
                        logging.warning(f"Incomplete neighbor information detected on interface {current_interface}.")

                iface = neighbor_interface_match.group(1)
                current_interface = normalize_interface_name(iface)
                current_neighbor_info = {}  # Reset for new neighbor
                logging.debug(f"Detected OSPF Neighbor Interface: {current_interface}")
                continue

            # If within a neighbor block, extract information
            if current_interface:
                # Attempt to extract Router ID
                router_id_match = router_id_regex.search(line)
                if router_id_match:
                    current_neighbor_info['router_id'] = router_id_match.group(1)
                    logging.debug(f"Detected Neighbor Router ID: {current_neighbor_info['router_id']}")

                # Attempt to extract Address
                address_match = address_regex.search(line)
                if address_match:
                    current_neighbor_info['address'] = address_match.group(1)
                    logging.debug(f"Detected Neighbor Address: {current_neighbor_info['address']}")

                # Attempt to extract State
                state_match = state_regex.search(line)
                if state_match:
                    current_neighbor_info['state'] = state_match.group(1)
                    logging.debug(f"Detected Neighbor State: {current_neighbor_info['state']}")

                # Check if we've collected all necessary info
                if 'router_id' in current_neighbor_info and 'address' in current_neighbor_info and 'state' in current_neighbor_info:
                    is_problematic = current_neighbor_info['state'].lower() != 'full'
                    neighbor_info = {
                        "interface": current_interface,
                        "router_id": current_neighbor_info['router_id'],
                        "address": current_neighbor_info['address'],
                        "state": current_neighbor_info['state'],
                        "is_problematic": is_problematic
                    }
                    neighbors_info.append(neighbor_info)
                    logging.info(f"Extracted Neighbor Info: {neighbor_info}")
                    # Reset for next neighbor
                    current_interface = None
                    current_neighbor_info = {}
                    continue

        # After processing all lines, check if there's an incomplete neighbor
        if current_interface and current_neighbor_info:
            if 'router_id' in current_neighbor_info and 'address' in current_neighbor_info and 'state' in current_neighbor_info:
                is_problematic = current_neighbor_info['state'].lower() != 'full'
                neighbor_info = {
                    "interface": current_interface,
                    "router_id": current_neighbor_info['router_id'],
                    "address": current_neighbor_info['address'],
                    "state": current_neighbor_info['state'],
                    "is_problematic": is_problematic
                }
                neighbors_info.append(neighbor_info)
                logging.info(f"Extracted Neighbor Info: {neighbor_info}")
            else:
                logging.warning(f"Incomplete neighbor information detected on interface {current_interface}.")

        if router_id:
            logging.info(f"Extracted OSPF Router ID: {router_id} with {len(neighbors_info)} neighbors.")
            return router_id, neighbors_info
        else:
            logging.info("No OSPF Process information found in 'display ospf peer' output.")
            return None, None

    def parse_display_isis_peer(self, output: str) -> Optional[int]:
        """
        Parse the output of 'display isis peer' to extract the total number of ISIS peers.

        :param output: Output of the command.
        :return: Number of peers as integer. Returns None if unable to parse.
        """
        total_peers = None
        lines = output.splitlines()
        logging.debug("Parsing 'display isis peer' output.")
        total_peers_regex = re.compile(r'^Total Peer\(s\):\s+(\d+)', re.IGNORECASE)

        for line in lines:
            match = total_peers_regex.search(line)
            if match:
                total_peers = int(match.group(1))
                logging.debug(f"Detected total ISIS peers: {total_peers}")
                break

        if total_peers is not None:
            logging.info(f"Extracted total ISIS peers: {total_peers}")
            return total_peers
        else:
            logging.info("No 'Total Peer(s):' line found in 'display isis peer' output.")
            return None

    def parse_display_isis_interface(self, output: str) -> List[str]:
        """
        Parse the output of 'display isis interface' to extract configured ISIS interfaces.

        :param output: Command output.
        :return: List of ISIS-configured interface names in standardized format.
        """
        interfaces = []
        lines = output.splitlines()
        logging.debug("Parsing 'display isis interface' output.")

        # Regular expression to match interface lines
        interface_regex = re.compile(r'^\s*(?P<interface>\S+)\s+[\d\.]+')

        for line in lines:
            # Skip empty lines and separator lines
            if not line.strip() or re.match(r'^[-=]+$', line):
                continue

            # Match interface lines
            match = interface_regex.match(line)
            if match:
                iface = match.group('interface')
                # Use normalize_interface_name to handle various interface name formats
                iface_formatted = normalize_interface_name(iface)
                interfaces.append(iface_formatted)
                logging.debug(f"Detected ISIS-configured interface: {iface_formatted}")
            else:
                logging.debug(f"Unmatched ISIS interface line: {line}")
        logging.debug(f"Parsed ISIS interfaces: {interfaces}")
        return interfaces

    def parse_display_bgp_all_summary(self, output: str) -> Optional[Dict[str, Any]]:
        """
        解析 'display bgp all summary' 命令的输出，提取 BGP 信息。

        :param output: 命令输出。
        :return: 包含 BGP 本地 Router ID、本地 AS 号、总对等体数、已建立对等体数、未建立对等体列表、
                Established peers with different AS 的字典。如果解析失败，则返回 None。
        """
        bgp_info = {
            "bgp_local_router_id": None,
            "bgp_local_as_number": None,
            "bgp_total_peers": 0,
            "bgp_established_peers": 0,
            "bgp_non_established_peers": [],
            "bgp_established_different_as_peers": []  # Established peers with different AS
        }

        lines = output.splitlines()
        logging.debug("解析 'display bgp all summary' 输出。")

        # Define regex patterns
        key_value_regex = re.compile(r'(\w+(?:\s+\w+)*)\s*:\s*([\d\.]+)', re.IGNORECASE)
        peer_entry_regex = re.compile(
            r'^(?P<peer_ip>\S+)\s+'
            r'(?P<peer_as>\d+)\s+'
            r'(?P<msg_rcvd>\d+)\s+'
            r'(?P<msg_sent>\d+)\s+'
            r'(?P<out_q>\d+)\s+'
            r'(?P<up_down>\S+)\s+'
            r'(?P<state>\S+)', re.IGNORECASE
        )

        local_as_number = None
        peer_as_numbers = set()
        inter_as_established_peers: List[str] = []  # List to store inter-AS established peer IPs
        in_peer_table = False  # Flag to indicate if parsing peer table

        for line in lines:
            # Extract key-value pairs
            key_value_matches = key_value_regex.findall(line)
            for key, value in key_value_matches:
                key = key.strip().lower()
                if key == 'bgp local router id':
                    bgp_info["bgp_local_router_id"] = value
                    logging.debug(f"Detected BGP Local Router ID: {bgp_info['bgp_local_router_id']}")
                elif key == 'local as number':
                    try:
                        local_as_number = int(value)
                        bgp_info["bgp_local_as_number"] = local_as_number
                        logging.debug(f"Detected Local AS Number: {bgp_info['bgp_local_as_number']}")
                    except ValueError:
                        logging.error(f"Unable to parse Local AS Number: {value}")
                elif key == 'total number of peers':
                    try:
                        bgp_info["bgp_total_peers"] = int(value)
                        logging.debug(f"Detected Total BGP Peers: {bgp_info['bgp_total_peers']}")
                    except ValueError:
                        logging.error(f"Unable to parse Total BGP Peers: {value}")
                elif key == 'peers in established state':
                    try:
                        bgp_info["bgp_established_peers"] = int(value)
                        logging.debug(f"Detected Established BGP Peers: {bgp_info['bgp_established_peers']}")
                    except ValueError:
                        logging.error(f"Unable to parse Established BGP Peers: {value}")

            # Detect start of peer table
            if line.strip().startswith("Peer"):
                in_peer_table = True
                continue

            if in_peer_table:
                # Detect end of peer table
                if re.match(r'^[-=]+$', line.strip()):
                    in_peer_table = False
                    continue

                # Parse peer entries
                match = peer_entry_regex.match(line.strip())
                if match:
                    peer_ip = match.group('peer_ip')
                    peer_as = int(match.group('peer_as'))
                    state = match.group('state').lower()

                    # Collect all peer AS numbers
                    peer_as_numbers.add(peer_as)

                    if state == 'established':
                        # Only record if AS numbers differ
                        if local_as_number is not None and peer_as != local_as_number:
                            bgp_info["bgp_established_different_as_peers"].append(peer_ip)
                            logging.debug(f"Detected established BGP peer with different AS: IP={peer_ip}, AS={peer_as}")
                    else:
                        bgp_info["bgp_non_established_peers"].append({
                            "peer_ip": peer_ip,
                            "peer_as": peer_as,
                            "state": state.capitalize()
                        })
                        logging.debug(f"Detected non-established BGP peer: IP={peer_ip}, AS={peer_as}, State={state.capitalize()}")
                else:
                    logging.debug(f"Unmatched BGP peer line: {line}")

        # Determine if this router is a boundary router
        # Removed related boundary router logic

        # Verify essential BGP information
        if bgp_info["bgp_local_router_id"] and bgp_info["bgp_local_as_number"]:
            logging.info(f"Extracted BGP Information: {bgp_info}")
            return bgp_info
        else:
            logging.warning("Failed to extract some BGP information from 'display bgp all summary' output.")
            return None

    def parse_display_ip_interface_brief(self, output: str) -> List[Dict[str, Any]]:
        """
        Parse the output of 'display ip interface brief', capture IP and subnet mask, and return structured data.

        :param output: Command output.
        :return: List of interface information dictionaries.
        """
        lines = output.splitlines()
        interfaces = []
        header_found = False

        # Regular expression to match interface lines with subnet mask
        interface_regex = re.compile(
            r'^\s*(?P<interface>\S+)\s+'
            r'(?P<ip_address>(?:\d{1,3}\.){3}\d{1,3})/(?P<mask>\d{1,2})\s+'
            r'(?P<physical>up|down)\s+'
            r'(?P<protocol>up|down)\s+'
            r'(?P<vpn>\S+)'
        )

        # Define interfaces to exclude
        excluded_interfaces = {"Ethernet1/0/0.192"}

        for line in lines:
            # Look for table header
            if not header_found:
                if re.match(r'^Interface\s+IP Address/Mask\s+Physical\s+Protocol\s+VPN', line):
                    header_found = True
                    logging.debug("Found 'display ip interface brief' table header.")
                continue
            else:
                # Skip empty lines or separator lines
                if not line.strip() or re.match(r'^[-=]+$', line):
                    continue

                match = interface_regex.match(line)
                if match:
                    iface = match.group('interface')
                    iface_formatted = normalize_interface_name(iface)
                    if iface_formatted in excluded_interfaces:
                        logging.debug(f"Skipping excluded interface: {iface_formatted}")
                        continue  # Skip excluded interfaces

                    ip_address = match.group('ip_address')
                    mask = int(match.group('mask'))

                    if ip_address.lower() != 'unassigned':
                        try:
                            network = ipaddress.IPv4Network(f"{ip_address}/{mask}", strict=False)
                            network_str = str(network)
                        except ValueError as ve:
                            logging.error(f"Invalid IP address or mask: {ip_address}/{mask} - {ve}")
                            network_str = "Invalid"
                        interface_info = {
                            'Interface': iface_formatted,
                            'IP Address': ip_address,
                            'Mask': mask,
                            'Network': network_str,
                            'Physical': match.group('physical'),
                            'Protocol': match.group('protocol'),
                            'VPN': match.group('vpn')
                        }
                        interfaces.append(interface_info)
                        logging.debug(f"Parsed interface: {interface_info}")
                else:
                    # Handle 'unassigned' IPs by skipping
                    continue

        logging.debug(f"Parsed Telnet interfaces: {interfaces}")
        return interfaces

    def parse_display_ospf_interface(self, output: str) -> List[str]:
        """
        Parse the output of 'display ospf interface' to extract OSPF-configured interfaces.

        :param output: Command output.
        :return: List of OSPF-configured interface names in standardized format.
        """
        interfaces = []
        lines = output.splitlines()
        logging.debug("Parsing 'display ospf interface' output.")

        # Regular expression to match interface lines
        interface_regex = re.compile(r'^\s*(?P<interface>\S+)\s+[\d\.]+')

        for line in lines:
            if "Interfaces" in line:
                parsing = True
                logging.debug("Starting to parse OSPF interface information.")
                continue
            # Skip empty lines and separator lines
            if not line.strip() or re.match(r'^[-=]+$', line):
                continue

            # Match interface lines
            match = interface_regex.match(line)
            if match:
                iface = match.group('interface')
                # Use normalize_interface_name to handle various interface name formats
                iface_formatted = normalize_interface_name(iface)
                interfaces.append(iface_formatted)
                logging.debug(f"Detected OSPF-configured interface: {iface_formatted}")
            else:
                logging.debug(f"Unmatched OSPF interface line: {line}")
        logging.debug(f"Parsed OSPF interfaces: {interfaces}")
        return interfaces

    def get_routing_table(self, tn: telnetlib.Telnet) -> List[str]:
        """
        执行 'display ip routing-table' 命令，并解析出 Cost 大于5且小于20的 Destination。

        :param tn: Telnet 连接对象。
        :return: 满足条件的 Destination 地址列表。
        """
        command = 'display ip routing-table'
        tn.write(command.encode('ascii') + b'\n')
        logging.info(f"[{tn.host}:{tn.port}] 发送命令: {command}")
        time.sleep(2)  # 等待命令执行

        output = tn.read_very_eager().decode('ascii', errors='ignore')
        logging.debug(f"[{tn.host}:{tn.port}] '{command}' 输出:\n{output}")

        destinations = []
        lines = output.splitlines()
        # 找到表格的起始位置
        table_start = False
        for line in lines:
            if re.match(r'^Destination/Mask', line):
                table_start = True
                continue
            if table_start:
                if not line.strip() or re.match(r'^[-=]+$', line):
                    continue
                # 解析表格行
                match = re.match(r'^\s*(?P<destination>\d+\.\d+\.\d+\.\d+/\d+)\s+\S+\s+\d+\s+(?P<cost>\d+)\s+\S+\s+\S+\s+\S+', line)
                if match:
                    dest = match.group('destination')
                    cost = int(match.group('cost'))
                    if 5 < cost < 20:
                        destinations.append(dest.split('/')[0])  # 取地址部分
                        logging.debug(f"[{tn.host}:{tn.port}] 找到满足条件的 Destination: {dest.split('/')[0]}，Cost: {cost}")
        logging.info(f"[{tn.host}:{tn.port}] 满足 Cost >5 且 <20 的 Destinations: {destinations}")
        return destinations

    def tracert_and_monitor(self, tn: telnetlib.Telnet, destination: str) -> str:
        """
        执行 tracert 命令并监控输出，根据结果判断链路状态。

        :param tn: Telnet 连接对象。
        :param destination: 需要追踪的目的地址。
        :return: 链路状态描述。
        """
        tracert_command = f"tracert {destination}"
        tn.write(tracert_command.encode('ascii') + b'\n')
        logging.info(f"[{tn.host}:{tn.port}] 发送命令: {tracert_command}")
        time_started = time.time()
        tracert_output = ""
        status = "未检测到"

        while True:
            time.sleep(5)  # 每隔五秒检查一次
            output = tn.read_very_eager().decode('ascii', errors='ignore')
            tracert_output += output
            logging.debug(f"[{tn.host}:{tn.port}] 'tracert' 输出:\n{output}")

            if '*' in output:
                # 发送 Ctrl+C 终止 tracert
                tn.write(b'\x03')
                logging.warning(f"[{tn.host}:{tn.port}] 'tracert' 输出中存在 '*'，链路存在问题。")
                status = "链路存在问题"
                break
            elif re.search(r'[<\[]', output):
                logging.info(f"[{tn.host}:{tn.port}] 'tracert' 输出中存在 '<' 或 '[', 连通性良好。")
                status = "连通性良好"
                break
            # 可根据需要设置超时时间
            if time.time() - time_started > 60:  # 超过60秒则停止
                tn.write(b'\x03')
                logging.warning(f"[{tn.host}:{tn.port}] 'tracert' 命令执行超时。")
                status = "tracert 命令执行超时"
                break

        return status

    def connect_and_get_sysnames_and_configs(self):
        """
        Establish Telnet connections to all nodes and retrieve their configurations concurrently.
        """
        nodes = self.telnet_info.get("node", [])
        if not nodes:
            logging.warning("No nodes found in telnet_info.")
            return

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_node = {}
            for node in nodes:
                image_type = node.get("image_type", "").lower()
                if "huaweine40" in image_type:
                    host, port = node.get("hostip"), node.get("port")
                    if not host or not port:
                        logging.warning(f"Host IP or port missing for node with image_type '{image_type}'. Skipping.")
                        continue
                    try:
                        tn = telnetlib.Telnet(host, port, timeout=10)
                        tn.host, tn.port = host, port  # Assign host and port attributes
                        future = executor.submit(self.get_configuration_via_telnet, tn, image_type)
                        future_to_node[future] = node
                    except Exception as e:
                        logging.error(f"Failed to connect to {host}:{port} via Telnet: {e}")

            for future in as_completed(future_to_node):
                node = future_to_node[future]
                host, port = node.get("hostip"), node.get("port")
                config = future.result()
                msg = "successful" if config else "failed"
                logging.info(f"[{host}:{port}] Configuration retrieval {msg}.")

    def collect_results(self) -> Dict[str, Any]:
        """
        Collect all results, perform interface matching, and output interface configuration statuses,
        including OSPF, ISIS, and BGP statuses. Additionally, identify inter-AS links based on
        network segments and provide recommendations.
        """
        # 存储接口状态信息
        interface_status = {}
        ospf_status = {}
        isis_status = {}
        bgp_info_dict = {}
        ospf_neighbors_details = {}
        tracert_status = {}  # 存储 tracert 的状态信息

        # 创建集合来存储存在配置问题的节点
        ospf_issues = set()
        isis_issues = set()
        bgp_issues = set()
        ospf_neighbor_issues = set()
        tracert_issues = set()  # 新增 tracert 问题集合

        # Initialize the aggregate protocol_issues_set
        all_protocol_issues_set = set()

        for host_port, sysname in self.telnet_sysnames.items():
            tn = telnetlib.Telnet(host_port.split(":")[0], int(host_port.split(":")[1]), timeout=10)
            tn.host, tn.port = host_port.split(":")[0], int(host_port.split(":")[1])

            telnet_interfaces = self.telnet_configurations.get(host_port, [])
            ospf_interfaces = self.telnet_ospf_interfaces.get(host_port, [])
            isis_interfaces = self.telnet_isis_interfaces.get(host_port, [])
            isis_peers = self.telnet_isis_peers_count.get(host_port)
            router_id = self.telnet_router_ids.get(host_port)
            ospf_neighbors = self.telnet_ospf_peers.get(host_port)
            ospf_neighbors_info = self.telnet_ospf_neighbors_info.get(host_port, [])
            bgp_info = self.telnet_bgp_info.get(host_port)

            # Extract interface names and convert to lowercase for comparison
            telnet_interface_names = [iface['Interface'].lower() for iface in telnet_interfaces if isinstance(iface, dict)]
            ospf_interfaces_lower = [iface.lower() for iface in ospf_interfaces]
            isis_interfaces_lower = [iface.lower() for iface in isis_interfaces]

            logging.debug(f"[{host_port}] Telnet 获取的接口: {telnet_interface_names}")
            logging.debug(f"[{host_port}] OSPF 配置的接口: {ospf_interfaces_lower}")
            logging.debug(f"[{host_port}] ISIS 配置的接口: {isis_interfaces_lower}")
            if bgp_info:
                logging.debug(f"[{host_port}] BGP 信息: {bgp_info}")

            interface_status[host_port] = []
            ospf_status[host_port] = ""
            isis_status[host_port] = ""
            bgp_info_dict[host_port] = {}
            ospf_neighbors_details[host_port] = []
            tracert_status[host_port] = "未执行"

            # 处理接口状态
            for iface_info in telnet_interfaces:
                iface = iface_info.get('Interface')
                if not iface:
                    continue
                # 保持原始接口名称格式
                iface_formatted = iface
                ip_address = iface_info.get('IP Address')
                mask = iface_info.get('Mask')

                if ip_address and ip_address.lower() != 'unassigned':
                    physical_status = iface_info.get('Physical', '').lower()
                    protocol_status = iface_info.get('Protocol', '').lower()

                    # 处理 Physical 状态，忽略附加信息，只关注 'up' 或 'down'
                    physical_up = 'up' in physical_status
                    # 处理 Protocol 状态，忽略附加信息，只关注 'up' 或 'down'
                    protocol_up = 'up' in protocol_status

                    status = f"{iface_formatted}接口状态: "
                    if physical_up and protocol_up:
                        status += "已配置IP地址，接口正常使用中"
                    else:
                        status += "已配置IP地址，接口状态异常，请检查接口配置"
                        # 如果接口状态异常，记录协议配置问题（可选）
                        # 具体逻辑可根据需求调整

                    # 检查是否配置了 OSPF
                    if iface.lower() in ospf_interfaces_lower:
                        status += " | 配置了 OSPF"
                    else:
                        status += " | 未配置 OSPF"
                        # 如果未配置 OSPF，不应将其视为协议配置问题
                        # 仅当接口需要配置 OSPF 且未配置时，才可能是问题（需要进一步定义需求）
                        # 这里假设未配置 OSPF 不是问题
                        pass  # 不添加到 ospf_issues

                    # 检查是否配置了 ISIS
                    if iface.lower() in isis_interfaces_lower:
                        status += " | 配置了 ISIS"
                    else:
                        status += " | 未配置 ISIS"
                        # 同上，不将未配置 ISIS 的接口视为问题
                        pass  # 不添加到 isis_issues

                    interface_status[host_port].append(status)
                    logging.info(f"[{host_port}] {status}")
                else:
                    # 未配置IP地址的接口不被包括在输出中
                    continue

            # 判断 OSPF 状态并合并详细邻居信息
            if router_id:
                if ospf_neighbors:
                    neighbors_str = ', '.join(ospf_neighbors)
                    ospf_status_message = f"OSPF 配置正常，Router ID: {router_id}，邻居 Router IDs: {neighbors_str}"
                else:
                    if ospf_interfaces:
                        ospf_status_message = "OSPF 配置问题：未检测到邻居 Router ID"
                        ospf_issues.add(host_port)  # 添加到 OSPF 问题节点集合
                    else:
                        ospf_status_message = "OSPF 未配置"
                ospf_status[host_port] = ospf_status_message
            else:
                if ospf_interfaces:
                    ospf_status_message = "OSPF 配置问题：未检测到 Router ID"
                    ospf_issues.add(host_port)  # 添加到 OSPF 问题节点集合
                else:
                    ospf_status_message = "OSPF 未配置"
                ospf_status[host_port] = ospf_status_message

            # 如果存在 OSPF 详细邻居信息，检查是否有问题
            if ospf_neighbors_info:
                ospf_neighbors_details[host_port] = ospf_neighbors_info
                # Check for problematic neighbor states
                for neighbor in ospf_neighbors_info:
                    if neighbor.get("is_problematic"):
                        ospf_neighbor_issues.add(host_port)
                        logging.warning(f"[{host_port}] OSPF Neighbor on {neighbor.get('interface')} with Router ID {neighbor.get('router_id')} is in state {neighbor.get('state')}.")

            # 判断 ISIS 状态
            if isis_peers is not None:
                if isis_peers > 0 and isis_interfaces:
                    isis_status[host_port] = f"ISIS 配置正常，邻居数量: {isis_peers}"
                elif isis_interfaces and isis_peers == 0:
                    isis_status[host_port] = "ISIS 配置问题：接口配置了 ISIS 但未检测到邻居 Router ID"
                    isis_issues.add(host_port)  # 添加到 ISIS 问题节点集合
                elif not isis_interfaces and isis_peers > 0:
                    isis_status[host_port] = "ISIS 配置问题：存在 ISIS 邻居但未配置 ISIS 接口"
                    isis_issues.add(host_port)  # 添加到 ISIS 问题节点集合
                else:
                    isis_status[host_port] = "ISIS 配置问题：未知情况"
                    isis_issues.add(host_port)  # 添加到 ISIS 问题节点集合
            else:
                if isis_interfaces:
                    isis_status[host_port] = "ISIS 配置错误：接口配置了 ISIS 但未检测到邻居 Router ID"
                    isis_issues.add(host_port)  # 添加到 ISIS 问题节点集合
                else:
                    isis_status[host_port] = "ISIS 未配置"

            # 提取 BGP 信息
            if bgp_info:
                bgp_local_router_id = bgp_info.get("bgp_local_router_id", "未知")
                bgp_local_as_number = bgp_info.get("bgp_local_as_number", "未知")
                bgp_total_peers = bgp_info.get("bgp_total_peers", 0)
                bgp_established_peers = bgp_info.get("bgp_established_peers", 0)
                bgp_non_established_peers = bgp_info.get("bgp_non_established_peers", [])
                bgp_established_different_as_peers = bgp_info.get("bgp_established_different_as_peers", [])

                bgp_info_dict[host_port] = {
                    "bgp_local_router_id": bgp_local_router_id,
                    "bgp_local_as_number": bgp_local_as_number,
                    "bgp_total_peers": bgp_total_peers,
                    "bgp_established_peers": bgp_established_peers,
                    "bgp_non_established_peers": bgp_non_established_peers,
                    "bgp_established_different_as_peers": bgp_established_different_as_peers  # New field
                }

                # Record BGP status logs
                if bgp_info.get("bgp_total_peers", 0) > 0:
                    logging.info(f"[{host_port}] BGP 本地 Router ID: {bgp_local_router_id}")
                    logging.info(f"[{host_port}] BGP 本地 AS Number: {bgp_local_as_number}")
                    logging.info(f"[{host_port}] BGP 总邻居数量: {bgp_total_peers}")
                    logging.info(f"[{host_port}] BGP 建立状态的邻居数量: {bgp_established_peers}")
                    if bgp_non_established_peers:
                        logging.info(f"[{host_port}] BGP 未建立状态的邻居: {bgp_non_established_peers}")
                        # 将存在未建立状态的 BGP peers 的节点添加到 bgp_issues
                        bgp_issues.add(host_port)
                    else:
                        logging.info(f"[{host_port}] 所有 BGP peers 均处于 Established 状态。")
                else:
                    logging.info(f"[{host_port}] BGP 未配置或无 peers。")
            else:
                bgp_info_dict[host_port] = {
                    "bgp_local_router_id": "未知",
                    "bgp_local_as_number": "未知",
                    "bgp_total_peers": 0,
                    "bgp_established_peers": 0,
                    "bgp_non_established_peers": [],
                    "bgp_established_different_as_peers": []
                }
                logging.info(f"[{host_port}] BGP 未配置")

            # 执行并监控 tracert
            destinations = self.get_routing_table(tn)
            if destinations:
                selected_destination = destinations[0]  # 选择第一个满足条件的 Destination
                tracert_result = self.tracert_and_monitor(tn, selected_destination)
                tracert_status[host_port] = tracert_result
                if tracert_result == "链路存在问题":
                    tracert_issues.add(host_port)
            else:
                tracert_status[host_port] = "未找到满足条件的 Destination，未执行 tracert。"

            # # 关闭 Telnet 连接
            # tn.write(b'q\n')
            # tn.close()

        # 准备映射
        mapping = {
            "telnet_devices": {
                host_port: {
                    "sysname": self.telnet_sysnames.get(host_port, "未知节点"),
                    "router_id": self.telnet_router_ids.get(host_port, "未知 Router ID"),
                    "ospf_neighbors": self.telnet_ospf_peers.get(host_port, []),
                    "ospf_neighbors_info": self.telnet_ospf_neighbors_info.get(host_port, []),  # New field
                    "interfaces": [
                        iface for iface in self.telnet_configurations.get(host_port, [])
                        if iface['IP Address'].lower() != 'unassigned'
                    ],
                    "ospf_interfaces": self.telnet_ospf_interfaces.get(host_port, []),
                    "isis_interfaces": self.telnet_isis_interfaces.get(host_port, []),
                    "interface_status": interface_status.get(host_port, []),
                    "ospf_status": ospf_status.get(host_port, "未配置 OSPF"),
                    "isis_status": isis_status.get(host_port, "未配置 ISIS"),
                    "bgp_info": bgp_info_dict.get(host_port, {}),
                    "ospf_neighbors_details": ospf_neighbors_details.get(host_port, []),  # New field
                    "tracert_status": tracert_status.get(host_port, "未执行")  # 新增 tracert 状态
                }
                for host_port in self.telnet_sysnames.keys()
            },
            "total_nodes": len(self.telnet_sysnames),  # Total number of nodes
            "evaluation_per_as": {},  # To be filled later
            "protocol_issues": {  # 新增协议配置问题的节点列表
                "OSPF_issues": list(ospf_issues),
                "ISIS_issues": list(isis_issues),
                "BGP_issues": list(bgp_issues),
                "OSPF_Neighbor_Issues": list(ospf_neighbor_issues),  # New field
                "Tracert_Issues": list(tracert_issues)  # 新增 tracert 问题节点列表
            }
        }

        # Collect protocol configuration issues into the aggregate set
        for host_port in ospf_issues:
            all_protocol_issues_set.add(host_port)
        for host_port in isis_issues:
            all_protocol_issues_set.add(host_port)
        for host_port in bgp_issues:
            all_protocol_issues_set.add(host_port)
        for host_port in ospf_neighbor_issues:
            all_protocol_issues_set.add(host_port)
        for host_port in tracert_issues:
            all_protocol_issues_set.add(host_port)

        # Optionally, include the aggregate set in the mapping
        mapping["protocol_issues"]["All_Protocol_Issues"] = list(all_protocol_issues_set)

        # Generate evaluation information per AS
        # Removed evaluation_per_as logic

        mapping["evaluation_per_as"] = {}  # 保持为空或根据需要调整

        # 添加协议配置问题的评估信息
        protocol_issues_evaluation = []
        if ospf_issues or isis_issues or bgp_issues or ospf_neighbor_issues or tracert_issues:
            protocol_issues_evaluation.append("以下节点的协议配置存在问题，请检查相关配置：")
            if ospf_issues:
                ospf_nodes = ', '.join([self.telnet_sysnames.get(node, node) for node in ospf_issues])
                protocol_issues_evaluation.append(f"    OSPF 配置问题的节点: {ospf_nodes}")
                logging.warning(f"OSPF 配置问题的节点: {ospf_nodes}")
            if isis_issues:
                isis_nodes = ', '.join([self.telnet_sysnames.get(node, node) for node in isis_issues])
                protocol_issues_evaluation.append(f"    ISIS 配置问题的节点: {isis_nodes}")
                logging.warning(f"ISIS 配置问题的节点: {isis_nodes}")
            if bgp_issues:
                bgp_nodes = ', '.join([self.telnet_sysnames.get(node, node) for node in bgp_issues])
                protocol_issues_evaluation.append(f"    BGP 配置问题的节点: {bgp_nodes}")
                logging.warning(f"BGP 配置问题的节点: {bgp_nodes}")
            if tracert_issues:
                tracert_nodes = ', '.join([self.telnet_sysnames.get(node, node) for node in tracert_issues])
                protocol_issues_evaluation.append(f"    Tracert 配置问题的节点: {tracert_nodes}")
                logging.warning(f"Tracert 配置问题的节点: {tracert_nodes}")
            if ospf_neighbor_issues:
                ospf_neighbor_nodes = ', '.join([self.telnet_sysnames.get(node, node) for node in ospf_neighbor_issues])
                protocol_issues_evaluation.append(f"    OSPF 邻居状态问题的节点: {ospf_neighbor_nodes}")
                logging.warning(f"OSPF 邻居状态问题的节点: {ospf_neighbor_nodes}")
        else:
            protocol_issues_evaluation.append("所有节点的 OSPF、BGP、ISIS 和 Tracert 配置均正常。")
            logging.info("所有节点的 OSPF、BGP、ISIS 和 Tracert 配置均正常。")

        mapping["protocol_issues_evaluation"] = protocol_issues_evaluation

        return mapping

    def write_output(self, output_path: str, mapping: Dict[str, Any]):
        """
        将结果写入输出的 JSON 文件。

        :param output_path: 输出文件路径。
        :param mapping: 结果字典。
        """  
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(mapping, f, indent=4, ensure_ascii=False)  # 使用 ensure_ascii=False 以支持中文
            logging.info(f"Mapping results written to {output_path}")
        except IOError as e:
            logging.error(f"Error writing to output file: {e}")
            sys.exit(1)

    def write_interface_status(self, data_txt_path: str, mapping: Dict[str, Any]):
        """
        写入接口状态和 OSPF/ISIS/BGP 状态到 data.txt。
        """
        try:
            with open(data_txt_path, 'w', encoding='utf-8') as f:
                telnet_devices = mapping.get("telnet_devices", {})
                # as_boundary_router_count = mapping.get("as_boundary_router_count", {})  # 已删除
                evaluation_per_as = mapping.get("evaluation_per_as", {})
                total_nodes = mapping.get("total_nodes", 0)
                protocol_issues_evaluation = mapping.get("protocol_issues_evaluation", [])

                for host_port, device_info in telnet_devices.items():
                    sysname = device_info.get("sysname", "未知节点")
                    interface_status_list = device_info.get("interface_status", [])
                    ospf_status = device_info.get("ospf_status", "未配置 OSPF")
                    isis_status = device_info.get("isis_status", "未配置 ISIS")
                    ospf_interfaces = device_info.get("ospf_interfaces", [])
                    isis_interfaces = device_info.get("isis_interfaces", [])
                    bgp_info = device_info.get("bgp_info", {})
                    ospf_neighbors_details = device_info.get("ospf_neighbors_details", [])
                    tracert_status = device_info.get("tracert_status", "未执行")  # 新增 tracert 状态

                    f.write(f"节点: {sysname} ({host_port})\n")
                    for status in interface_status_list:
                        f.write(f"    接口: {status}\n")
                    f.write(f"    OSPF 状态: {ospf_status}\n")
                    f.write(f"    ISIS 状态: {isis_status}\n")

                    # 写入 BGP 信息
                    if bgp_info and bgp_info.get("bgp_local_router_id") != "未知":
                        f.write(f"    BGP 本地 Router ID: {bgp_info.get('bgp_local_router_id')}\n")
                        f.write(f"    BGP 本地 AS Number: {bgp_info.get('bgp_local_as_number')}\n")
                        f.write(f"    BGP 总邻居数量: {bgp_info.get('bgp_total_peers')}\n")
                        f.write(f"    BGP 建立状态的邻居数量: {bgp_info.get('bgp_established_peers')}\n")

                        non_established_peers = bgp_info.get("bgp_non_established_peers", [])
                        if non_established_peers:
                            f.write(f"    BGP 未建立状态的邻居:\n")
                            for peer in non_established_peers:
                                peer_ip = peer.get("peer_ip", "未知")
                                peer_as = peer.get("peer_as", "未知")
                                state = peer.get("state", "未知")
                                f.write(f"        Peer IP: {peer_ip}, AS: {peer_as}, State: {state}\n")
                        else:
                            f.write(f"    BGP 未建立状态的邻居: 无\n")
                    else:
                        f.write(f"    BGP 未配置\n")

                    # 写入 OSPF 邻居详细信息
                    if ospf_neighbors_details:
                        f.write(f"    OSPF 邻居详细信息:\n")
                        for neighbor in ospf_neighbors_details:
                            iface = neighbor.get("interface", "未知接口")
                            neighbor_router_id = neighbor.get("router_id", "未知 Router ID")
                            address = neighbor.get("address", "未知地址")
                            state = neighbor.get("state", "未知状态")
                            is_problematic = neighbor.get("is_problematic", False)
                            problem_flag = " [问题]" if is_problematic else ""
                            f.write(f"        接口: {iface}, Router ID: {neighbor_router_id}, Address: {address}, State: {state}{problem_flag}\n")
                    else:
                        f.write(f"    OSPF 邻居详细信息: 无\n")

                    # 写入 Tracert 状态
                    f.write(f"    Tracert 状态: {tracert_status}\n")

                    f.write("\n")  # 添加设备之间的空行

                # # 写入评估信息
                # f.write("评估:\n")
                # for as_number, evaluation in evaluation_per_as.items():
                #     f.write(f"    {evaluation}\n")

                # 分开写入协议配置问题评估信息
                f.write("\n协议配置问题评估:\n")
                for line in protocol_issues_evaluation:
                    f.write(f"    {line}\n")

                # 写入总节点数量
                f.write(f"\n网络中的总节点数量: {total_nodes}\n")

            logging.info(f"接口状态已写入 {data_txt_path}")
        except IOError as e:
            logging.error(f"写入 {data_txt_path} 时出错: {e}")
            sys.exit(1)


def find_latest_folder(base_path: str) -> str:
    """
    Find the latest numbered folder within the base path.

    :param base_path: The base directory path.
    :return: The name of the latest folder.
    """
    try:
        all_folders = [f for f in os.listdir(base_path) if f.isdigit()]
        if not all_folders:
            raise ValueError("No numbered folders found in the base path.")
        latest_folder = max(all_folders, key=int)
        logging.info(f"Latest folder identified: {latest_folder}")
        return latest_folder
    except FileNotFoundError:
        logging.error(f"Base path not found: {base_path}")
        sys.exit(1)
    except ValueError as ve:
        logging.error(ve)
        sys.exit(1)


def load_telnet_info(input_path: str) -> Dict[str, Any]:
    """
    Load telnet information from a JSON file.

    :param input_path: Path to the param.json file.
    :return: Dictionary containing telnet information.
    """
    try:
        with open(input_path, 'r', encoding='utf-8') as f:
            telnet_info = json.load(f)
        logging.info(f"Successfully loaded telnet_info from {input_path}")
        return telnet_info
    except FileNotFoundError:
        logging.error(f"param.json file not found at path: {input_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logging.error(f"Error decoding JSON from param.json: {e}")
        sys.exit(1)


def main(input_path: str, output_path: str):
    """
    Main function to orchestrate the router configuration processing.

    :param input_path: Path to the param.json file, may contain {t} for latest folder.
    :param output_path: Path to the output JSON file, may contain {t} for latest folder.
    """
    base_path = "/uploadPath/reasoning"
    if "{t}" in input_path or "{t}" in output_path:
        latest_folder = find_latest_folder(base_path)
        input_path = input_path.replace("{t}", latest_folder)
        output_path = output_path.replace("{t}", latest_folder)
        logging.debug(f"Resolved input_path: {input_path}")
        logging.debug(f"Resolved output_path: {output_path}")

    telnet_info = load_telnet_info(input_path)
    router_manager = RouterManager(telnet_info)

    # 由于已删除 UNL 文件相关操作，此处不再读取 UNL 文件

    router_manager.connect_and_get_sysnames_and_configs()
    mapping = router_manager.collect_results()

    logging.info("Collected router configurations:")
    logging.info(json.dumps(mapping, indent=4, ensure_ascii=False))
    router_manager.write_output(output_path, mapping)

    # 定义 data.txt 的路径，放在 output_path 的同一目录下
    output_dir = os.path.dirname(output_path)
    data_txt_path = os.path.join(output_dir, "data.txt")
    router_manager.write_interface_status(data_txt_path, mapping)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process router configurations from param.json.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json, use {t} for latest folder number.")
    parser.add_argument("-o", "--output", required=True, help="Output path for process information, use {t} for latest folder number.")
    args = parser.parse_args()

    # Configure logging to include file handler
    logging.basicConfig(
        level=logging.DEBUG,  # Set to DEBUG for detailed logs
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("router_manager.log", encoding='utf-8')
        ]
    )
    # Disable all logging messages
    # logging.disable(logging.CRITICAL)

    main(args.input, args.output)
