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
import xml.etree.ElementTree as ET
import re

# Configure logging for better traceability and control
logging.basicConfig(
    level=logging.DEBUG,  # Set to DEBUG for more detailed logs
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)


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
        self.telnet_configurations: Dict[str, List[Dict[str, str]]] = {}  # Structured interface info
        self.telnet_ospf_interfaces: Dict[str, List[str]] = {}  # Interfaces configured with OSPF
        self.telnet_router_ids: Dict[str, str] = {}  # Router IDs per device
        self.telnet_ospf_peers: Dict[str, List[str]] = {}  # OSPF neighbor Router IDs per device
        self.telnet_isis_interfaces: Dict[str, List[str]] = {}  # Interfaces configured with ISIS
        self.telnet_isis_peers_count: Dict[str, int] = {}  # ISIS peer counts per device
        self.telnet_bgp_info: Dict[str, Dict[str, Any]] = {}  # BGP info per device
        self.network_connections: Optional[List[Dict[str, Any]]] = None
        self.node_interfaces: Dict[str, List[Dict[str, str]]] = {}  # Node interface info from UNL
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
                        router_id, neighbors = self.parse_display_ospf_peer(ospf_peer_output)
                        if router_id:
                            self.telnet_router_ids[key] = router_id
                        if neighbors is not None:
                            self.telnet_ospf_peers[key] = neighbors
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

    def parse_display_ospf_peer(self, output: str) -> Tuple[Optional[str], Optional[List[str]]]:
        """
        Parse the output of 'display ospf peer' to extract the current node's Router ID and neighbor Router IDs.

        :param output: Output of the command.
        :return: Tuple containing Router ID and list of neighbor Router IDs. Returns (None, None) if no relevant output.
        """
        router_id = None
        neighbors = []
        lines = output.splitlines()
        logging.debug(f"Parsing 'display ospf peer' output.")
        ospf_process_regex = re.compile(r'OSPF Process \d+ with Router ID (\d+\.\d+\.\d+\.\d+)')
        router_id_found = False

        for line in lines:
            ospf_process_match = ospf_process_regex.search(line)
            if ospf_process_match:
                router_id = ospf_process_match.group(1)
                router_id_found = True
                logging.debug(f"Detected Router ID: {router_id}")
                continue

            if router_id_found:
                neighbor_regex = re.compile(r'Router ID:\s+(\d+\.\d+\.\d+\.\d+)')
                neighbor_match = neighbor_regex.search(line)
                if neighbor_match:
                    neighbor_id = neighbor_match.group(1)
                    neighbors.append(neighbor_id)
                    logging.debug(f"Detected Neighbor Router ID: {neighbor_id}")

        if router_id_found:
            logging.info(f"Extracted Router ID: {router_id} with Neighbors: {neighbors}")
            return router_id, neighbors
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

        # Skip header lines until the data starts
        data_started = False
        for line in lines:
            if line.strip().startswith("Interface"):
                data_started = True
                continue
            if not data_started:
                continue
            if not line.strip() or re.match(r'^[-=]+$', line):
                continue
            # Example line:
            # Eth1/0/0          001         Up          Mtu:Dn/Lnk:Dn/IP:Dn 1497 L1/L2 No/No
            parts = line.split()
            if len(parts) < 1:
                continue
            iface = parts[0]
            # Use normalize_interface_name to handle both 'e1/0/0' and 'Ethernet1/0/0' and 'Ethernet 1/0/0'
            iface_formatted = normalize_interface_name(iface)
            interfaces.append(iface_formatted)
            logging.debug(f"Detected ISIS-configured interface: {iface_formatted}")

        logging.debug(f"Parsed ISIS interfaces: {interfaces}")
        return interfaces

    def parse_display_bgp_all_summary(self, output: str) -> Optional[Dict[str, Any]]:
        """
        解析 'display bgp all summary' 命令的输出，提取 BGP 信息并根据 AS 号比较判断路由器是否为边界路由器。

        :param output: 命令输出。
        :return: 包含 BGP 本地 Router ID、本地 AS 号、总对等体数、已建立对等体数、未建立对等体列表、
                边界路由器标志的字典。如果解析失败，则返回 None。
        """
        bgp_info = {
            "bgp_local_router_id": None,
            "bgp_local_as_number": None,
            "bgp_total_peers": 0,
            "bgp_established_peers": 0,
            "bgp_non_established_peers": [],
            "is_boundary_router": False  # 新增标志，用于表示是否为边界路由器
        }

        lines = output.splitlines()
        logging.debug("解析 'display bgp all summary' 输出。")

        # 定义正则表达式模式
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
        in_peer_table = False  # 标志，表示是否正在解析对等体表格

        for line in lines:
            # 提取键值对
            key_value_matches = key_value_regex.findall(line)
            for key, value in key_value_matches:
                key = key.strip().lower()
                if key == 'bgp local router id':
                    bgp_info["bgp_local_router_id"] = value
                    logging.debug(f"检测到 BGP 本地 Router ID: {bgp_info['bgp_local_router_id']}")
                elif key == 'local as number':
                    try:
                        local_as_number = int(value)
                        bgp_info["bgp_local_as_number"] = local_as_number
                        logging.debug(f"检测到本地 AS 号: {bgp_info['bgp_local_as_number']}")
                    except ValueError:
                        logging.error(f"无法解析本地 AS 号: {value}")
                elif key == 'total number of peers':
                    try:
                        bgp_info["bgp_total_peers"] = int(value)
                        logging.debug(f"检测到 BGP 总对等体数: {bgp_info['bgp_total_peers']}")
                    except ValueError:
                        logging.error(f"无法解析 BGP 总对等体数: {value}")
                elif key == 'peers in established state':
                    try:
                        bgp_info["bgp_established_peers"] = int(value)
                        logging.debug(f"检测到已建立的 BGP 对等体数: {bgp_info['bgp_established_peers']}")
                    except ValueError:
                        logging.error(f"无法解析已建立的 BGP 对等体数: {value}")

            # 识别对等体表格的开始
            if line.strip().startswith("Peer"):
                in_peer_table = True
                continue

            if in_peer_table:
                # 识别对等体表格的结束
                if re.match(r'^[-=]+$', line.strip()):
                    in_peer_table = False
                    continue

                # 解析对等体条目
                match = peer_entry_regex.match(line.strip())
                if match:
                    peer_as = int(match.group('peer_as'))
                    peer_as_numbers.add(peer_as)
                    state = match.group('state').lower()
                    if state != 'established':
                        bgp_info["bgp_non_established_peers"].append({
                            "peer_ip": match.group('peer_ip'),
                            "peer_as": match.group('peer_as'),
                            "state": match.group('state').capitalize()
                        })
                        logging.debug(f"检测到未建立的 BGP 对等体: IP={match.group('peer_ip')}, AS={match.group('peer_as')}, 状态={match.group('state').capitalize()}")
                else:
                    logging.debug(f"未匹配的 BGP 对等体行: {line}")

        # 判断是否为边界路由器
        if local_as_number is not None and any(peer_as != local_as_number for peer_as in peer_as_numbers):
            bgp_info["is_boundary_router"] = True
            logging.info("该路由器是边界路由器。")
        else:
            logging.info("该路由器不是边界路由器。")

        # 判断是否为边界路由器
        if bgp_info["bgp_local_router_id"] and bgp_info["bgp_local_as_number"]:
            logging.info(f"提取的 BGP 信息: {bgp_info}")
            return bgp_info
        else:
            logging.warning("未能从 'display bgp all summary' 输出中提取部分 BGP 信息。")
            return None

    def parse_display_ip_interface_brief(self, output: str) -> List[Dict[str, str]]:
        """
        Parse the output of 'display ip interface brief', exclude interfaces with 'unassigned' IP, and return structured data.

        :param output: Command output.
        :return: List of interface information dictionaries.
        """
        lines = output.splitlines()
        interfaces = []
        header_found = False

        # Regular expression to match interface lines
        interface_regex = re.compile(
            r'^\s*(?P<interface>\S+)\s+'
            r'(?P<ip_address>(?:\d{1,3}\.){3}\d{1,3}/\d{1,2}|unassigned)\s+'
            r'(?P<physical>up|down)\s+'
            r'(?P<protocol>up|down)\s+'
            r'(?P<vpn>\S+)'
        )

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
                    ip_address = match.group('ip_address')
                    if ip_address.lower() != 'unassigned':
                        iface = match.group('interface')
                        # Use normalize_interface_name to handle both 'e1/0/0' and 'Ethernet1/0/0' and 'Ethernet 1/0/0'
                        iface_formatted = normalize_interface_name(iface)
                        interface_info = {
                            'Interface': iface_formatted,
                            'IP Address/Mask': match.group('ip_address'),
                            'Physical': match.group('physical'),
                            'Protocol': match.group('protocol'),
                            'VPN': match.group('vpn')
                        }
                        interfaces.append(interface_info)
                        logging.debug(f"Parsed interface: {interface_info}")
                else:
                    logging.debug(f"Unmatched line in 'display ip interface brief': {line}")
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
        parsing = False  # Flag to indicate if parsing has started

        # Regular expression to match interface lines
        interface_regex = re.compile(r'^\s*(?P<interface>\S+)\s+[\d\.]+')

        for line in lines:
            if "Interfaces" in line:
                parsing = True
                logging.debug("Starting to parse OSPF interface information.")
                continue
            if parsing:
                # Skip empty lines and separator lines
                if not line.strip() or re.match(r'^[-=]+$', line):
                    continue
                # Skip lines that do not start with a valid interface name
                if line.strip().startswith("Area"):
                    continue

                # Match interface lines
                match = interface_regex.match(line)
                if match:
                    iface = match.group('interface')
                    # Use normalize_interface_name to handle both 'e1/0/0' and 'Ethernet1/0/0' and 'Ethernet 1/0/0'
                    iface_formatted = normalize_interface_name(iface)
                    interfaces.append(iface_formatted)
                    logging.debug(f"Formatted OSPF interface name: {iface_formatted}")
                else:
                    logging.debug(f"Unmatched OSPF interface line: {line}")
        logging.debug(f"Parsed OSPF interfaces: {interfaces}")
        return interfaces

    def connect_and_get_sysnames_and_configs(self):
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
                        tn.host, tn.port = host, port
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
        收集所有结果，执行接口匹配，并输出接口配置状态，包括 OSPF、ISIS 和 BGP 状态。
        还统计每个自治域中边界路由器的数量并判断其合理性。
        """
        # 存储接口状态信息
        interface_status = {}
        ospf_status = {}
        isis_status = {}
        bgp_info_dict = {}
        as_boundary_router_count = {}  # 按自治域统计边界路由器数量

        for host_port, sysname in self.telnet_sysnames.items():
            node_interfaces = self.node_interfaces.get(sysname, [])
            telnet_interfaces = self.telnet_configurations.get(host_port, [])
            ospf_interfaces = self.telnet_ospf_interfaces.get(host_port, [])
            isis_interfaces = self.telnet_isis_interfaces.get(host_port, [])
            isis_peers = self.telnet_isis_peers_count.get(host_port)
            router_id = self.telnet_router_ids.get(host_port)
            ospf_neighbors = self.telnet_ospf_peers.get(host_port)
            bgp_info = self.telnet_bgp_info.get(host_port)

            # 提取 Telnet 接口名称并转换为小写以便比较
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

            for iface in node_interfaces:
                # 使用 normalize_interface_name 格式化接口名称
                iface_name = iface['name']
                iface_formatted = normalize_interface_name(iface_name)
                iface_formatted_lower = iface_formatted.lower()
                logging.debug(f"[{host_port}] 格式化后的接口名称: {iface_formatted}")

                # 判断接口是否配置了 IP
                config_status = "已配置IP地址" if iface_formatted_lower in telnet_interface_names else "未配置IP地址"

                # 判断 OSPF 配置状态
                ospf_iface_status = "OSPF已配置" if iface_formatted_lower in ospf_interfaces_lower else "OSPF未配置"

                # 判断 ISIS 配置状态
                isis_iface_status = "ISIS已配置" if iface_formatted_lower in isis_interfaces_lower else "ISIS未配置"

                status = f"{iface_formatted}接口配置状态: {config_status}, {ospf_iface_status}, {isis_iface_status}"
                interface_status[host_port].append(status)
                logging.info(f"[{host_port}] {status}")

            # 判断 OSPF 状态
            if host_port in self.telnet_router_ids:
                if self.telnet_ospf_peers.get(host_port):
                    neighbors_str = ', '.join(self.telnet_ospf_peers[host_port])
                    ospf_status[host_port] = f"OSPF 配置正常，邻居 Router IDs: {neighbors_str}"
                else:
                    ospf_status[host_port] = "OSPF 配置问题：未检测到邻居 Router ID" if ospf_interfaces else "OSPF 未配置"
            else:
                ospf_status[host_port] = "OSPF 配置问题：未检测到 Router ID" if ospf_interfaces else "OSPF 未配置"

            # 判断 ISIS 状态
            if isis_peers is not None:
                if isis_peers > 0 and isis_interfaces:
                    isis_status[host_port] = f"ISIS 配置正常，邻居数量: {isis_peers}"
                elif isis_interfaces and isis_peers == 0:
                    isis_status[host_port] = "ISIS 配置问题：接口配置了 ISIS 但未检测到邻居 Router ID"
                elif not isis_interfaces and isis_peers > 0:
                    isis_status[host_port] = "ISIS 配置问题：存在 ISIS 邻居但未配置 ISIS 接口"
                else:
                    isis_status[host_port] = "ISIS 配置问题：未知情况"
            else:
                isis_status[host_port] = "ISIS 配置错误：接口配置了 ISIS 但未检测到邻居 Router ID" if isis_interfaces else "ISIS 未配置"

            # 提取 BGP 信息
            if bgp_info:
                bgp_local_router_id = bgp_info.get("bgp_local_router_id", "未知")
                bgp_local_as_number = bgp_info.get("bgp_local_as_number", "未知")
                bgp_total_peers = bgp_info.get("bgp_total_peers", 0)
                bgp_established_peers = bgp_info.get("bgp_established_peers", 0)
                bgp_non_established_peers = bgp_info.get("bgp_non_established_peers", [])
                is_boundary_router = bgp_info.get("is_boundary_router", False)

                bgp_info_dict[host_port] = {
                    "bgp_local_router_id": bgp_local_router_id,
                    "bgp_local_as_number": bgp_local_as_number,
                    "bgp_total_peers": bgp_total_peers,
                    "bgp_established_peers": bgp_established_peers,
                    "bgp_non_established_peers": bgp_non_established_peers,
                    "is_boundary_router": is_boundary_router
                }

                # 统计自治域边界路由器数量
                if is_boundary_router and isinstance(bgp_local_as_number, int):
                    as_number = bgp_local_as_number  # 路由器的本地 AS 号代表其所属的自治域
                    if as_number in as_boundary_router_count:
                        as_boundary_router_count[as_number] += 1
                    else:
                        as_boundary_router_count[as_number] = 1

                # 记录 BGP 状态日志
                if bgp_info.get("bgp_total_peers", 0) > 0:
                    logging.info(f"[{host_port}] BGP 本地 Router ID: {bgp_local_router_id}")
                    logging.info(f"[{host_port}] BGP 本地 AS Number: {bgp_local_as_number}")
                    logging.info(f"[{host_port}] BGP 总邻居数量: {bgp_total_peers}")
                    logging.info(f"[{host_port}] BGP 建立状态的邻居数量: {bgp_established_peers}")
                    if bgp_non_established_peers:
                        logging.info(f"[{host_port}] BGP 未建立状态的邻居: {bgp_non_established_peers}")
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
                    "is_boundary_router": False
                }
                logging.info(f"[{host_port}] BGP 未配置")

        # 获取总节点数量
        total_nodes = len(self.node_interfaces)
        logging.info(f"网络中的总节点数量: {total_nodes}")

        # 生成评估信息按自治域
        evaluation_per_as = {}
        for as_number, count in as_boundary_router_count.items():
            if count < 2:
                evaluation = f"自治域 {as_number} 中边界路由器数量为 {count}，建议增加边界路由器。"
                logging.warning(evaluation)
            elif 2 <= count < 5:
                evaluation = f"自治域 {as_number} 中边界路由器数量为 {count}，数量合理。"
                logging.info(evaluation)
            else:  # count >=5
                evaluation = f"自治域 {as_number} 中边界路由器数量为 {count}，建议减少边界路由器。"
                logging.warning(evaluation)
            evaluation_per_as[as_number] = evaluation

        return {
            "telnet_devices": {
                host_port: {
                    "sysname": sysname,
                    "router_id": self.telnet_router_ids.get(host_port, "未知 Router ID"),
                    "ospf_neighbors": self.telnet_ospf_peers.get(host_port, []),
                    "interfaces": self.telnet_configurations.get(host_port, []),
                    "ospf_interfaces": self.telnet_ospf_interfaces.get(host_port, []),
                    "isis_interfaces": self.telnet_isis_interfaces.get(host_port, []),
                    "interface_status": interface_status.get(host_port, []),
                    "ospf_status": ospf_status.get(host_port, "未配置 OSPF"),
                    "isis_status": isis_status.get(host_port, "未配置 ISIS"),
                    "bgp_info": bgp_info_dict.get(host_port, {})
                }
                for host_port, sysname in self.telnet_sysnames.items()
            },
            "network_connections": self.network_connections,
            "as_boundary_router_count": as_boundary_router_count,  # 按自治域统计边界路由器数量
            "total_nodes": total_nodes,  # 添加总节点数量到结果中
            "evaluation_per_as": evaluation_per_as  # 添加评估信息到结果中
        }

    def read_unl_file(self, lab_id: int):
        unl_file_path = f"/opt/unetlab/labs/{lab_id}.unl"
        try:
            with open(unl_file_path, 'r', encoding='utf-8') as f:
                unl_content = f.read()
            logging.info(f"Successfully read UNL file from {unl_file_path}")
            self.parse_unl_file(unl_content)
        except FileNotFoundError:
            logging.error(f"UNL file not found at path: {unl_file_path}")
        except IOError as e:
            logging.error(f"Error reading UNL file: {e}")

    def parse_unl_file(self, unl_content: str):
        """
        Parse the UNL file to extract network connections and node interface information.
        """
        try:
            root = ET.fromstring(unl_content)
            connections = []

            # First, parse nodes and their interfaces
            nodes = {}
            for node in root.findall(".//node"):
                node_id = node.get("id")
                node_name = node.get("name")
                nodes[node_id] = node_name

            # Build a mapping from network_id to list of (node_name, interface_name, type)
            network_to_interfaces = {}
            for node in root.findall(".//node"):
                node_id = node.get("id")
                node_name = nodes.get(node_id)
                for interface in node.findall("interface"):
                    network_id = interface.get("network_id")
                    interface_name = interface.get("name")
                    interface_type = interface.get("type", "ethernet")  # Default type is ethernet
                    if network_id and node_name:
                        normalized_iface_name = normalize_interface_name(interface_name)
                        if network_id not in network_to_interfaces:
                            network_to_interfaces[network_id] = []
                        network_to_interfaces[network_id].append({
                            "node_name": node_name,
                            "interface_name": normalized_iface_name,
                            "type": interface_type
                        })
                        # Store node interface information
                        if node_name not in self.node_interfaces:
                            self.node_interfaces[node_name] = []
                        self.node_interfaces[node_name].append({
                            "name": normalized_iface_name,
                            "type": interface_type
                        })

            # Now, for each network_id, if there are exactly two interfaces, create a connection
            for network_id, interfaces in network_to_interfaces.items():
                if len(interfaces) == 2:
                    connection = {
                        "network_id": network_id,
                        "node1": interfaces[0]["node_name"],
                        "interface1": interfaces[0]["interface_name"],
                        "node2": interfaces[1]["node_name"],
                        "interface2": interfaces[1]["interface_name"]
                    }
                    connections.append(connection)
                    logging.info(f"Connected {connection['node1']}:{connection['interface1']} <-> {connection['node2']}:{connection['interface2']} via network_id {network_id}")
                else:
                    logging.warning(f"Network_id {network_id} does not have exactly two interfaces. Skipping connection.")

            self.network_connections = connections
            logging.info(f"Successfully parsed UNL file into network connections.")
        except ET.ParseError as e:
            logging.error(f"Error parsing UNL file: {e}")

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
        写入接口状态和 OSPF/ISIS/BGP 状态到 data.txt，格式如下：
        节点: R3 (192.168.3.117:32899)
            接口: Ethernet1/0/0接口配置状态: 已配置IP地址, OSPF已配置, ISIS未配置
            接口: Ethernet1/0/1接口配置状态: 已配置IP地址, OSPF已配置, ISIS未配置
            ...
            OSPF 状态: OSPF 配置正常，邻居 Router IDs: 2.2.2.2, 1.1.1.1
            ISIS 状态: ISIS 配置正常，邻居数量: 1
            BGP 本地 Router ID: 3.3.3.3
            BGP 本地 AS Number: 100
            BGP 总邻居数量: 3
            BGP 建立状态的邻居数量: 2
            BGP 未建立状态的邻居:
                Peer IP: x.x.x.x, AS: y, State: Z

        自治域边界路由器统计:
            自治域 100 边界路由器数量: 2
            自治域 200 边界路由器数量: 1
            ...

        评估:
            自治域 100 中边界路由器数量为 2，数量合理。
            自治域 200 中边界路由器数量为 1，建议增加边界路由器。
        """
        try:
            with open(data_txt_path, 'w', encoding='utf-8') as f:
                telnet_devices = mapping.get("telnet_devices", {})
                as_boundary_router_count = mapping.get("as_boundary_router_count", {})
                evaluation_per_as = mapping.get("evaluation_per_as", {})
                total_nodes = mapping.get("total_nodes", 0)

                for host_port, device_info in telnet_devices.items():
                    sysname = device_info.get("sysname", "未知节点")
                    interface_status_list = device_info.get("interface_status", [])
                    ospf_status = device_info.get("ospf_status", "未配置 OSPF")
                    isis_status = device_info.get("isis_status", "未配置 ISIS")
                    bgp_info = device_info.get("bgp_info", {})

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

                    f.write("\n")  # 添加设备之间的空行

                # 写入自治域边界路由器统计
                f.write("自治域边界路由器统计:\n")
                for as_number, count in as_boundary_router_count.items():
                    f.write(f"    自治域 {as_number} 边界路由器数量: {count}\n")
                f.write("\n")

                # 写入评估信息
                f.write("评估:\n")
                for as_number, evaluation in evaluation_per_as.items():
                    f.write(f"    {evaluation}\n")

                # 写入总节点数量
                f.write(f"\n网络中的总节点数量: {total_nodes}\n")

            logging.info(f"接口状态已写入 {data_txt_path}")
        except IOError as e:
            logging.error(f"写入 {data_txt_path} 时出错: {e}")
            sys.exit(1)


def find_latest_folder(base_path: str) -> str:
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
    base_path = "/uploadPath/reasoning"
    if "{t}" in input_path or "{t}" in output_path:
        latest_folder = find_latest_folder(base_path)
        input_path = input_path.replace("{t}", latest_folder)
        output_path = output_path.replace("{t}", latest_folder)
        logging.debug(f"Resolved input_path: {input_path}")
        logging.debug(f"Resolved output_path: {output_path}")

    telnet_info = load_telnet_info(input_path)
    router_manager = RouterManager(telnet_info)

    # 读取基于 labId 的 UNL 文件
    lab_id = telnet_info.get("labId")
    if lab_id is not None:
        router_manager.read_unl_file(lab_id)
    else:
        logging.warning("labId not found in telnet_info.")

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
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("router_manager.log", encoding='utf-8')
        ]
    )
    main(args.input, args.output)
