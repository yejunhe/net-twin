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
            "h3c": (
                [
                    'screen-length disable',
                    'display ip interface brief',
                    'display ospf interface',
                    'display ospf peer',
                    'display isis interface',
                    'display isis peer',
                    'display bgp peer ipv4'  # 新命令
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

            command_outputs = {}

            for cmd in commands:
                tn.write(cmd.encode('ascii') + b'\n')
                logging.info(f"[{tn.host}:{tn.port}] Sending command: {cmd}")
                time.sleep(2)  # Increased wait time to ensure complete command output
                cmd_output = tn.read_very_eager().decode('ascii', errors='ignore')
                command_outputs[cmd] = cmd_output
                logging.debug(f"[{tn.host}:{tn.port}] Output for '{cmd}':\n{cmd_output}")

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
                        isis_peers_info = self.parse_display_isis_peer(isis_peer_output)
                        if isis_peers_info:
                            self.telnet_isis_peers_count[key] = len(isis_peers_info.get('peers', []))
                    else:
                        logging.warning(f"[{tn.host}:{tn.port}] Missing 'display isis peer' output.")

                    # Parse 'display bgp peer ipv4'
                    if 'display bgp peer ipv4' in command_outputs:
                        bgp_output = command_outputs['display bgp peer ipv4']
                        bgp_info = self.parse_display_bgp_peer_ipv4(bgp_output)
                        if bgp_info:
                            self.telnet_bgp_info[key] = bgp_info
                    else:
                        logging.warning(f"[{tn.host}:{tn.port}] Missing 'display bgp peer ipv4' output.")
        else:
            logging.warning(f"[{tn.host}:{tn.port}] No output received from Telnet commands.")
        return command_outputs

    def parse_display_ospf_peer(self, output: str) -> Tuple[Optional[str], Optional[List[str]]]:
        """
        解析 'display ospf peer' 的输出，以提取本地 Router ID 和邻居 Router IDs。

        :param output: 命令输出。
        :return: 元组 (本地 Router ID, 邻居 Router IDs 列表)。如果无法解析，返回 (None, None)。
        """
        router_id = None
        neighbors = []
        lines = output.splitlines()
        logging.debug("解析 'display ospf peer' 输出。")

        # 正则表达式匹配本地 Router ID
        ospf_process_regex = re.compile(r'OSPF Process \d+ with Router ID (\d+\.\d+\.\d+\.\d+)')
        # 正则表达式匹配表格标题
        table_header_regex = re.compile(r'^Router ID\s+Address\s+Pri\s+Dead-Time\s+State\s+Interface', re.IGNORECASE)
        # 正则表达式匹配表格行
        table_row_regex = re.compile(
            r'^(?P<router_id>\d+\.\d+\.\d+\.\d+)\s+'      # Router ID
            r'(?P<address>\d+\.\d+\.\d+\.\d+)\s+'        # Address
            r'(?P<pri>\d+)\s+'                           # Pri
            r'(?P<dead_time>\d+)\s+'                     # Dead-Time
            r'(?P<state>\S+/\S+)\s+'                     # State
            r'(?P<interface>\S+)'                        # Interface
        )

        in_table = False  # 标志是否进入表格部分

        for line in lines:
            line = line.strip()
            if not line:
                continue  # 跳过空行

            # 提取本地 Router ID
            if not router_id:
                match = ospf_process_regex.search(line)
                if match:
                    router_id = match.group(1)
                    logging.debug(f"检测到本地 Router ID: {router_id}")
                continue

            # 检测表格标题，开始解析表格行
            if table_header_regex.match(line):
                in_table = True
                logging.debug("检测到邻居信息表格标题，开始解析表格行。")
                continue

            # 如果处于表格解析状态，尝试解析表格行
            if in_table:
                match = table_row_regex.match(line)
                if match:
                    neighbor_router_id = match.group('router_id')
                    neighbors.append(neighbor_router_id)
                    logging.debug(f"检测到邻居 Router ID: {neighbor_router_id}")
                else:
                    logging.debug(f"未匹配的表格行: {line}")

        if router_id:
            logging.info(f"提取到本地 Router ID: {router_id} 和邻居 Router IDs: {neighbors}")
            return router_id, neighbors
        else:
            logging.warning("未能提取到本地 Router ID。")
            return None, None

    def parse_display_isis_interface(self, output: str) -> List[str]:
        """
        解析 'display isis interface' 的输出，以提取配置了 ISIS 的接口名称。

        :param output: 命令输出。
        :return: ISIS 配置的接口名称列表，格式如 'GigabitEthernet1/0'。
        """
        interfaces = []
        lines = output.splitlines()
        current_interface = None
        in_interface_block = False

        # 正则表达式匹配接口名称行
        interface_regex = re.compile(r'^Interface:\s+(?P<interface>\S+)', re.IGNORECASE)

        # 遍历每一行，查找接口名称
        for line in lines:
            line = line.strip()
            if not line:
                continue  # 跳过空行

            # 检测接口名称行
            match = interface_regex.match(line)
            if match:
                current_interface = match.group('interface')
                in_interface_block = True
                logging.debug(f"检测到接口: {current_interface}")
                continue

            # 如果在接口块中，解析接口状态
            if in_interface_block and current_interface:
                # 假设接口状态在 'IPv4 state' 列，可以根据实际需要调整
                # 例如，检查 'IPv4 state' 是否为 'Up'
                # 解析当前接口的状态信息
                state_line = line
                state_parts = state_line.split()
                if len(state_parts) >= 3:
                    ipv4_state = state_parts[1]
                    # 仅当 IPv4 state 为 'Up' 时，认为接口已配置 ISIS
                    if ipv4_state.lower() == 'up':
                        standardized_interface = self.standardize_interface_name(current_interface)
                        if standardized_interface and standardized_interface not in interfaces:
                            interfaces.append(standardized_interface)
                            logging.debug(f"接口 {current_interface} 标准化为 {standardized_interface} 并添加到列表。")
                    else:
                        logging.debug(f"接口 {current_interface} 的 IPv4 state 为 '{ipv4_state}'，未添加到列表。")
                # 结束当前接口块的解析
                in_interface_block = False
                current_interface = None

        logging.debug(f"解析后的 ISIS 接口列表: {interfaces}")
        return interfaces

    def parse_display_isis_peer(self, output: str) -> Dict[str, Any]:
        """
        解析 'display isis peer' 的输出，以提取本地 System ID 和邻居信息。

        :param output: 命令输出。
        :return: 包含本地 System ID 和邻居信息的字典。如果无法解析，返回空字典。
        """
        local_system_id = None
        peers = []
        lines = output.splitlines()
        logging.debug("解析 'display isis peer' 输出。")

        # 正则表达式匹配本地 System ID
        system_id_regex = re.compile(r'^System ID:\s+(\d+\.\d+\.\d+\.\d+)', re.IGNORECASE)
        # 正则表达式匹配邻居信息表格行
        peer_entry_regex = re.compile(
            r'^(?P<index>\d+)\s+'          # Index
            r'(?P<ipv4_state>\w+)\s+'      # IPv4 state
            r'(?P<ipv6_state>\w+)\s+'      # IPv6 state
            r'(?P<circuit_id>\d+)\s+'      # Circuit ID
            r'(?P<mtu>\d+)\s+'             # MTU
            r'(?P<type>\S+)\s+'            # Type
            r'(?P<dis>\S+)'                # DIS
        )

        for line in lines:
            line = line.strip()
            if not line:
                continue  # 跳过空行

            # 提取本地 System ID
            if not local_system_id:
                match = system_id_regex.match(line)
                if match:
                    local_system_id = match.group(1)
                    logging.debug(f"检测到本地 System ID: {local_system_id}")
                continue

            # 提取邻居信息
            match = peer_entry_regex.match(line)
            if match:
                peer = {
                    'index': match.group('index'),
                    'ipv4_state': match.group('ipv4_state'),
                    'ipv6_state': match.group('ipv6_state'),
                    'circuit_id': match.group('circuit_id'),
                    'mtu': match.group('mtu'),
                    'type': match.group('type'),
                    'dis': match.group('dis')
                }
                peers.append(peer)
                logging.debug(f"检测到邻居信息: {peer}")

        if local_system_id:
            logging.info(f"提取到本地 System ID: {local_system_id} 和 {len(peers)} 个邻居。")
            return {
                'local_system_id': local_system_id,
                'peers': peers
            }
        else:
            logging.warning("未能从 'display isis peer' 输出中提取到本地 System ID。")
            return {}

    def parse_display_bgp_peer_ipv4(self, output: str) -> Dict[str, Any]:
        """
        解析 'display bgp peer ipv4' 的输出，以提取本地 BGP 信息和邻居信息。

        :param output: 命令输出。
        :return: 包含本地 BGP 信息和邻居信息的字典。如果无法解析，返回空字典。
        """
        bgp_info = {
            'bgp_local_router_id': None,
            'bgp_local_as_number': None,
            'bgp_total_peers': 0,
            'bgp_established_peers': 0,
            'peers': []
        }
        lines = output.splitlines()
        logging.debug("解析 'display bgp peer ipv4' 输出。")

        # 正则表达式匹配本地 BGP 信息
        local_router_id_regex = re.compile(r'^BGP local router ID:\s+(\d+\.\d+\.\d+\.\d+)', re.IGNORECASE)
        local_as_number_regex = re.compile(r'^Local AS number:\s+(\d+)', re.IGNORECASE)
        total_peers_regex = re.compile(r'^Total number of peers:\s+(\d+)\s+Peers in established state:\s+(\d+)', re.IGNORECASE)
        
        # 正则表达式匹配表格行
        # 处理可能有 '*' 前缀的 Peer 行
        peer_entry_regex = re.compile(
            r'^(?:\*\s*)?(?P<peer_ip>\d+\.\d+\.\d+\.\d+)\s+'   # Peer IP，可能带有 '*' 前缀
            r'(?P<as_number>\d+)\s+'                         # AS
            r'(?P<msg_rcvd>\d+)\s+'                          # MsgRcvd
            r'(?P<msg_sent>\d+)\s+'                          # MsgSent
            r'(?P<out_q>\d+)\s+'                             # OutQ
            r'(?P<pref_rcv>\d+)\s+'                          # PrefRcv
            r'(?P<up_down>\S+)\s+'                           # Up/Down
            r'(?P<state>\S+)'                                # State
        )

        in_peer_table = False  # 标志是否进入 Peer 信息表格

        for line in lines:
            line = line.strip()
            if not line:
                continue  # 跳过空行

            # 提取本地 BGP 信息
            if bgp_info['bgp_local_router_id'] is None:
                match = local_router_id_regex.match(line)
                if match:
                    bgp_info['bgp_local_router_id'] = match.group(1)
                    logging.debug(f"检测到本地 BGP Router ID: {bgp_info['bgp_local_router_id']}")
                continue

            if bgp_info['bgp_local_as_number'] is None:
                match = local_as_number_regex.match(line)
                if match:
                    bgp_info['bgp_local_as_number'] = int(match.group(1))
                    logging.debug(f"检测到本地 BGP AS Number: {bgp_info['bgp_local_as_number']}")
                continue

            if bgp_info['bgp_total_peers'] == 0 and bgp_info['bgp_established_peers'] == 0:
                match = total_peers_regex.match(line)
                if match:
                    bgp_info['bgp_total_peers'] = int(match.group(1))
                    bgp_info['bgp_established_peers'] = int(match.group(2))
                    logging.debug(f"检测到总 Peer 数量: {bgp_info['bgp_total_peers']}，建立状态的 Peer 数量: {bgp_info['bgp_established_peers']}")
                continue

            # 检测表格标题
            if re.match(r'^Peer\s+AS\s+MsgRcvd\s+MsgSent\s+OutQ\s+PrefRcv\s+Up/Down\s+State', line, re.IGNORECASE):
                in_peer_table = True
                logging.debug("检测到 Peer 信息表格标题，开始解析 Peer 信息。")
                continue

            # 如果处于 Peer 信息表格中，解析每一行 Peer 信息
            if in_peer_table:
                match = peer_entry_regex.match(line)
                if match:
                    peer = {
                        'peer_ip': match.group('peer_ip'),
                        'as_number': int(match.group('as_number')),
                        'msg_rcvd': int(match.group('msg_rcvd')),
                        'msg_sent': int(match.group('msg_sent')),
                        'out_q': int(match.group('out_q')),
                        'pref_rcv': int(match.group('pref_rcv')),
                        'up_down': match.group('up_down'),
                        'state': match.group('state')
                    }
                    bgp_info['peers'].append(peer)
                    logging.debug(f"检测到 Peer 信息: {peer}")
                else:
                    logging.debug(f"未匹配的 Peer 行: {line}")

        # 确保必填字段已提取
        if bgp_info['bgp_local_router_id'] and bgp_info['bgp_local_as_number']:
            # 分离未建立状态的 Peers
            bgp_non_established_peers = [
                peer for peer in bgp_info.get("peers", [])
                if peer.get("state", "").lower() != "established"
            ]
            bgp_info['bgp_non_established_peers'] = bgp_non_established_peers

            logging.info(f"提取到本地 BGP Router ID: {bgp_info['bgp_local_router_id']}，AS Number: {bgp_info['bgp_local_as_number']}，总 Peers: {bgp_info['bgp_total_peers']}，建立状态的 Peers: {bgp_info['bgp_established_peers']}")
            logging.info(f"提取到 {len(bgp_info['peers'])} 个 Peer 信息。")
            return bgp_info
        else:
            logging.warning("未能完全提取到本地 BGP 信息。")
            return {}

    def parse_display_ip_interface_brief(self, output: str) -> List[Dict[str, str]]:
        """
        解析 'display ip interface brief' 的输出，排除IP地址为 '--' 的接口，并返回结构化数据。

        :param output: 命令输出。
        :return: 接口信息字典的列表。
        """
        lines = output.splitlines()
        interfaces = []
        header_found = False

        # 更新后的正则表达式，匹配新的输出格式
        interface_regex = re.compile(
            r'^\s*(?P<interface>\S+)\s+'                    # Interface
            r'(?P<physical>up|down)\s+'                     # Physical
            r'(?P<protocol>up(?:\(\w\))?|down(?:\(\w\))?)\s+'# Protocol，可能带有附加信息如(up(s))
            r'(?P<ip_address>(?:\d{1,3}\.){3}\d{1,3}/\d{1,2}|--)\s+' # IP address/Mask 或 --
            r'(?P<vpn>\S+)\s+'                               # VPN instance
            r'(?P<description>.*)$'                          # Description
        )

        for line in lines:
            # 查找表头
            if not header_found:
                if re.match(r'^Interface\s+Physical\s+Protocol\s+IP address/Mask\s+VPN instance\s+Description', line):
                    header_found = True
                    logging.debug("找到 'display ip interface brief' 表头。")
                continue
            else:
                # 跳过空行或分隔线
                if not line.strip() or re.match(r'^[-=]+$', line):
                    continue

                match = interface_regex.match(line)
                if match:
                    ip_address = match.group('ip_address')
                    if ip_address != '--':
                        interface_info = {
                            'Interface': match.group('interface'),
                            'IP Address/Mask': match.group('ip_address'),
                            'Physical': match.group('physical'),
                            'Protocol': match.group('protocol'),
                            'VPN': match.group('vpn'),
                            'Description': match.group('description').strip()  # 去除描述字段的前后空白
                        }
                        interfaces.append(interface_info)
                        logging.debug(f"解析的接口信息: {interface_info}")
                else:
                    logging.debug(f"在 'display ip interface brief' 中未匹配的行: {line}")
                    continue

        logging.debug(f"解析后的 Telnet 接口列表: {interfaces}")
        return interfaces

    def parse_display_ospf_interface(self, output: str) -> List[str]:
        """
        解析 'display ospf interface' 的输出，以提取配置了 OSPF 的接口名称。

        :param output: 命令输出。
        :return: OSPF 配置的接口名称列表，格式如 'GigabitEthernet1/0'。
        """
        interfaces = []
        lines = output.splitlines()
        in_interfaces_section = False
        in_table_header = False

        # 正则表达式匹配 Area 行和 IP Address 行
        area_regex = re.compile(r'^Area:\s+\d+\.\d+\.\d+\.\d+')
        ip_header_regex = re.compile(r'^IP Address\s+Type\s+State\s+Cost\s+Pri\s+DR\s+BDR', re.IGNORECASE)
        ip_entry_regex = re.compile(
            r'^(?P<ip_address>(?:\d{1,3}\.){3}\d{1,3}/\d{1,2}|\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\s+'
            r'(?P<type>\S+)\s+'
            r'(?P<state>\S+)\s+'
            r'(?P<cost>\d+)\s+'
            r'(?P<pri>\d+)\s+'
            r'(?P<dr>\d+\.\d+\.\d+\.\d+)\s+'
            r'(?P<bdr>\d+\.\d+\.\d+\.\d+)'
        )

        # 收集 OSPF 配置的 IP 地址
        ospf_ip_addresses = set()

        for line in lines:
            line = line.strip()
            if not in_interfaces_section:
                if line.startswith("Interfaces"):
                    in_interfaces_section = True
                    logging.debug("进入 'Interfaces' 部分。")
                continue
            else:
                if area_regex.match(line):
                    logging.debug(f"检测到 Area 行: {line}")
                    continue
                if ip_header_regex.match(line):
                    in_table_header = True
                    logging.debug("检测到 IP 地址表头。")
                    continue
                if in_table_header:
                    if not line or re.match(r'^[-=]+$', line):
                        continue  # 跳过空行和分隔线
                    match = ip_entry_regex.match(line)
                    if match:
                        ip_address_full = match.group('ip_address')
                        # 如果 IP 地址带有掩码（如10.0.38.2/24），去除掩码部分
                        ip_address = ip_address_full.split('/')[0] if '/' in ip_address_full else ip_address_full
                        ospf_ip_addresses.add(ip_address)
                        logging.debug(f"提取到 OSPF 配置的 IP 地址: {ip_address}")
                    else:
                        logging.debug(f"未匹配的 OSPF 接口行: {line}")

        # 通过 IP 地址映射到接口名称
        # self.telnet_configurations 是一个字典，键为 "host:port"，值为接口信息列表
        for host_port, interfaces_info in self.telnet_configurations.items():
            for iface in interfaces_info:
                ip_mask = iface.get('IP Address/Mask', '')
                iface_ip = ip_mask.split('/')[0] if '/' in ip_mask else ip_mask
                if iface_ip in ospf_ip_addresses:
                    interface_name = iface.get('Interface', '').lower()
                    standardized_interface = self.standardize_interface_name(interface_name)
                    if standardized_interface and standardized_interface not in interfaces:
                        interfaces.append(standardized_interface)
                        logging.debug(f"通过 IP 地址 {iface_ip} 映射到接口名称: {standardized_interface}")

        logging.debug(f"解析后的 OSPF 接口列表: {interfaces}")
        return interfaces

    def standardize_interface_name(self, interface_name: str) -> Optional[str]:
        """
        将接口名称标准化为统一格式，例如将 'GE1/0' 转换为 'GigabitEthernet1/0'。

        :param interface_name: 原始接口名称。
        :return: 标准化后的接口名称，或 None 如果无法标准化。
        """
        interface_mapping = {
            'ge': 'GigabitEthernet',
            'gi': 'GigabitEthernet',
            'eth': 'Ethernet',
            'loop': 'Loopback',
            'lo': 'Loopback',
            # 根据实际需要添加更多接口类型的映射
        }

        # 正则表达式匹配接口类型和编号，例如 'GE1/0' 或 'Loop0'
        pattern = re.compile(r'^([a-zA-Z]+)(\d+)(?:/(\d+))?$')
        match = pattern.match(interface_name.lower())
        if match:
            iface_type, slot, port = match.groups()
            standardized_type = interface_mapping.get(iface_type, iface_type.capitalize())
            if port:
                # 构建标准化接口名称，包含 '/' 和端口号
                standardized_name = f"{standardized_type}{slot}/{port}"
            else:
                # 构建标准化接口名称，不包含 '/'
                standardized_name = f"{standardized_type}{slot}"
            return standardized_name
        else:
            logging.warning(f"无法标准化的接口名称: {interface_name}")
            return None

    def connect_and_get_sysnames_and_configs(self):
        nodes = self.telnet_info.get("node", [])
        if not nodes:
            logging.warning("No nodes found in telnet_info.")
            return

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_node = {}
            for node in nodes:
                image_type = node.get("image_type", "").lower()
                if "h3c" in image_type:
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
        Collect all results, perform interface matching, and output interface configuration status including OSPF, ISIS, and BGP statuses.
        """
        # Store interface status information
        interface_status = {}
        ospf_status = {}
        isis_status = {}
        bgp_info_dict = {}

        for host_port, sysname in self.telnet_sysnames.items():
            node_interfaces = self.node_interfaces.get(sysname, [])
            telnet_interfaces = self.telnet_configurations.get(host_port, [])
            ospf_interfaces = self.telnet_ospf_interfaces.get(host_port, [])
            isis_interfaces = self.telnet_isis_interfaces.get(host_port, [])
            isis_peers = self.telnet_isis_peers_count.get(host_port)
            router_id = self.telnet_router_ids.get(host_port)
            ospf_neighbors = self.telnet_ospf_peers.get(host_port)
            bgp_info = self.telnet_bgp_info.get(host_port)

            # Extract interface names from Telnet and standardize them
            telnet_interface_names = [
                self.standardize_interface_name(iface['Interface']) for iface in telnet_interfaces
                if isinstance(iface, dict) and self.standardize_interface_name(iface['Interface'])
            ]

            # Convert to lowercase for case-insensitive comparison
            telnet_interface_names_lower = set(name.lower() for name in telnet_interface_names)
            ospf_interfaces_lower = set(iface.lower() for iface in ospf_interfaces)
            isis_interfaces_lower = set(iface.lower() for iface in isis_interfaces)

            logging.debug(f"[{host_port}] Telnet fetched interfaces: {telnet_interface_names}")
            logging.debug(f"[{host_port}] OSPF-configured interfaces: {ospf_interfaces_lower}")
            logging.debug(f"[{host_port}] ISIS-configured interfaces: {isis_interfaces_lower}")
            if bgp_info:
                logging.debug(f"[{host_port}] BGP info: {bgp_info}")

            interface_status[host_port] = []
            ospf_status[host_port] = ""
            isis_status[host_port] = ""
            bgp_info_dict[host_port] = {}

            for iface in node_interfaces:
                # Get raw interface name
                iface_name = iface['name']
                
                # Standardize the interface name
                standardized_interface = self.standardize_interface_name(iface_name)
                
                if standardized_interface:
                    iface_formatted = standardized_interface
                    iface_formatted_lower = iface_formatted.lower()
                    logging.debug(f"[{host_port}] Standardized interface name: {iface_formatted}")

                    # Determine if interface has IP configured
                    config_status = "已配置IP地址" if iface_formatted_lower in telnet_interface_names_lower else "未配置IP地址"

                    # Determine OSPF configuration status
                    ospf_iface_status = "OSPF已配置" if iface_formatted_lower in ospf_interfaces_lower else "OSPF未配置"

                    # Determine ISIS configuration status
                    isis_iface_status = "ISIS已配置" if iface_formatted_lower in isis_interfaces_lower else "ISIS未配置"

                    status = f"{iface_formatted}接口配置状态: {config_status}, {ospf_iface_status}, {isis_iface_status}"
                    interface_status[host_port].append(status)
                    logging.info(f"[{host_port}] {status}")
                else:
                    logging.warning(f"[{host_port}] Interface name '{iface_name}' could not be standardized. Skipping.")
                    continue

            # Determine OSPF status based on 'display ospf peer' and OSPF interfaces
            if host_port in self.telnet_router_ids:
                if self.telnet_ospf_peers.get(host_port):
                    neighbors_str = ', '.join(self.telnet_ospf_peers[host_port])
                    ospf_status[host_port] = f"OSPF 配置正常，邻居 Router IDs: {neighbors_str}"
                else:
                    ospf_status[host_port] = "OSPF 配置问题：未检测到邻居 Router ID" if ospf_interfaces else "OSPF 未配置"
            else:
                ospf_status[host_port] = "OSPF 配置问题：未检测到 Router ID" if ospf_interfaces else "OSPF 未配置"

            # Determine ISIS status based on 'display isis peer' and ISIS interfaces
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

            # Extract BGP info
            if bgp_info:
                bgp_non_established_peers = bgp_info.get("bgp_non_established_peers", [])
                bgp_info_dict[host_port] = {
                    "bgp_local_router_id": bgp_info.get("bgp_local_router_id", "未知"),
                    "bgp_local_as_number": bgp_info.get("bgp_local_as_number", "未知"),
                    "bgp_total_peers": bgp_info.get("bgp_total_peers", 0),
                    "bgp_established_peers": bgp_info.get("bgp_established_peers", 0),
                    "bgp_non_established_peers": bgp_non_established_peers
                }
            else:
                bgp_info_dict[host_port] = {
                    "bgp_local_router_id": "未知",
                    "bgp_local_as_number": "未知",
                    "bgp_total_peers": 0,
                    "bgp_established_peers": 0,
                    "bgp_non_established_peers": []
                }

            # Log BGP status
            if bgp_info:
                if bgp_info.get("bgp_total_peers", 0) > 0:
                    logging.info(f"[{host_port}] BGP 本地 Router ID: {bgp_info.get('bgp_local_router_id')}")
                    logging.info(f"[{host_port}] BGP 本地 AS Number: {bgp_info.get('bgp_local_as_number')}")
                    logging.info(f"[{host_port}] BGP 总邻居数量: {bgp_info.get('bgp_total_peers')}")
                    logging.info(f"[{host_port}] BGP 建立状态的邻居数量: {bgp_info.get('bgp_established_peers')}")
                    if bgp_info.get("bgp_non_established_peers"):
                        logging.info(f"[{host_port}] BGP 未建立状态的邻居: {bgp_info.get('bgp_non_established_peers')}")
                    else:
                        logging.info(f"[{host_port}] 所有 BGP peers 均处于 Established 状态。")
                else:
                    logging.info(f"[{host_port}] BGP 未配置或无 peers.")
            else:
                logging.info(f"[{host_port}] BGP 未配置")

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
            "network_connections": self.network_connections
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
                        if network_id not in network_to_interfaces:
                            network_to_interfaces[network_id] = []
                        network_to_interfaces[network_id].append({
                            "node_name": node_name,
                            "interface_name": interface_name,
                            "type": interface_type
                        })
                        # Store node interface information
                        if node_name not in self.node_interfaces:
                            self.node_interfaces[node_name] = []
                        self.node_interfaces[node_name].append({
                            "name": interface_name,
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

def write_output(output_path: str, data: Dict[str, Any]):
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)  # Use ensure_ascii=False to support Chinese
        logging.info(f"Mapping results written to {output_path}")
    except IOError as e:
        logging.error(f"Error writing to output file: {e}")
        sys.exit(1)

def write_interface_status(data_txt_path: str, mapping: Dict[str, Any]):
    """
    Write interface status and OSPF/ISIS/BGP status to data.txt in the following format:
    Node: sysname1 (host:port)
        Interface: GigabitEthernet1/0接口配置状态: 已配置IP地址, OSPF已配置, ISIS未配置
        OSPF Status: OSPF 配置正常，邻居 Router IDs: 2.2.2.2, 1.1.1.1
        ISIS Status: ISIS 配置正常，邻居数量: 1
        BGP Local Router ID: 3.3.3.3
        BGP Local AS Number: 100
        BGP Total Peers: 3
        BGP Established Peers: 2
        BGP Non-Established Peers:
            Peer IP: x.x.x.x, AS: y, State: Z

    Node: sysname2 (host:port)
        Interface: GigabitEthernet1/0/2接口配置状态: 未配置IP地址, OSPF未配置, ISIS未配置
        OSPF Status: OSPF 未配置
        ISIS Status: ISIS 未配置
        BGP 未配置
    """
    try:
        with open(data_txt_path, 'w', encoding='utf-8') as f:
            telnet_devices = mapping.get("telnet_devices", {})
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

                # Write BGP information
                if bgp_info and bgp_info.get("bgp_local_router_id") != "未知":
                    f.write(f"    BGP 本地 Router ID: {bgp_info.get('bgp_local_router_id')}\n")
                    f.write(f"    BGP 本地 AS Number: {bgp_info.get('bgp_local_as_number')}\n")
                    f.write(f"    BGP 总邻居数量: {bgp_info.get('bgp_total_peers')}\n")
                    f.write(f"    BGP 建立状态的邻居数量: {bgp_info.get('bgp_established_peers')}\n")

                    bgp_non_established_peers = bgp_info.get("bgp_non_established_peers", [])
                    if bgp_non_established_peers:
                        f.write(f"    BGP 未建立状态的邻居:\n")
                        for peer in bgp_non_established_peers:
                            peer_ip = peer.get("peer_ip", "未知")
                            peer_as = peer.get("as_number", "未知")  # 修正键名
                            state = peer.get("state", "未知")
                            f.write(f"        Peer IP: {peer_ip}, AS: {peer_as}, State: {state}\n")
                    else:
                        f.write(f"    BGP 未建立状态的邻居: 无\n")
                else:
                    f.write(f"    BGP 未配置\n")

                f.write("\n")  # Add empty line between devices
        logging.info(f"接口状态已写入 {data_txt_path}")
    except IOError as e:
        logging.error(f"写入 {data_txt_path} 时出错: {e}")
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

    # Read UNL file based on labId
    lab_id = telnet_info.get("labId")
    if lab_id is not None:
        router_manager.read_unl_file(lab_id)
    else:
        logging.warning("labId not found in telnet_info.")

    router_manager.connect_and_get_sysnames_and_configs()
    mapping = router_manager.collect_results()

    logging.info("Collected router configurations:")
    logging.info(json.dumps(mapping, indent=4, ensure_ascii=False))
    write_output(output_path, mapping)

    # Define the path for data.txt, placed in the same directory as output_path
    output_dir = os.path.dirname(output_path)
    data_txt_path = os.path.join(output_dir, "data.txt")
    write_interface_status(data_txt_path, mapping)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process router configurations from param.json.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json, use {t} for latest folder number.")
    parser.add_argument("-o", "--output", required=True, help="Output path for process information, use {t} for latest folder number.")
    args = parser.parse_args()
    main(args.input, args.output)
