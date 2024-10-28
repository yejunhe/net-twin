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

    def execute_telnet_commands(self, tn: telnetlib.Telnet, commands: List[str], quit_cmd: bytes) -> str:
        try:
            tn.write(b'\n')
            time.sleep(1)
            output = tn.read_very_eager().decode('ascii', errors='ignore')
            logging.debug(f"[{tn.host}:{tn.port}] Initial Telnet output:\n{output}")

            for cmd in commands:
                tn.write(cmd.encode('ascii') + b'\n')
                logging.info(f"[{tn.host}:{tn.port}] Sending command: {cmd}")
                time.sleep(1)
                cmd_output = tn.read_very_eager().decode('ascii', errors='ignore')
                output += cmd_output
                logging.debug(f"[{tn.host}:{tn.port}] Output for '{cmd}':\n{cmd_output}")

            prompt = self.get_prompt(tn)
            if prompt and not (prompt.startswith('<') and prompt.endswith('>')):
                tn.write(quit_cmd)
                logging.info(f"[{tn.host}:{tn.port}] Sending quit command.")
                time.sleep(1)
                output += tn.read_very_eager().decode('ascii', errors='ignore')
            return output
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] Telnet Error: {e}")
            return ""

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

    def get_configuration_via_telnet(self, tn: telnetlib.Telnet, image_type: str) -> Optional[str]:
        # Find matching device type based on partial image_type
        matched_key = next((key for key in self.commands_map if key in image_type), None)
        if not matched_key:
            logging.warning(f"[{tn.host}:{tn.port}] Unsupported image_type '{image_type}'. Skipping.")
            return None

        commands, quit_cmd = self.commands_map[matched_key]
        output = self.execute_telnet_commands(tn, commands, quit_cmd)
        if output:
            sysname = self.get_sysname_via_telnet(tn)
            if sysname:
                key = f"{tn.host}:{tn.port}"
                with self.telnet_lock:
                    self.telnet_sysnames[key] = sysname
                    # 调用解析方法
                    parsed_interfaces = self.parse_display_ip_interface_brief(output)
                    self.telnet_configurations[key] = parsed_interfaces
                    # 解析 OSPF 接口
                    ospf_interfaces = self.parse_display_ospf_interface(output)
                    self.telnet_ospf_interfaces[key] = ospf_interfaces
        else:
            logging.warning(f"[{tn.host}:{tn.port}] No output received from Telnet commands.")
        return output

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
        收集所有结果，并进行接口匹配，输出接口配置状态，包括 OSPF 配置状态。
        """
        # 存储接口状态信息
        interface_status = {}

        for host_port, sysname in self.telnet_sysnames.items():
            node_interfaces = self.node_interfaces.get(sysname, [])
            telnet_interfaces = self.telnet_configurations.get(host_port, [])
            ospf_interfaces = self.telnet_ospf_interfaces.get(host_port, [])

            # 提取 Telnet 获取的接口名称列表
            telnet_interface_names = [iface['Interface'] for iface in telnet_interfaces if isinstance(iface, dict)]

            logging.debug(f"[{host_port}] Telnet 获取的接口列表: {telnet_interface_names}")
            logging.debug(f"[{host_port}] 配置了 OSPF 的接口列表: {ospf_interfaces}")

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

                logging.debug(f"[{host_port}] 格式化后的接口名称: {iface_formatted}")

                # 判断接口是否在 Telnet 获取的接口列表中
                if iface_formatted in telnet_interface_names:
                    config_status = "已配置IP地址"
                else:
                    config_status = "未配置IP地址"

                # 判断接口是否配置了 OSPF
                if iface_formatted in ospf_interfaces:
                    ospf_status = "OSPF已配置"
                else:
                    ospf_status = "OSPF未配置"

                status = f"{iface_formatted}接口配置状态: {config_status}, {ospf_status}"
                interface_status[host_port].append(status)
                logging.info(f"[{host_port}] {status}")

        return {
            "telnet_devices": {
                host_port: {
                    "sysname": sysname,
                    "interfaces": self.telnet_configurations.get(host_port, []),
                    "ospf_interfaces": self.telnet_ospf_interfaces.get(host_port, []),
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
            r'^(?P<interface>\S+)\s+'
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
                    # 保留表头
                    # interfaces.append(line)  # 如果不需要保留表头，可以注释掉
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
                else:
                    # 如果行不匹配预期格式，忽略或记录（可选）
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

        for line in lines:
            if "Interfaces" in line:
                parsing = True
                continue
            if parsing:
                # 跳过空行和分隔线
                if not line.strip() or re.match(r'^[-=]+$', line):
                    continue
                # 使用正则表达式匹配接口行
                match = re.match(r'^(?P<interface>\S+)\s+', line)
                if match:
                    iface = match.group('interface')
                    # 将接口名称格式化为 'Ethernet1/0/0'
                    if iface.lower().startswith('eth'):
                        iface_number = iface[3:]  # 去掉前缀 'Eth'
                        iface_formatted = f"Ethernet{iface_number}"
                        interfaces.append(iface_formatted)
                    elif iface.lower().startswith('loop'):
                        # 处理 Loop 接口，如 Loop0
                        iface_formatted = iface.capitalize()
                        interfaces.append(iface_formatted)
                    else:
                        # 其他类型接口按需处理
                        iface_formatted = iface.capitalize()
                        interfaces.append(iface_formatted)
        logging.debug(f"Parsed OSPF interfaces: {interfaces}")
        return interfaces


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
    节点: sysname1
        接口: Interface1 - 已配置
        接口: Interface2 - 未配置
    节点: sysname2
        接口: Interface3 - 已配置
        接口: Interface4 - 未配置
    """
    try:
        with open(data_txt_path, 'w', encoding='utf-8') as f:
            telnet_devices = mapping.get("telnet_devices", {})
            for host_port, device_info in telnet_devices.items():
                sysname = device_info.get("sysname", "未知节点")
                interface_status_list = device_info.get("interface_status", [])
                f.write(f"节点: {sysname} ({host_port})\n")
                for status in interface_status_list:
                    # 假设 status 格式为 "InterfaceName接口已配置" 或 "InterfaceName接口未配置"
                    f.write(f"    接口: {status}\n")
                f.write("\n")  # 添加空行以分隔不同节点
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

    # 定义 data.txt 的路径，放在与 output_path 相同的目录下
    output_dir = os.path.dirname(output_path)
    data_txt_path = os.path.join(output_dir, "data.txt")
    write_interface_status(data_txt_path, mapping)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process router configurations from param.json.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json, use {t} for latest folder number.")
    parser.add_argument("-o", "--output", required=True, help="Output path for process information, use {t} for latest folder number.")
    args = parser.parse_args()
    main(args.input, args.output)
