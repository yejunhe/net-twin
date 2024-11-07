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

# 配置日志记录
logging.basicConfig(
    level=logging.DEBUG,  # 设置为 DEBUG 级别以获得详细日志
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
        self.telnet_configurations: Dict[str, List[Dict[str, str]]] = {}  # 结构化的接口信息
        self.telnet_ospf_interfaces: Dict[str, List[str]] = {}  # 配置了 OSPF 的接口
        self.telnet_router_ids: Dict[str, str] = {}  # 每个设备的 Router ID
        self.telnet_ospf_peers: Dict[str, List[str]] = {}  # 每个设备的 OSPF 邻居 Router ID
        self.telnet_isis_interfaces: Dict[str, List[str]] = {}  # 配置了 ISIS 的接口
        self.telnet_isis_peers_count: Dict[str, int] = {}  # 每个设备的 ISIS 邻居数量
        self.telnet_bgp_info: Dict[str, Dict[str, Any]] = {}  # 每个设备的 BGP 信息
        self.network_connections: Optional[List[Dict[str, Any]]] = None
        self.node_interfaces: Dict[str, List[Dict[str, str]]] = {}  # 来自 UNL 的节点接口信息
        self.max_workers = max_workers
        self.telnet_lock = Lock()
        # 定义不同设备类型的命令序列
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
        # 初始化 NQA 相关属性
        self.nqa_metrics: Dict[str, Dict[str, Any]] = {}
        self.nqa_evaluations: Dict[str, Dict[str, str]] = {}
        self.nqa_summaries: Dict[str, str] = {}

    def execute_telnet_commands(self, tn: telnetlib.Telnet, commands: List[str], quit_cmd: bytes) -> Dict[str, str]:
        """
        执行一系列 Telnet 命令并返回其输出。
        
        :param tn: Telnet 连接对象。
        :param commands: 要执行的命令列表。
        :param quit_cmd: 退出 Telnet 会话的命令。
        :return: 字典，键为命令，值为对应的输出。
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
                time.sleep(2)  # 增加等待时间以确保命令输出完整
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
        # 根据部分 image_type 查找匹配的设备类型
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
                    # 解析 'display ip interface brief'
                    if 'display ip interface brief' in command_outputs:
                        parsed_interfaces = self.parse_display_ip_interface_brief(command_outputs['display ip interface brief'])
                        self.telnet_configurations[key] = parsed_interfaces
                    else:
                        logging.warning(f"[{tn.host}:{tn.port}] Missing 'display ip interface brief' output.")
                
                    # 解析 'display ospf interface'
                    if 'display ospf interface' in command_outputs:
                        ospf_interfaces = self.parse_display_ospf_interface(command_outputs['display ospf interface'])
                        self.telnet_ospf_interfaces[key] = ospf_interfaces
                    else:
                        logging.warning(f"[{tn.host}:{tn.port}] Missing 'display ospf interface' output.")
                
                    # 解析 'display ospf peer'
                    if 'display ospf peer' in command_outputs:
                        ospf_peer_output = command_outputs['display ospf peer']
                        router_id, neighbors = self.parse_display_ospf_peer(ospf_peer_output)
                        if router_id:
                            self.telnet_router_ids[key] = router_id
                        if neighbors is not None:
                            self.telnet_ospf_peers[key] = neighbors
                    else:
                        logging.warning(f"[{tn.host}:{tn.port}] Missing 'display ospf peer' output.")
                
                    # 解析 'display isis interface'
                    if 'display isis interface' in command_outputs:
                        isis_output = command_outputs['display isis interface']
                        isis_interfaces = self.parse_display_isis_interface(isis_output)
                        self.telnet_isis_interfaces[key] = isis_interfaces
                    else:
                        logging.warning(f"[{tn.host}:{tn.port}] Missing 'display isis interface' output.")
                
                    # 解析 'display isis peer'
                    if 'display isis peer' in command_outputs:
                        isis_peer_output = command_outputs['display isis peer']
                        isis_peers = self.parse_display_isis_peer(isis_peer_output)
                        if isis_peers is not None:
                            self.telnet_isis_peers_count[key] = isis_peers
                    else:
                        logging.warning(f"[{tn.host}:{tn.port}] Missing 'display isis peer' output.")
                
                    # 解析 'display bgp all summary'
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
        解析 'display ospf peer' 输出，提取当前节点的 Router ID 和邻居 Router IDs。
        
        :param output: 命令输出。
        :return: 包含 Router ID 和邻居 Router IDs 的元组。如果没有相关输出，则返回 (None, None)。
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
        解析 'display isis peer' 输出，提取 ISIS 邻居的总数。
        
        :param output: 命令输出。
        :return: 邻居数量。如果无法解析，则返回 None。
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
        解析 'display isis interface' 输出，提取配置了 ISIS 的接口名称。
        
        :param output: 命令输出。
        :return: 标准化后的接口名称列表。
        """
        interfaces = []
        lines = output.splitlines()
        logging.debug("Parsing 'display isis interface' output.")
        
        # 跳过表头，直到数据开始
        data_started = False
        for line in lines:
            if line.strip().startswith("Interface"):
                data_started = True
                logging.debug("Found 'Interface' header in 'display isis interface' output.")
                continue
            if not data_started:
                continue
            if not line.strip() or re.match(r'^[-=]+$', line):
                continue
            # 例子行:
            # Eth1/0/0          001         Up          Mtu:Dn/Lnk:Dn/IP:Dn 1497 L1/L2 No/No
            parts = line.split()
            if len(parts) < 1:
                continue
            iface = parts[0]
            # 使用 normalize_interface_name 处理接口名称
            iface_formatted = normalize_interface_name(iface)
            interfaces.append(iface_formatted)
            logging.debug(f"Detected ISIS-configured interface: {iface_formatted}")
        
        logging.debug(f"Parsed ISIS interfaces: {interfaces}")
        return interfaces

    def parse_display_bgp_all_summary(self, output: str) -> Optional[Dict[str, Any]]:
        """
        解析 'display bgp all summary' 输出，提取 BGP 信息。
        
        :param output: 命令输出。
        :return: 包含 BGP 信息的字典。如果解析失败，则返回 None。
        """
        bgp_info = {
            "bgp_local_router_id": None,
            "bgp_local_as_number": None,
            "bgp_total_peers": 0,
            "bgp_established_peers": 0,
            "bgp_non_established_peers": []
        }

        lines = output.splitlines()
        logging.debug("Parsing 'display bgp all summary' output.")

        # 定义正则表达式模式
        key_value_regex = re.compile(r'(\w+(?:\s+\w+)*)\s*:\s*(\d+)', re.IGNORECASE)
        # 定义邻居条目正则表达式
        peer_entry_regex = re.compile(
            r'^(?P<peer_ip>\S+)\s+'
            r'(?P<peer_as>\d+)\s+'
            r'(?P<msg_rcvd>\d+)\s+'
            r'(?P<msg_sent>\d+)\s+'
            r'(?P<out_q>\d+)\s+'
            r'(?P<up_down>\S+)\s+'
            r'(?P<state>\S+)', re.IGNORECASE
        )

        in_peer_table = False  # 标志，指示是否正在解析邻居表

        for line in lines:
            # 提取键值对
            key_value_matches = key_value_regex.findall(line)
            for key, value in key_value_matches:
                key = key.strip().lower()
                if key == 'bgp local router id':
                    bgp_info["bgp_local_router_id"] = value
                    logging.debug(f"Detected BGP local Router ID: {bgp_info['bgp_local_router_id']}")
                elif key == 'local as number':
                    bgp_info["bgp_local_as_number"] = value
                    logging.debug(f"Detected local AS number: {bgp_info['bgp_local_as_number']}")
                elif key == 'total number of peers':
                    bgp_info["bgp_total_peers"] = int(value)
                    logging.debug(f"Detected total BGP peers: {bgp_info['bgp_total_peers']}")
                elif key == 'peers in established state':
                    bgp_info["bgp_established_peers"] = int(value)
                    logging.debug(f"Detected established BGP peers: {bgp_info['bgp_established_peers']}")

            # 识别邻居表的开始
            if line.strip().startswith("Peer"):
                in_peer_table = True
                continue

            if in_peer_table:
                # 识别邻居表的结束
                if re.match(r'^[-=]+$', line.strip()):
                    in_peer_table = False
                    continue

                # 解析邻居条目
                match = peer_entry_regex.match(line.strip())
                if match:
                    state = match.group('state').lower()
                    if state != 'established':
                        bgp_info["bgp_non_established_peers"].append({
                            "peer_ip": match.group('peer_ip'),
                            "peer_as": match.group('peer_as'),
                            "state": match.group('state').capitalize()
                        })
                        logging.debug(f"Detected non-established BGP peer: IP={match.group('peer_ip')}, AS={match.group('peer_as')}, State={match.group('state').capitalize()}")
                else:
                    logging.debug(f"Unmatched BGP peer line: {line}")

        # 检查解析是否成功
        if bgp_info["bgp_local_router_id"] and bgp_info["bgp_local_as_number"]:
            logging.info(f"Extracted BGP info: {bgp_info}")
            return bgp_info
        else:
            logging.warning("Failed to extract some BGP information from 'display bgp all summary' output.")
            return None

    def parse_display_ip_interface_brief(self, output: str) -> List[Dict[str, str]]:
        """
        解析 'display ip interface brief' 输出，排除 IP 为 'unassigned' 的接口，并返回结构化数据。
        
        :param output: 命令输出。
        :return: 接口信息字典的列表。
        """
        lines = output.splitlines()
        interfaces = []
        header_found = False

        # 正则表达式匹配接口行
        interface_regex = re.compile(
            r'^\s*(?P<interface>\S+)\s+'
            r'(?P<ip_address>(?:\d{1,3}\.){3}\d{1,3}/\d{1,2}|unassigned)\s+'
            r'(?P<physical>up|down)\s+'
            r'(?P<protocol>up|down)\s+'
            r'(?P<vpn>\S+)'
        )

        for line in lines:
            # 查找表头
            if not header_found:
                if re.match(r'^Interface\s+IP Address/Mask\s+Physical\s+Protocol\s+VPN', line):
                    header_found = True
                    logging.debug("Found 'display ip interface brief' table header.")
                continue
            else:
                # 跳过空行或分隔线
                if not line.strip() or re.match(r'^[-=]+$', line):
                    continue

                match = interface_regex.match(line)
                if match:
                    ip_address = match.group('ip_address')
                    if ip_address.lower() != 'unassigned':
                        iface = match.group('interface')
                        # 使用 normalize_interface_name 处理接口名称
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
        解析 'display ospf interface' 输出，提取配置了 OSPF 的接口名称。
        
        :param output: 命令输出。
        :return: 标准化后的接口名称列表。
        """
        interfaces = []
        lines = output.splitlines()
        parsing = False  # 标志，指示是否开始解析

        # 正则表达式匹配接口行
        interface_regex = re.compile(r'^\s*(?P<interface>\S+)\s+[\d\.]+')

        for line in lines:
            if "Interfaces" in line:
                parsing = True
                logging.debug("Starting to parse OSPF interface information.")
                continue
            if parsing:
                # 跳过空行和分隔线
                if not line.strip() or re.match(r'^[-=]+$', line):
                    continue
                # 跳过以 "Area" 开头的行
                if line.strip().startswith("Area"):
                    continue

                # 匹配接口行
                match = interface_regex.match(line)
                if match:
                    iface = match.group('interface')
                    # 使用 normalize_interface_name 处理接口名称
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
        收集所有结果，执行接口匹配，并输出接口配置状态，包括 OSPF、ISIS、BGP 状态。
        """
        # 存储接口状态信息
        interface_status = {}
        ospf_status = {}
        isis_status = {}
        bgp_info_dict = {}
        nqa_info_dict = {}
        nqa_evaluation_dict = {}
        nqa_summary_dict = {}

        for host_port, sysname in self.telnet_sysnames.items():
            node_interfaces = self.node_interfaces.get(sysname, [])
            telnet_interfaces = self.telnet_configurations.get(host_port, [])
            ospf_interfaces = self.telnet_ospf_interfaces.get(host_port, [])
            isis_interfaces = self.telnet_isis_interfaces.get(host_port, [])
            isis_peers = self.telnet_isis_peers_count.get(host_port)
            router_id = self.telnet_router_ids.get(host_port)
            ospf_neighbors = self.telnet_ospf_peers.get(host_port)
            bgp_info = self.telnet_bgp_info.get(host_port)

            # 从 Telnet 提取的接口名称转换为小写以便比较
            telnet_interface_names = [iface['Interface'].lower() for iface in telnet_interfaces if isinstance(iface, dict)]
            ospf_interfaces_lower = [iface.lower() for iface in ospf_interfaces]
            isis_interfaces_lower = [iface.lower() for iface in isis_interfaces]

            logging.debug(f"[{host_port}] Telnet fetched interfaces: {telnet_interface_names}")
            logging.debug(f"[{host_port}] OSPF-configured interfaces: {ospf_interfaces_lower}")
            logging.debug(f"[{host_port}] ISIS-configured interfaces: {isis_interfaces_lower}")
            if bgp_info:
                logging.debug(f"[{host_port}] BGP info: {bgp_info}")

            interface_status[host_port] = []
            ospf_status[host_port] = ""
            isis_status[host_port] = ""
            bgp_info_dict[host_port] = {}
            nqa_info_dict[host_port] = {}
            nqa_evaluation_dict[host_port] = {}
            nqa_summary_dict[host_port] = ""

            for iface in node_interfaces:
                # 使用 normalize_interface_name 格式化接口名称
                iface_name = iface['name']
                iface_formatted = normalize_interface_name(iface_name)
                iface_formatted_lower = iface_formatted.lower()
                logging.debug(f"[{host_port}] Formatted interface name: {iface_formatted}")

                # 确定接口是否配置了 IP
                config_status = "已配置IP地址" if iface_formatted_lower in telnet_interface_names else "未配置IP地址"

                # 确定 OSPF 配置状态
                ospf_iface_status = "OSPF已配置" if iface_formatted_lower in ospf_interfaces_lower else "OSPF未配置"

                # 确定 ISIS 配置状态
                isis_iface_status = "ISIS已配置" if iface_formatted_lower in isis_interfaces_lower else "ISIS未配置"

                status = f"{iface_formatted}接口配置状态: {config_status}, {ospf_iface_status}, {isis_iface_status}"
                interface_status[host_port].append(status)
                logging.info(f"[{host_port}] {status}")

            # 根据 'display ospf peer' 和 OSPF 接口确定 OSPF 状态
            if host_port in self.telnet_router_ids:
                if self.telnet_ospf_peers.get(host_port):
                    neighbors_str = ', '.join(self.telnet_ospf_peers[host_port])
                    ospf_status[host_port] = f"OSPF 配置正常，邻居 Router IDs: {neighbors_str}"
                else:
                    ospf_status[host_port] = "OSPF 配置问题：未检测到邻居 Router ID" if ospf_interfaces else "OSPF 未配置"
            else:
                ospf_status[host_port] = "OSPF 配置问题：未检测到 Router ID" if ospf_interfaces else "OSPF 未配置"

            # 根据 'display isis peer' 和 ISIS 接口确定 ISIS 状态
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

                bgp_info_dict[host_port] = {
                    "bgp_local_router_id": bgp_local_router_id,
                    "bgp_local_as_number": bgp_local_as_number,
                    "bgp_total_peers": bgp_total_peers,
                    "bgp_established_peers": bgp_established_peers,
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

            # 记录 BGP 状态
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

            # 集成 NQA 结果和性能评估
            nqa_metrics = self.nqa_metrics.get(host_port, {})
            nqa_evaluation = self.nqa_evaluations.get(host_port, {})
            nqa_summary = self.nqa_summaries.get(host_port, "NQA 未执行")

            nqa_info_dict[host_port] = nqa_metrics
            nqa_evaluation_dict[host_port] = nqa_evaluation
            nqa_summary_dict[host_port] = nqa_summary

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
                        "bgp_info": bgp_info_dict.get(host_port, {}),
                        "nqa_metrics": nqa_info_dict.get(host_port, {}),  # 修改为 'nqa_metrics'
                        "nqa_evaluation": nqa_evaluation_dict.get(host_port, {}),
                        "nqa_summary": nqa_summary_dict.get(host_port, "NQA 未执行")
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
        解析 UNL 文件，提取网络连接和节点接口信息。
        """
        try:
            root = ET.fromstring(unl_content)
            connections = []

            # 首先，解析节点及其接口
            nodes = {}
            for node in root.findall(".//node"):
                node_id = node.get("id")
                node_name = node.get("name")
                nodes[node_id] = node_name

            # 构建 network_id 到接口列表的映射
            network_to_interfaces = {}
            for node in root.findall(".//node"):
                node_id = node.get("id")
                node_name = nodes.get(node_id)
                for interface in node.findall("interface"):
                    network_id = interface.get("network_id")
                    interface_name = interface.get("name")
                    interface_type = interface.get("type", "ethernet")  # 默认类型为 ethernet
                    if network_id and node_name:
                        normalized_iface_name = normalize_interface_name(interface_name)
                        if network_id not in network_to_interfaces:
                            network_to_interfaces[network_id] = []
                        network_to_interfaces[network_id].append({
                            "node_name": node_name,
                            "interface_name": normalized_iface_name,
                            "type": interface_type
                        })
                        # 存储节点接口信息
                        if node_name not in self.node_interfaces:
                            self.node_interfaces[node_name] = []
                        self.node_interfaces[node_name].append({
                            "name": normalized_iface_name,
                            "type": interface_type
                        })

            # 现在，对于每个 network_id，如果正好有两个接口，则创建一个连接
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

    # NQA 相关方法
    def get_sysname_and_routing_table(self, tn: telnetlib.Telnet) -> Tuple[Optional[str], Optional[str]]:
        """
        获取节点的 sysname 和 OSPF 路由表中的第一个 OSPF 路由 IP。
        """
        sysname = self.get_sysname_via_telnet(tn)
        ospf_ip = None
        if sysname:
            try:
                tn.write(b'scr 0 t\n')
                time.sleep(1)
                tn.read_until(b']', timeout=3)

                tn.write(b'display ip routing-table\n')
                routing_output = tn.read_until(b'>', timeout=5).decode('ascii', errors='ignore')
                ospf_ip = self.parse_routing_table(routing_output)
            except Exception as e:
                logging.error(f"[{tn.host}:{tn.port}] Error retrieving routing table: {e}")
        return sysname, ospf_ip

    def parse_routing_table(self, routing_table: str) -> Optional[str]:
        """
        解析路由表，找到第一次出现 OSPF 的目的地址，并去掉子网掩码。
        
        :param routing_table: 路由表输出。
        :return: OSPF 目的地址。如果未找到，则返回 None。
        """
        for line in routing_table.splitlines():
            if 'OSPF' in line:
                parts = line.split()
                if parts:
                    # 提取目的地址并去除子网掩码（如果有）
                    dest_ip = parts[0].split('/')[0]
                    logging.debug(f"Found OSPF route: {dest_ip}")
                    return dest_ip
        logging.info("No OSPF route found in routing table.")
        return None

    def perform_nqa_test(self, tn: telnetlib.Telnet, dest_ip: str, max_attempts: int = 5) -> Optional[Dict[str, Any]]:
        """
        配置并执行 NQA 测试，返回性能指标。
        
        :param tn: Telnet 连接对象。
        :param dest_ip: 目标 IP 地址进行 NQA 测试。
        :param max_attempts: 最大尝试次数以获取测试结果。
        :return: 包含性能指标的字典。如果失败，则返回 None。
        """
        try:
            # 进入 system-view 模式
            tn.write(b'system-view\n')
            time.sleep(1)
            tn.read_until(b']', timeout=3)

            # 配置 NQA 测试实例
            nqa_commands = [
                b'nqa test-instance admin perfor_test\n',
                b'test-type icmpjitter\n',
                f'destination-address ipv4 {dest_ip}\n'.encode('ascii'),
                b'probe-count 2\n',
                b'interval milliseconds 100\n',
                b'timeout 1\n'
            ]
            for cmd in nqa_commands:
                tn.write(cmd)
                time.sleep(1)
                tn.read_until(b']', timeout=3)

            # 发送命令开始测试
            tn.write(b'start now\n')
            time.sleep(1)
            tn.read_until(b']', timeout=3)

            # 确保配置提交
            tn.write(b'commit\n')
            time.sleep(1)
            tn.read_until(b']', timeout=3)

            # 尝试获取 NQA 测试结果
            attempt_count = 0
            result = ""
            while attempt_count < max_attempts:
                tn.write(b'display nqa results test-instance admin perfor_test\n')
                partial_output = tn.read_until(b'>', timeout=5).decode('ascii')
                result += partial_output

                if "The test is finished" in partial_output:
                    logging.info(f"NQA test finished for {dest_ip} on attempt {attempt_count + 1}")
                    break

                attempt_count += 1
                logging.debug(f"Attempt {attempt_count}/{max_attempts} for NQA result on {dest_ip}...")

                if attempt_count >= max_attempts:
                    logging.warning(f"Max attempts reached for {dest_ip}. Test result may be incomplete.")
                    break

            # 记录并返回结果
            logging.debug(f"NQA Test Result for {dest_ip}:\n{result}")

            # 执行结束和清理命令
            tn.write(b'stop\n')
            time.sleep(1)
            tn.read_until(b']', timeout=3)

            tn.write(b'q\n')
            time.sleep(1)
            tn.read_until(b'>', timeout=3)

            tn.write(b'undo nqa test-instance admin perfor_test\n')
            time.sleep(1)
            tn.read_until(b']', timeout=3)

            tn.write(b'commit\n')
            time.sleep(1)
            tn.read_until(b']', timeout=3)

            # 解析并返回性能评估输入
            metrics = self.parse_nqa_result(result)
            if metrics is None:
                logging.warning(f"Failed to parse NQA results for {dest_ip}.")
            return metrics

        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] Error during NQA test: {e}")
            return None

    def parse_nqa_result(self, nqa_result: str) -> Optional[Dict[str, Any]]:
        """
        解析 NQA 结果，提取性能指标。
        
        :param nqa_result: NQA 测试结果输出。
        :return: 包含性能指标的字典。如果解析失败，则返回 None。
        """
        metrics = {
            "latency": None,
            "jitter": None,
            "packet_loss": None
        }

        # 定义每个性能指标的正则表达式
        rtt_pattern = re.compile(r'Min/Max/Avg/Sum RTT:(\d+)/(\d+)/(\d+)/(\d+)')
        jitter_pattern = re.compile(r'Average of Jitter:\s*(\d+(\.\d+)?)')
        packet_loss_pattern = re.compile(r'Packet Loss Ratio:\s*(\d+(\.\d+)?)\s*%')

        for line in nqa_result.splitlines():
            line = line.strip()
            
            # 解析 RTT (延迟)
            rtt_match = rtt_pattern.search(line)
            if rtt_match:
                try:
                    latency_avg = float(rtt_match.group(3))  # 平均 RTT 是第3个捕获组
                    metrics["latency"] = latency_avg
                    logging.debug(f"Parsed latency (Avg RTT): {latency_avg} ms")
                except ValueError as e:
                    logging.error(f"Failed to parse latency from line: '{line}'. Error: {e}")
                    metrics["latency"] = None

            # 解析抖动 (Jitter)
            jitter_match = jitter_pattern.search(line)
            if jitter_match:
                try:
                    jitter_value = float(jitter_match.group(1))
                    metrics["jitter"] = jitter_value
                    logging.debug(f"Parsed jitter (Avg Jitter): {jitter_value} ms")
                except ValueError as e:
                    logging.error(f"Failed to parse jitter from line: '{line}'. Error: {e}")
                    metrics["jitter"] = None

            # 解析丢包率 (Packet Loss Ratio)
            packet_loss_match = packet_loss_pattern.search(line)
            if packet_loss_match:
                try:
                    packet_loss = float(packet_loss_match.group(1))
                    metrics["packet_loss"] = packet_loss
                    logging.debug(f"Parsed packet loss ratio: {packet_loss} %")
                except ValueError as e:
                    logging.error(f"Failed to parse packet loss ratio from line: '{line}'. Error: {e}")
                    metrics["packet_loss"] = None

        # 如果所有指标都无法解析，记录原始 NQA 结果以便调试
        if all(value is None for value in metrics.values()):
            logging.warning("All performance metrics are None. Raw NQA result:")
            logging.warning(nqa_result)

        return metrics

    def evaluate_network_performance(self, metrics: Dict[str, Any]) -> Dict[str, str]:
        """
        根据性能指标评估网络性能，并以中文表述结果。
        
        :param metrics: 包含性能指标的字典。
        :return: 包含评估结果的字典。
        """
        evaluation = {
            "latency": "未知",
            "jitter": "未知",
            "packet_loss": "未知",
            "overall_performance": "未知"
        }

        # 定义性能评估的阈值
        latency_thresholds = {"good": 100, "average": 200}
        jitter_thresholds = {"good": 50, "average": 100}
        packet_loss_thresholds = {"good": 1, "average": 5}

        # 评估延迟
        if metrics["latency"] is not None:
            if metrics["latency"] <= latency_thresholds["good"]:
                evaluation["latency"] = "良好"
            elif metrics["latency"] <= latency_thresholds["average"]:
                evaluation["latency"] = "中等"
            else:
                evaluation["latency"] = "差"

        # 评估抖动
        if metrics["jitter"] is not None:
            if metrics["jitter"] <= jitter_thresholds["good"]:
                evaluation["jitter"] = "良好"
            elif metrics["jitter"] <= jitter_thresholds["average"]:
                evaluation["jitter"] = "中等"
            else:
                evaluation["jitter"] = "差"

        # 评估丢包
        if metrics["packet_loss"] is not None:
            if metrics["packet_loss"] <= packet_loss_thresholds["good"]:
                evaluation["packet_loss"] = "良好"
            elif metrics["packet_loss"] <= packet_loss_thresholds["average"]:
                evaluation["packet_loss"] = "中等"
            else:
                evaluation["packet_loss"] = "差"

        # 综合评估
        metrics_values = [evaluation["latency"], evaluation["jitter"], evaluation["packet_loss"]]
        if all(v == "良好" for v in metrics_values):
            evaluation["overall_performance"] = "良好"
        elif any(v == "差" for v in metrics_values):
            evaluation["overall_performance"] = "差"
        elif any(v == "中等" for v in metrics_values):
            evaluation["overall_performance"] = "中等"

        return evaluation

    def generate_performance_summary(self, evaluation: Dict[str, str]) -> str:
        """
        根据性能评估结果生成综合的网络性能评价。
        
        :param evaluation: 包含评估结果的字典。
        :return: 综合评价的字符串。
        """
        summaries = []
        
        # 延迟评价
        if evaluation["latency"] == "良好":
            summaries.append("延迟良好")
        elif evaluation["latency"] == "中等":
            summaries.append("延迟中等")
        elif evaluation["latency"] == "差":
            summaries.append("延迟较高")
        else:
            summaries.append("延迟未知")
        
        # 抖动评价
        if evaluation["jitter"] == "良好":
            summaries.append("抖动小")
        elif evaluation["jitter"] == "中等":
            summaries.append("抖动中等")
        elif evaluation["jitter"] == "差":
            summaries.append("抖动较大")
        else:
            summaries.append("抖动未知")
        
        # 丢包率评价
        if evaluation["packet_loss"] == "良好":
            summaries.append("丢包率低")
        elif evaluation["packet_loss"] == "中等":
            summaries.append("丢包率中等")
        elif evaluation["packet_loss"] == "差":
            summaries.append("丢包率较高")
        else:
            summaries.append("丢包率未知")
        
        # 综合评估
        if evaluation["overall_performance"] == "良好":
            overall = "网络性能良好。"
        elif evaluation["overall_performance"] == "中等":
            overall = "网络性能中等。"
        elif evaluation["overall_performance"] == "差":
            overall = "网络性能较差。"
        else:
            overall = "网络性能未知。"
        
        # 组合所有评价
        summary = "该设备网络性能" + "，".join(summaries) + "。" + overall
        return summary

    def process_router(self, node) -> Dict[str, Any]:
        """
        处理单个路由器的连接和测试，返回结果字典。
        
        :param node: 路由器节点信息。
        :return: 包含路由器信息和测试结果的字典。
        """
        host = node.get("hostip")
        port = node.get("port")
        result_data = {
            "host": host,
            "port": port,
            "sysname": None,
            "router_id": None,
            "ospf_ip": None,
            "ospf_neighbors": [],
            "isis_interfaces": [],
            "isis_peers_count": None,
            "bgp_info": {},
            "interface_status": [],
            "ospf_status": "",
            "isis_status": "",
            "nqa_metrics": {},
            "nqa_evaluation": {},
            "nqa_summary": ""
        }

        if not host or not port:
            logging.warning(f"无效的节点配置: {node}")
            result_data["nqa_summary"] = "无效的节点配置。"
            return result_data

        try:
            tn = telnetlib.Telnet(host, port, timeout=10)
            tn.host, tn.port = host, port
            # 获取 sysname 和 OSPF IP
            sysname, ospf_ip = self.get_sysname_and_routing_table(tn)
            if sysname:
                result_data["sysname"] = sysname
                key = f"{host}:{port}"
                self.telnet_sysnames[key] = sysname

                if ospf_ip:
                    result_data["ospf_ip"] = ospf_ip
                    # 执行 NQA 测试
                    nqa_metrics = self.perform_nqa_test(tn, ospf_ip)
                    if nqa_metrics:
                        result_data["nqa_metrics"] = nqa_metrics
                        evaluation = self.evaluate_network_performance(nqa_metrics)
                        summary = self.generate_performance_summary(evaluation)
                        result_data["nqa_evaluation"] = evaluation
                        result_data["nqa_summary"] = summary
                        self.nqa_metrics[key] = nqa_metrics
                        self.nqa_evaluations[key] = evaluation
                        self.nqa_summaries[key] = summary
            else:
                logging.warning(f"[{host}:{port}] 无法检索 sysname。")
        except Exception as e:
            logging.error(f"Error processing {host}:{port} - {e}")
            result_data["nqa_summary"] = "处理过程中发生错误。"

        return result_data

    def connect_and_get_sysnames_routes_and_nqa(self) -> List[Dict[str, Any]]:
        """
        通过 Telnet 连接每个路由器，检索 sysname、OSPF 路由，执行 NQA 测试，并评估网络性能。
        
        :return: 包含所有路由器信息和测试结果的列表。
        """
        results = []
        nodes = self.telnet_info.get("node", [])
        if not nodes:
            logging.warning("没有找到任何节点配置。")
            return results

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # 提交所有路由器的处理任务
            future_to_node = {executor.submit(self.process_router, node): node for node in nodes}
            for future in as_completed(future_to_node):
                node = future_to_node[future]
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    logging.error(f"处理节点 {node} 时发生错误: {e}")
                    results.append({
                        "host": node.get("hostip"),
                        "port": node.get("port"),
                        "sysname": None,
                        "router_id": None,
                        "ospf_ip": None,
                        "ospf_neighbors": [],
                        "isis_interfaces": [],
                        "isis_peers_count": None,
                        "bgp_info": {},
                        "interface_status": [],
                        "ospf_status": "",
                        "isis_status": "",
                        "nqa_metrics": {},
                        "nqa_evaluation": {},
                        "nqa_summary": "处理过程中发生错误。"
                    })

        return results

    def collect_all_results(self) -> Dict[str, Any]:
        """
        收集所有结果，包括 Telnet 设备信息和网络连接。
        
        :return: 包含所有结果的字典。
        """
        telnet_results = self.collect_results()
        return {
            "telnet_devices": telnet_results.get("telnet_devices", {}),
            "network_connections": self.network_connections
        }

def find_latest_folder(base_path: str) -> str:
    try:
        all_folders = [f for f in os.listdir(base_path) if f.isdigit()]
        if not all_folders:
            raise ValueError("基路径中未找到编号文件夹。")
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
            json.dump(data, f, indent=4, ensure_ascii=False)  # 使用 ensure_ascii=False 以支持中文
        logging.info(f"Mapping results written to {output_path}")
    except IOError as e:
        logging.error(f"Error writing to output file: {e}")
        sys.exit(1)


def write_interface_status(data_txt_path: str, mapping: Dict[str, Any]):
    """
    将接口状态和 OSPF/ISIS/BGP/NQA 状态写入 data.txt，格式如下：
    节点: sysname1 (host:port)
        接口: Ethernet1/0/0接口配置状态: 已配置IP地址, OSPF已配置, ISIS未配置
        OSPF 状态: OSPF 配置正常，邻居 Router IDs: 2.2.2.2, 1.1.1.1
        ISIS 状态: ISIS 配置正常，邻居数量: 1
        BGP 本地 Router ID: 3.3.3.3
        BGP 本地 AS Number: 100
        BGP 总邻居数量: 3
        BGP 建立状态的邻居数量: 2
        BGP 未建立状态的邻居:
            Peer IP: x.x.x.x, AS: y, State: Z
        NQA 延迟: 50 ms
        NQA 抖动: 10 ms
        NQA 丢包率: 0 %
        NQA 性能评估:
            延迟: 良好
            抖动: 良好
            丢包率: 良好
        NQA 性能总结: 该设备网络性能延迟良好，抖动小，丢包率低。网络性能良好。

    节点: sysname2 (host:port)
        接口: Ethernet1/0/2接口配置状态: 未配置IP地址, OSPF未配置, ISIS未配置
        OSPF 状态: OSPF 未配置
        ISIS 状态: ISIS 未配置
        BGP 未配置
        NQA 未执行
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
                nqa_metrics = device_info.get("nqa_metrics", {})  # 保持 'nqa_metrics'
                nqa_evaluation = device_info.get("nqa_evaluation", {})
                nqa_summary = device_info.get("nqa_summary", "NQA 未执行")

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

                # 写入 NQA 信息
                if nqa_metrics:
                    latency = nqa_metrics.get("latency", "未知")
                    jitter = nqa_metrics.get("jitter", "未知")
                    packet_loss = nqa_metrics.get("packet_loss", "未知")
                    f.write(f"    NQA 延迟: {latency if latency != '未知' else '未知'} ms\n")
                    f.write(f"    NQA 抖动: {jitter if jitter != '未知' else '未知'} ms\n")
                    f.write(f"    NQA 丢包率: {packet_loss if packet_loss != '未知' else '未知'} %\n")
                    if nqa_evaluation:
                        f.write(f"    NQA 性能评估:\n")
                        f.write(f"        延迟: {nqa_evaluation.get('latency', '未知')}\n")
                        f.write(f"        抖动: {nqa_evaluation.get('jitter', '未知')}\n")
                        f.write(f"        丢包率: {nqa_evaluation.get('packet_loss', '未知')}\n")
                    f.write(f"    NQA 性能总结: {nqa_summary}\n")
                else:
                    f.write(f"    NQA 未执行\n")

                f.write("\n")  # 在设备之间添加空行
        logging.info(f"接口状态已写入 {data_txt_path}")
    except IOError as e:
        logging.error(f"写入 {data_txt_path} 时出错: {e}")
        sys.exit(1)




def main(input_path: str, output_path: str, max_threads: int = 20):
    base_path = "/uploadPath/reasoning"
    if "{t}" in input_path or "{t}" in output_path:
        latest_folder = find_latest_folder(base_path)
        input_path = input_path.replace("{t}", latest_folder)
        output_path = output_path.replace("{t}", latest_folder)
        logging.debug(f"Resolved input_path: {input_path}")
        logging.debug(f"Resolved output_path: {output_path}")

    telnet_info = load_telnet_info(input_path)
    router_manager = RouterManager(telnet_info, max_workers=max_threads)

    # 根据 labId 读取 UNL 文件
    lab_id = telnet_info.get("labId")
    if lab_id is not None:
        router_manager.read_unl_file(lab_id)
    else:
        logging.warning("labId not found in telnet_info.")

    # 通过 Telnet 连接并检索配置信息
    router_manager.connect_and_get_sysnames_and_configs()

    # 执行 NQA 测试并收集结果
    router_manager.connect_and_get_sysnames_routes_and_nqa()

    # 收集所有结果
    mapping = router_manager.collect_all_results()

    logging.info("Collected router configurations:")
    logging.info(json.dumps(mapping, indent=4, ensure_ascii=False))
    write_output(output_path, mapping)

    # 定义 data.txt 的路径，位于 output_path 相同的目录下
    output_dir = os.path.dirname(output_path)
    data_txt_path = os.path.join(output_dir, "data.txt")
    write_interface_status(data_txt_path, mapping)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="通过 Telnet 从路由器检索 sysname、OSPF 路由，并执行 NQA 测试以评估网络性能。")
    parser.add_argument("-i", "--input", required=True, help="param.json 的路径，使用 {t} 表示最新的文件夹编号。")
    parser.add_argument("-o", "--output", required=True, help="输出路径，用于存储处理信息，使用 {t} 表示最新的文件夹编号。")
    parser.add_argument("--max-threads", type=int, default=20, help="最大并发线程数（默认为 20）。")
    args = parser.parse_args()
    main(args.input, args.output, max_threads=args.max_threads)
