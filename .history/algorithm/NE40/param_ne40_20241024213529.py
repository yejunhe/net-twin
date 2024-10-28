import json
import telnetlib
import os
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import logging
from typing import Optional, Dict, Any, List
import xml.etree.ElementTree as ET
import re

# Configure logging for better traceability and control
logging.basicConfig(
    level=logging.DEBUG,  # 设置为 DEBUG 以获取更多调试信息
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

class RouterManager:
    def __init__(self, telnet_info: Dict[str, Any], max_workers: int = 20):
        self.telnet_info = telnet_info
        self.telnet_sysnames: Dict[str, str] = {}
        self.telnet_configurations: Dict[str, List[Dict[str, str]]] = {}  # 结构化接口信息
        self.telnet_ospf_interfaces: Dict[str, List[str]] = {}  # 存储配置了 OSPF 的接口
        self.telnet_ospf_peers: Dict[str, List[Dict[str, Any]]] = {}  # 存储 OSPF 邻居关系
        self.network_connections: Optional[List[Dict[str, Any]]] = None
        self.node_interfaces: Dict[str, List[Dict[str, str]]] = {}  # 存储UNL文件中的节点接口信息
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
        执行一系列 Telnet 命令，并返回每个命令的输出。

        :param tn: Telnet 连接对象。
        :param commands: 要执行的命令列表。
        :param quit_cmd: 退出命令。
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
                time.sleep(2)  # 增加等待时间以确保命令输出完全
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

    def standardize_interface_name(self, iface: str) -> str:
        """
        标准化接口名称，将 Eth 转换为 Ethernet。

        :param iface: 原始接口名称。
        :return: 标准化后的接口名称。
        """
        if iface.lower().startswith('eth'):
            return 'Ethernet' + iface[3:]
        elif iface.lower().startswith('loop'):
            return iface.capitalize()
        else:
            # 其他类型接口按需处理
            return iface.capitalize()

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
                    # 调用解析方法
                    if 'display ip interface brief' in command_outputs:
                        parsed_interfaces = self.parse_display_ip_interface_brief(command_outputs['display ip interface brief'])
                        self.telnet_configurations[key] = parsed_interfaces
                    else:
                        logging.warning(f"[{tn.host}:{tn.port}] Missing 'display ip interface brief' output.")
                    
                    if 'display ospf interface' in command_outputs:
                        ospf_interfaces = self.parse_display_ospf_interface(command_outputs['display ospf interface'])
                        self.telnet_ospf_interfaces[key] = ospf_interfaces
                    else:
                        logging.warning(f"[{tn.host}:{tn.port}] Missing 'display ospf interface' output.")
                    
                    if 'display ospf peer' in command_outputs:
                        ospf_peers = self.parse_display_ospf_peer(command_outputs['display ospf peer'])
                        self.telnet_ospf_peers[key] = ospf_peers
                    else:
                        logging.warning(f"[{tn.host}:{tn.port}] Missing 'display ospf peer' output.")
        else:
            logging.warning(f"[{tn.host}:{tn.port}] No output received from Telnet commands.")
        return command_outputs

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
        收集所有结果，并进行接口匹配，输出接口配置状态，包括 OSPF 配置状态和邻居关系。
        """
        # 存储接口状态信息
        interface_status = {}

        for host_port, sysname in self.telnet_sysnames.items():
            node_interfaces = self.node_interfaces.get(sysname, [])
            telnet_interfaces = self.telnet_configurations.get(host_port, [])
            ospf_interfaces = self.telnet_ospf_interfaces.get(host_port, [])
            ospf_peers = self.telnet_ospf_peers.get(host_port, [])

            # 提取 Telnet 获取的接口名称列表，并转换为统一的小写
            telnet_interface_names = [iface['Interface'].lower() for iface in telnet_interfaces if isinstance(iface, dict)]
            ospf_interfaces_lower = [iface.lower() for iface in ospf_interfaces]

            logging.debug(f"[{host_port}] Telnet 获取的接口列表: {telnet_interface_names}")
            logging.debug(f"[{host_port}] 配置了 OSPF 的接口列表: {ospf_interfaces_lower}")

            interface_status[host_port] = []

            for iface in node_interfaces:
                # 格式化接口名称，如 type="ethernet" name="e1/0/0" => "Ethernet1/0/0"
                iface_name = iface['name']
                if iface_name.lower().startswith('e'):
                    iface_number = iface_name[1:]  # 去掉前缀 'e'
                    iface_formatted = f"Ethernet{iface_number}"
                else:
                    # 如果接口名称不以 'e' 开头，按原样格式化
                    iface_formatted = f"{iface['type'].capitalize()}{iface['name']}"

                iface_formatted_lower = iface_formatted.lower()
                logging.debug(f"[{host_port}] 格式化后的接口名称: {iface_formatted}")

                # 判断接口是否在 Telnet 获取的接口列表中
                if iface_formatted_lower in telnet_interface_names:
                    config_status = "已配置"
                else:
                    config_status = "未配置"

                # 判断接口是否配置了 OSPF
                if iface_formatted_lower in ospf_interfaces_lower:
                    ospf_status = "OSPF已配置"
                else:
                    ospf_status = "OSPF未配置"

                # 获取该接口的邻居信息
                peers = [peer for peer in ospf_peers if peer['interface'].lower() == iface_formatted_lower]
                if peers:
                    neighbor_info = "; ".join([
                        f"Router ID: {peer.get('router_id', '未知')}, Address: {peer.get('address', '未知')}, "
                        f"State: {peer.get('state', '未知')}, Priority: {peer.get('priority', '未知')}"
                        for peer in peers
                    ])
                else:
                    neighbor_info = "无邻居"

                status = f"{iface_formatted}接口配置状态: {config_status}, {ospf_status}, 邻居: {neighbor_info}"
                interface_status[host_port].append(status)
                logging.info(f"[{host_port}] {status}")

        return {
            "telnet_devices": {
                host_port: {
                    "sysname": sysname,
                    "interfaces": self.telnet_configurations.get(host_port, []),
                    "ospf_interfaces": self.telnet_ospf_interfaces.get(host_port, []),
                    "ospf_peers": self.telnet_ospf_peers.get(host_port, []),
                    "interface_status": interface_status.get(host_port, [])
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
        解析 UNL 文件，提取网络连接信息和节点接口信息。
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
                    interface_type = interface.get("type", "ethernet")  # 默认类型为 ethernet
                    if network_id and node_name:
                        if network_id not in network_to_interfaces:
                            network_to_interfaces[network_id] = []
                        network_to_interfaces[network_id].append({
                            "node_name": node_name,
                            "interface_name": interface_name,
                            "type": interface_type
                        })
                        # 存储节点接口信息
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

    def parse_display_ip_interface_brief(self, output: str) -> List[Dict[str, str]]:
        """
        解析 'display ip interface brief' 命令的输出，去除 IP Address/Mask 为 'unassigned' 的接口，并以结构化的方式返回。

        :param output: 命令的输出内容。
        :return: 过滤后的接口信息列表，每个接口信息为字典。
        """
        lines = output.splitlines()
        interfaces = []
        header_found = False
        headers = []

        # 正则表达式匹配接口行
        interface_regex = re.compile(
            r'^\s*(?P<interface>\S+)\s+'
            r'(?P<ip_address>(?:\d{1,3}\.){3}\d{1,3}/\d{1,2}|unassigned)\s+'
            r'(?P<physical>up|down)\s+'
            r'(?P<protocol>up|down)\s+'
            r'(?P<vpn>\S+)'
        )

        for line in lines:
            # 寻找表头
            if not header_found:
                if re.match(r'^Interface\s+IP Address/Mask\s+Physical\s+Protocol\s+VPN', line):
                    header_found = True
                    headers = re.split(r'\s{2,}', line)
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
                        interface_info = {
                            'Interface': match.group('interface'),
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
        解析 'display ospf interface' 命令的输出，提取配置了 OSPF 的接口名称，并格式化为 'Ethernet1/0/0' 的形式。

        :param output: 命令的输出内容。
        :return: 配置了 OSPF 的接口名称列表。
        """
        interfaces = []
        lines = output.splitlines()
        parsing = False  # 标志位，标记是否开始解析接口信息

        # 正则表达式匹配接口行，允许前导空格
        interface_regex = re.compile(r'^\s*(?P<interface>\S+)\s+[\d\.]+')  # 修改后的正则表达式

        for line in lines:
            if "Interfaces" in line:
                parsing = True
                logging.debug("开始解析 OSPF 接口信息")
                continue
            if parsing:
                # 跳过空行和分隔线
                if not line.strip() or re.match(r'^[-=]+$', line):
                    continue
                # 使用正则表达式匹配接口行
                match = interface_regex.match(line)
                if match:
                    iface = match.group('interface')
                    iface_formatted = self.standardize_interface_name(iface)
                    logging.debug(f"找到接口: {iface} -> {iface_formatted}")
                    interfaces.append(iface_formatted)
                else:
                    logging.debug(f"未匹配接口行: {line}")
        logging.debug(f"解析到的 OSPF 接口: {interfaces}")
        return interfaces

    def parse_display_ospf_peer(self, output: str) -> List[Dict[str, Any]]:
        """
        解析 'display ospf peer' 命令的输出，提取接口的 OSPF 邻居关系。

        :param output: 命令的输出内容。
        :return: 接口的 OSPF 邻居关系列表，每个关系为字典。
        """
        ospf_peers = []
        lines = output.splitlines()
        parsing = False  # 标志位，标记是否开始解析邻居信息
        current_interface = None

        # 正则表达式匹配接口行
        interface_regex = re.compile(r'Area\s+[\d\.]+\s+interface\s+[\d\.]+\s+\((?P<interface>\S+)\)\'s neighbors', re.IGNORECASE)

        # 正则表达式匹配 Router ID 和 Address
        router_regex = re.compile(r'Router ID:\s+(?P<router_id>[\d\.]+)\s+Address:\s+(?P<address>[\d\.]+)', re.IGNORECASE)

        # 正则表达式匹配状态行
        state_regex = re.compile(r'State:\s+(?P<state>\S+)\s+Mode:\S+\s+Priority:\s+(?P<priority>\d+)', re.IGNORECASE)

        for line in lines:
            # 寻找接口的邻居信息开始
            match_interface = interface_regex.match(line)
            if match_interface:
                raw_interface = match_interface.group('interface')
                current_interface = self.standardize_interface_name(raw_interface)
                logging.debug(f"正在解析接口: {current_interface}")
                parsing = True
                continue

            if parsing:
                # 如果找到新的接口，停止当前接口的解析
                match_new_interface = interface_regex.match(line)
                if match_new_interface:
                    raw_interface = match_new_interface.group('interface')
                    current_interface = self.standardize_interface_name(raw_interface)
                    logging.debug(f"正在解析接口: {current_interface}")
                    parsing = True
                    continue

                # 跳过空行和分隔线
                if not line.strip() or re.match(r'^[-=]+$', line):
                    continue

                # 匹配 Router ID 和 Address
                match_router = router_regex.match(line)
                if match_router and current_interface:
                    router_id = match_router.group('router_id')
                    address = match_router.group('address')
                    ospf_peer = {
                        "interface": current_interface,
                        "router_id": router_id,
                        "address": address
                    }
                    logging.debug(f"找到邻居: {ospf_peer}")
                    ospf_peers.append(ospf_peer)
                    continue

                # 匹配状态行
                match_state = state_regex.match(line)
                if match_state and current_interface and ospf_peers:
                    state = match_state.group('state')
                    priority = match_state.group('priority')
                    ospf_peers[-1]['state'] = state
                    ospf_peers[-1]['priority'] = priority
                    logging.debug(f"更新邻居状态: {ospf_peers[-1]}")
                    continue

        logging.debug(f"解析到的 OSPF 邻居关系: {ospf_peers}")
        return ospf_peers

    def collect_results(self) -> Dict[str, Any]:
        """
        收集所有结果，并进行接口匹配，输出接口配置状态，包括 OSPF 配置状态和邻居关系。
        """
        # 存储接口状态信息
        interface_status = {}

        for host_port, sysname in self.telnet_sysnames.items():
            node_interfaces = self.node_interfaces.get(sysname, [])
            telnet_interfaces = self.telnet_configurations.get(host_port, [])
            ospf_interfaces = self.telnet_ospf_interfaces.get(host_port, [])
            ospf_peers = self.telnet_ospf_peers.get(host_port, [])

            # 提取 Telnet 获取的接口名称列表，并转换为统一的小写
            telnet_interface_names = [iface['Interface'].lower() for iface in telnet_interfaces if isinstance(iface, dict)]
            ospf_interfaces_lower = [iface.lower() for iface in ospf_interfaces]

            logging.debug(f"[{host_port}] Telnet 获取的接口列表: {telnet_interface_names}")
            logging.debug(f"[{host_port}] 配置了 OSPF 的接口列表: {ospf_interfaces_lower}")

            interface_status[host_port] = []

            for iface in node_interfaces:
                # 格式化接口名称，如 type="ethernet" name="e1/0/0" => "Ethernet1/0/0"
                iface_name = iface['name']
                if iface_name.lower().startswith('e'):
                    iface_number = iface_name[1:]  # 去掉前缀 'e'
                    iface_formatted = f"Ethernet{iface_number}"
                else:
                    # 如果接口名称不以 'e' 开头，按原样格式化
                    iface_formatted = f"{iface['type'].capitalize()}{iface['name']}"

                iface_formatted_lower = iface_formatted.lower()
                logging.debug(f"[{host_port}] 格式化后的接口名称: {iface_formatted}")

                # 判断接口是否在 Telnet 获取的接口列表中
                if iface_formatted_lower in telnet_interface_names:
                    config_status = "已配置"
                else:
                    config_status = "未配置"

                # 判断接口是否配置了 OSPF
                if iface_formatted_lower in ospf_interfaces_lower:
                    ospf_status = "OSPF已配置"
                else:
                    ospf_status = "OSPF未配置"

                # 获取该接口的邻居信息
                peers = [peer for peer in ospf_peers if peer['interface'].lower() == iface_formatted_lower]
                if peers:
                    neighbor_info = "; ".join([
                        f"Router ID: {peer.get('router_id', '未知')}, Address: {peer.get('address', '未知')}, "
                        f"State: {peer.get('state', '未知')}, Priority: {peer.get('priority', '未知')}"
                        for peer in peers
                    ])
                else:
                    neighbor_info = "无邻居"

                status = f"{iface_formatted}接口配置状态: {config_status}, {ospf_status}, 邻居: {neighbor_info}"
                interface_status[host_port].append(status)
                logging.info(f"[{host_port}] {status}")

        return {
            "telnet_devices": {
                host_port: {
                    "sysname": sysname,
                    "interfaces": self.telnet_configurations.get(host_port, []),
                    "ospf_interfaces": self.telnet_ospf_interfaces.get(host_port, []),
                    "ospf_peers": self.telnet_ospf_peers.get(host_port, []),
                    "interface_status": interface_status.get(host_port, [])
                }
                for host_port, sysname in self.telnet_sysnames.items()
            },
            "network_connections": self.network_connections
        }

    def write_output(self, output_path: str, data: Dict[str, Any]):
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)  # 使用 ensure_ascii=False 以支持中文
            logging.info(f"Mapping results written to {output_path}")
        except IOError as e:
            logging.error(f"Error writing to output file: {e}")
            sys.exit(1)

    def write_interface_status(self, data_txt_path: str, mapping: Dict[str, Any]):
        """
        将接口状态写入 data.txt 文件，格式为：
        节点: sysname1 (host:port)
            接口: Ethernet1/0/0接口配置状态: 已配置, OSPF已配置, 邻居: Router ID: 2.2.2.2, Address: 10.0.23.1, State: Full, Priority: 1
            接口: Ethernet1/0/1接口配置状态: 已配置, OSPF已配置, 邻居: 无邻居
            接口: Ethernet1/0/2接口配置状态: 已配置, OSPF已配置, 邻居: Router ID: 1.1.1.1, Address: 10.0.13.1, State: Full, Priority: 1
            接口: Loop0接口配置状态: 已配置, OSPF未配置, 邻居: 无邻居
        节点: sysname2 (host:port)
            接口: Ethernet1/0/3接口配置状态: 已配置, OSPF未配置, 邻居: 无邻居
        """
        try:
            with open(data_txt_path, 'w', encoding='utf-8') as f:
                telnet_devices = mapping.get("telnet_devices", {})
                for host_port, device_info in telnet_devices.items():
                    sysname = device_info.get("sysname", "未知节点")
                    interface_status_list = device_info.get("interface_status", [])
                    f.write(f"节点: {sysname} ({host_port})\n")
                    for status in interface_status_list:
                        f.write(f"    接口: {status}\n")
                    f.write("\n")  # 添加空行以分隔不同节点
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
    将接口状态写入 data.txt 文件，格式为：
    节点: sysname1 (host:port)
        接口: Ethernet1/0/0接口配置状态: 已配置, OSPF已配置, 邻居: Router ID: 2.2.2.2, Address: 10.0.23.1, State: Full, Priority: 1
        接口: Ethernet1/0/1接口配置状态: 已配置, OSPF已配置, 邻居: 无邻居
        接口: Ethernet1/0/2接口配置状态: 已配置, OSPF已配置, 邻居: Router ID: 1.1.1.1, Address: 10.0.13.1, State: Full, Priority: 1
        接口: Loop0接口配置状态: 已配置, OSPF未配置, 邻居: 无邻居
    节点: sysname2 (host:port)
        接口: Ethernet1/0/3接口配置状态: 已配置, OSPF未配置, 邻居: 无邻居
    """
    try:
        with open(data_txt_path, 'w', encoding='utf-8') as f:
            telnet_devices = mapping.get("telnet_devices", {})
            for host_port, device_info in telnet_devices.items():
                sysname = device_info.get("sysname", "未知节点")
                interface_status_list = device_info.get("interface_status", [])
                f.write(f"节点: {sysname} ({host_port})\n")
                for status in interface_status_list:
                    f.write(f"    接口: {status}\n")
                f.write("\n")  # 添加空行以分隔不同节点
        logging.info(f"接口状态已写入 {data_txt_path}")
    except IOError as e:
        logging.error(f"写入 {data_txt_path} 时出错: {e}")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Process router configurations from param.json.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json, use {t} for latest folder number.")
    parser.add_argument("-o", "--output", required=True, help="Output path for process information, use {t} for latest folder number.")
    args = parser.parse_args()

    base_path = "/uploadPath/reasoning"
    input_path = args.input
    output_path = args.output

    if "{t}" in input_path or "{t}" in output_path:
        latest_folder = find_latest_folder(base_path)
        input_path = input_path.replace("{t}", latest_folder)
        output_path = output_path.replace("{t}", latest_folder)
        logging.debug(f"Resolved input_path: {input_path}")
        logging.debug(f"Resolved output_path: {output_path}")

    telnet_info = load_telnet_info(input_path)
    router_manager = RouterManager(telnet_info)
    router_manager.read_unl_file(telnet_info.get("labId", 0))
    router_manager.connect_and_get_sysnames_and_configs()
    mapping = router_manager.collect_results()

    logging.info("Collected router configurations:")
    logging.info(json.dumps(mapping, indent=4, ensure_ascii=False))
    write_output(output_path, mapping)

    # 定义 data.txt 的路径，放在与 output_path 相同的目录下
    output_dir = os.path.dirname(output_path)
    data_txt_path = os.path.join(output_dir, "data.txt")
    write_interface_status(data_txt_path, mapping)

if __name__ == "__main__":
    main()
