import json
import telnetlib
import os
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import logging
from logging.handlers import RotatingFileHandler
from typing import Optional, Dict, Any, List
import xml.etree.ElementTree as ET
import re
from datetime import datetime

# 配置统一的日志记录
def setup_logging():
    """配置日志记录"""
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)  # 设置为DEBUG以捕捉所有级别的日志

    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

    # 文件处理器，限制日志文件大小为5MB，保留5个备份
    try:
        file_handler = RotatingFileHandler("combined_script.log", maxBytes=5*1024*1024, backupCount=5)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except Exception as e:
        print(f"无法创建日志文件处理器。错误信息: {e}")
        sys.exit(1)

    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

# 调用日志配置
setup_logging()

class RouterManager:
    def __init__(self, telnet_info: Dict[str, Any], max_workers: int = 20):
        self.telnet_info = telnet_info
        self.telnet_sysnames: Dict[str, str] = {}
        self.telnet_configurations: Dict[str, List[Dict[str, str]]] = {}  # 结构化的接口信息
        self.network_connections: Optional[List[Dict[str, Any]]] = None
        self.node_interfaces: Dict[str, List[Dict[str, str]]] = {}  # 来自UNL的节点接口信息
        self.max_workers = max_workers
        self.telnet_lock = Lock()
        # 定义不同设备类型的命令序列
        self.commands_map = {
            "huaweine40": (
                [
                    'scr 0 t',
                    'display ip interface brief'
                ],
                b'q\n'
            )
        }

    def execute_telnet_commands(self, tn: telnetlib.Telnet, commands: List[str], quit_cmd: bytes) -> Dict[str, str]:
        """
        执行一系列Telnet命令并返回其输出。

        :param tn: Telnet连接对象。
        :param commands: 要执行的命令列表。
        :param quit_cmd: 退出Telnet会话的命令。
        :return: 命令及其输出的字典。
        """
        try:
            tn.write(b'\n')
            time.sleep(1)
            initial_output = tn.read_very_eager().decode('ascii', errors='ignore')
            logging.debug(f"[{tn.host}:{tn.port}] 初始Telnet输出:\n{initial_output}")

            command_outputs = {}

            for cmd in commands:
                tn.write(cmd.encode('ascii') + b'\n')
                logging.info(f"[{tn.host}:{tn.port}] 发送命令: {cmd}")
                time.sleep(2)  # 增加等待时间以确保命令输出完整
                cmd_output = tn.read_very_eager().decode('ascii', errors='ignore')
                command_outputs[cmd] = cmd_output
                logging.debug(f"[{tn.host}:{tn.port}] 命令 '{cmd}' 的输出:\n{cmd_output}")

            prompt = self.get_prompt(tn)
            if prompt and not (prompt.startswith('<') and prompt.endswith('>')):
                tn.write(quit_cmd)
                logging.info(f"[{tn.host}:{tn.port}] 发送退出命令。")
                time.sleep(1)
                cmd_output = tn.read_very_eager().decode('ascii', errors='ignore')
                command_outputs['quit'] = cmd_output

            return command_outputs
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] Telnet错误: {e}")
            return {}

    def get_prompt(self, tn: telnetlib.Telnet) -> Optional[str]:
        try:
            time.sleep(1)
            output = tn.read_very_eager().decode('ascii', errors='ignore')
            lines = output.splitlines()
            prompt = lines[-1].strip() if lines else None
            logging.debug(f"[{tn.host}:{tn.port}] 检测到的提示符: {prompt}")
            return prompt
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] 获取提示符时出错: {e}")
            return None

    def get_sysname_via_telnet(self, tn: telnetlib.Telnet) -> Optional[str]:
        try:
            tn.write(b'\n')
            time.sleep(1)
            output = tn.read_very_eager().decode('ascii', errors='ignore')
            for line in output.splitlines():
                if line.startswith('<') and line.endswith('>'):
                    sysname = line.strip('<> ').strip()
                    logging.info(f"[{tn.host}:{tn.port}] 检测到的sysname: {sysname}")
                    return sysname
            logging.warning(f"[{tn.host}:{tn.port}] 未检测到sysname。")
            return None
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] 获取sysname时的Telnet错误: {e}")
            return None

    def get_configuration_via_telnet(self, tn: telnetlib.Telnet, image_type: str) -> Optional[Dict[str, str]]:
        # 根据部分image_type找到匹配的设备类型
        matched_key = next((key for key in self.commands_map if key in image_type), None)
        if not matched_key:
            logging.warning(f"[{tn.host}:{tn.port}] 不支持的image_type '{image_type}'。跳过。")
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
                        logging.warning(f"[{tn.host}:{tn.port}] 缺少 'display ip interface brief' 输出。")
        else:
            logging.warning(f"[{tn.host}:{tn.port}] 未收到Telnet命令的输出。")
        return command_outputs

    def parse_display_ip_interface_brief(self, output: str) -> List[Dict[str, str]]:
        """
        解析 'display ip interface brief' 的输出，排除IP未分配的接口，并返回结构化数据。

        :param output: 命令输出。
        :return: 接口信息字典列表。
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
            # 查找表头
            if not header_found:
                if re.match(r'^Interface\s+IP Address/Mask\s+Physical\s+Protocol\s+VPN', line):
                    header_found = True
                    headers = re.split(r'\s{2,}', line)
                    logging.debug("找到 'display ip interface brief' 表头。")
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
                        logging.debug(f"解析的接口: {interface_info}")
                else:
                    logging.debug(f"未匹配的行: {line}")
                    continue

        logging.debug(f"解析的Telnet接口: {interfaces}")
        return interfaces

    def connect_and_get_sysnames_and_configs(self):
        nodes = self.telnet_info.get("node", [])
        if not nodes:
            logging.warning("telnet_info中未找到任何节点。")
            return

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_node = {}
            for node in nodes:
                image_type = node.get("image_type", "").lower()
                if "huaweine40" in image_type:
                    host, port = node.get("hostip"), node.get("port")
                    if not host or not port:
                        logging.warning(f"节点的hostip或port缺失，image_type: '{image_type}'。跳过。")
                        continue
                    try:
                        tn = telnetlib.Telnet(host, port, timeout=10)
                        tn.host, tn.port = host, port
                        future = executor.submit(self.get_configuration_via_telnet, tn, image_type)
                        future_to_node[future] = node
                    except Exception as e:
                        logging.error(f"无法通过Telnet连接到 {host}:{port}。错误信息: {e}")

            for future in as_completed(future_to_node):
                node = future_to_node[future]
                host, port = node.get("hostip"), node.get("port")
                config = future.result()
                msg = "成功" if config else "失败"
                logging.info(f"[{host}:{port}] 配置检索{msg}。")

    def collect_results(self) -> Dict[str, Any]:
        """
        收集所有结果并输出接口配置状态。
        """
        # 存储接口状态信息
        interface_status = {}

        for host_port, sysname in self.telnet_sysnames.items():
            node_interfaces = self.node_interfaces.get(sysname, [])
            telnet_interfaces = self.telnet_configurations.get(host_port, [])

            # 从Telnet提取的接口名称并转为小写以便比较
            telnet_interface_names = [iface['Interface'].lower() for iface in telnet_interfaces if isinstance(iface, dict)]

            logging.debug(f"[{host_port}] Telnet获取的接口: {telnet_interface_names}")

            interface_status[host_port] = ""

            for iface in node_interfaces:
                # 格式化接口名称，例如 type="ethernet" name="e1/0/0" => "Ethernet1/0/0"
                iface_name = iface['name']
                if iface_name.lower().startswith('e'):
                    iface_number = iface_name[1:]  # 移除 'e' 前缀
                    iface_formatted = f"Ethernet{iface_number}"
                else:
                    # 如果接口名称不以 'e' 开头，按类型格式化
                    iface_formatted = f"{iface['type'].capitalize()}{iface['name']}"

                iface_formatted_lower = iface_formatted.lower()
                logging.debug(f"[{host_port}] 格式化的接口名称: {iface_formatted}")

                # 确定接口是否配置了IP
                if iface_formatted_lower in telnet_interface_names:
                    config_status = "已配置IP地址"
                else:
                    config_status = "拓扑节点接口连接其他节点但未配置IP地址"

                status = f"{iface_formatted}接口配置状态: {config_status}"
                interface_status[host_port] += f"    接口: {status}\n"
                logging.info(f"[{host_port}] {status}")

        return {
            "telnet_devices": {
                host_port: {
                    "sysname": sysname,
                    "interfaces": self.telnet_configurations.get(host_port, []),
                    "interface_status": interface_status.get(host_port, "")
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
            logging.info(f"成功读取UNL文件: {unl_file_path}")
            self.parse_unl_file(unl_content)
        except FileNotFoundError:
            logging.error(f"未找到UNL文件路径: {unl_file_path}")
        except IOError as e:
            logging.error(f"读取UNL文件时出错: {e}")

    def parse_unl_file(self, unl_content: str):
        """
        解析UNL文件以提取网络连接和节点接口信息。
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

            # 构建从network_id到(node_name, interface_name, type)的映射
            network_to_interfaces = {}
            for node in root.findall(".//node"):
                node_id = node.get("id")
                node_name = nodes.get(node_id)
                for interface in node.findall("interface"):
                    network_id = interface.get("network_id")
                    interface_name = interface.get("name")
                    interface_type = interface.get("type", "ethernet")  # 默认类型为ethernet
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

            # 对于每个network_id，如果有且仅有两个接口，则创建连接
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
                    logging.info(f"通过 network_id {network_id} 连接 {connection['node1']}:{connection['interface1']} <-> {connection['node2']}:{connection['interface2']}")
                else:
                    logging.warning(f"network_id {network_id} 的接口数量不为2。跳过连接。")

            self.network_connections = connections
            logging.info("成功将UNL文件解析为网络连接。")
        except ET.ParseError as e:
            logging.error(f"解析UNL文件时出错: {e}")

class ArgumentParserCustom:
    """解析命令行参数"""

    def __init__(self):
        self.parser = argparse.ArgumentParser(description='路由器管理和拓扑变更检测脚本')
        self.parser.add_argument('-i', '--input', required=True, help='param.json文件的输入路径，使用 {t} 代表最新文件夹编号。')
        self.parser.add_argument('-o', '--output', required=True, help='输出路径，使用 {t} 代表最新文件夹编号。')
        self.args = None

    def parse(self):
        self.args = self.parser.parse_args()
        return self.args

class ParamReader:
    """读取和解析param.json文件"""

    def __init__(self, param_path):
        self.param_path = param_path
        self.param = None

    def read_param(self):
        logging.info(f"读取参数文件: {self.param_path}")
        if not os.path.exists(self.param_path):
            logging.error(f"参数文件 {self.param_path} 不存在。")
            raise FileNotFoundError(f"参数文件 {self.param_path} 不存在。")
        with open(self.param_path, 'r', encoding='utf-8') as f:
            try:
                self.param = json.load(f)
                logging.info("参数文件读取成功。")
                return self.param
            except json.JSONDecodeError as e:
                logging.error(f"无法解析JSON文件 {self.param_path}。错误信息: {e}")
                raise ValueError(f"无法解析JSON文件 {self.param_path}。错误信息: {e}")

class UnlParser:
    """查找并解析.unl文件，将其转换为JSON格式"""

    LABS_DIR = '/opt/unetlab/labs'

    def __init__(self, lab_id):
        self.lab_id = lab_id
        self.unl_path = self.find_unl_file()
        self.lab_json = None

    def find_unl_file(self):
        unl_filename = f"{self.lab_id}.unl"
        unl_path = os.path.join(self.LABS_DIR, unl_filename)
        logging.info(f"查找.unl文件: {unl_path}")
        if not os.path.exists(unl_path):
            logging.error(f"未找到对应的.unl文件: {unl_path}")
            raise FileNotFoundError(f"未找到对应的.unl文件: {unl_path}")
        logging.info(f"找到.unl文件: {unl_path}")
        return unl_path

    def parse_unl_to_json(self):
        try:
            logging.info(f"解析.unl文件: {self.unl_path}")
            tree = ET.parse(self.unl_path)
            root = tree.getroot()
            lab_json = {}
            lab_json['lab'] = {
                'name': root.attrib.get('name'),
                'id': root.attrib.get('id'),
                'version': root.attrib.get('version'),
                'scripttimeout': root.attrib.get('scripttimeout'),
                'lock': root.attrib.get('lock')
            }

            # 解析拓扑
            topology = root.find('topology')
            if topology is not None:
                # 解析节点
                nodes = []
                nodes_elem = topology.find('nodes')
                if nodes_elem is not None:
                    for node in nodes_elem.findall('node'):
                        node_dict = node.attrib.copy()
                        interfaces = []
                        for interface in node.findall('interface'):
                            interfaces.append(interface.attrib.copy())
                        node_dict['interfaces'] = interfaces
                        nodes.append(node_dict)
                lab_json['topology'] = {'nodes': nodes}

                # 解析网络
                networks = []
                networks_elem = topology.find('networks')
                if networks_elem is not None:
                    for network in networks_elem.findall('network'):
                        networks.append(network.attrib.copy())
                lab_json['topology']['networks'] = networks

            self.lab_json = lab_json
            logging.info("解析.unl文件成功。")
            return lab_json
        except ET.ParseError as e:
            logging.error(f"无法解析XML文件 {self.unl_path}。错误信息: {e}")
            raise ValueError(f"无法解析XML文件 {self.unl_path}。错误信息: {e}")

class HistoryManager:
    """管理历史记录，包括加载和保存历史JSON文件"""

    HISTORY_DIR = '/opt/unetlab/labs_history'

    def __init__(self, lab_id):
        self.lab_id = lab_id
        self.history_path = self.get_history_path()

    def get_history_path(self):
        try:
            if not os.path.exists(self.HISTORY_DIR):
                logging.info(f"历史记录目录不存在，正在创建: {self.HISTORY_DIR}")
                os.makedirs(self.HISTORY_DIR, exist_ok=True)
                logging.info(f"历史记录目录创建成功: {self.HISTORY_DIR}")
            else:
                logging.info(f"历史记录目录已存在: {self.HISTORY_DIR}")
        except Exception as e:
            logging.error(f"无法创建历史记录目录 {self.HISTORY_DIR}。错误信息: {e}")
            raise IOError(f"无法创建历史记录目录 {self.HISTORY_DIR}。错误信息: {e}")
        return os.path.join(self.HISTORY_DIR, f"{self.lab_id}.json")

    def load_history(self):
        if not os.path.exists(self.history_path):
            logging.info(f"历史记录文件不存在: {self.history_path}")
            return None
        logging.info(f"加载历史记录文件: {self.history_path}")
        with open(self.history_path, 'r', encoding='utf-8') as f:
            try:
                history = json.load(f)
                logging.info("历史记录文件加载成功。")
                return history
            except json.JSONDecodeError as e:
                logging.warning(f"无法解析历史JSON文件 {self.history_path}。错误信息: {e}")
                return None

    def save_history(self, data):
        try:
            with open(self.history_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            logging.info(f"历史记录已保存到 {self.history_path}")
        except Exception as e:
            logging.error(f"无法保存历史记录到 {self.history_path}。错误信息: {e}")
            raise IOError(f"无法保存历史记录到 {self.history_path}。错误信息: {e}")

class TopologyComparator:
    """比较当前JSON与历史JSON，检测拓扑变化"""

    IGNORED_FIELDS = {'left', 'top', 'uuid'}

    def __init__(self, current, history):
        self.current = current
        self.history = history
        self.differences = {
            'added_nodes': [],
            'removed_nodes': [],
            'modified_nodes': [],
            'added_networks': [],
            'removed_networks': [],
            'modified_networks': []
        }

    def compare(self):
        if self.history is None:
            logging.info("无历史记录可供比较。")
            return None  # 无历史记录

        # 比较节点
        current_nodes = {node['id']: node for node in self.current['topology'].get('nodes', [])}
        history_nodes = {node['id']: node for node in self.history['topology'].get('nodes', [])}

        # 检测新增节点
        for node_id in current_nodes:
            if node_id not in history_nodes:
                self.differences['added_nodes'].append(current_nodes[node_id])

        # 检测删除节点
        for node_id in history_nodes:
            if node_id not in current_nodes:
                self.differences['removed_nodes'].append(history_nodes[node_id])

        # 检测修改节点
        for node_id in current_nodes:
            if node_id in history_nodes:
                filtered_current = self.filter_ignored_fields(current_nodes[node_id])
                filtered_history = self.filter_ignored_fields(history_nodes[node_id])
                if filtered_current != filtered_history:
                    changes = self.find_changes(filtered_history, filtered_current)
                    self.differences['modified_nodes'].append({
                        'id': node_id,
                        'changes': changes
                    })

        # 比较网络
        current_networks = {network['id']: network for network in self.current['topology'].get('networks', [])}
        history_networks = {network['id']: network for network in self.history['topology'].get('networks', [])}

        # 检测新增网络
        for network_id in current_networks:
            if network_id not in history_networks:
                self.differences['added_networks'].append(current_networks[network_id])

        # 检测删除网络
        for network_id in history_networks:
            if network_id not in current_networks:
                self.differences['removed_networks'].append(history_networks[network_id])

        # 检测修改网络
        for network_id in current_networks:
            if network_id in history_networks:
                filtered_current = self.filter_ignored_fields(current_networks[network_id])
                filtered_history = self.filter_ignored_fields(history_networks[network_id])
                if filtered_current != filtered_history:
                    changes = self.find_changes(filtered_history, filtered_current)
                    self.differences['modified_networks'].append({
                        'id': network_id,
                        'changes': changes
                    })

        # 移除空的类别
        self.differences = {k: v for k, v in self.differences.items() if v}
        return self.differences

    def filter_ignored_fields(self, item):
        """返回一个新的字典，移除被忽略的字段"""
        return {k: v for k, v in item.items() if k not in self.IGNORED_FIELDS}

    def find_changes(self, old, new):
        """找到两个字典之间的不同，返回变化的字段和其旧值、新值"""
        changes = {}
        for key in new:
            if key not in old:
                changes[key] = {'old': None, 'new': new[key]}
            elif new[key] != old[key]:
                changes[key] = {'old': old[key], 'new': new[key]}
        for key in old:
            if key not in new:
                changes[key] = {'old': old[key], 'new': None}
        return changes

class OutputWriter:
    """将检测到的变化内容输出到指定路径"""

    def __init__(self, output_path):
        self.output_path = output_path

    def format_diff(self, differences, execution_time, execution_result):
        report_lines = []
        report_lines.append("=" * 50)
        report_lines.append("拓扑变更检测报告")
        report_lines.append(f"执行时间：{execution_time.strftime('%Y-%m-%d %H:%M:%S')}")
        report_lines.append(f"执行结果：{execution_result}")
        report_lines.append("变更内容：")
        report_lines.append("-" * 50)

        if differences is None:
            report_lines.append("首次运行，无历史记录进行比较。")
        elif not differences:
            report_lines.append("无拓扑变化。")
        else:
            # 新增节点
            if 'added_nodes' in differences:
                report_lines.append("新增节点:")
                for node in differences['added_nodes']:
                    report_lines.append(f" - ID: {node['id']}, Name: {node.get('name', 'N/A')}, Image: {node.get('image', 'N/A')}")
            # 删除节点
            if 'removed_nodes' in differences:
                report_lines.append("删除节点:")
                for node in differences['removed_nodes']:
                    report_lines.append(f" - ID: {node['id']}, Name: {node.get('name', 'N/A')}, Image: {node.get('image', 'N/A')}")
            # 修改节点
            if 'modified_nodes' in differences:
                report_lines.append("修改节点:")
                for node_change in differences['modified_nodes']:
                    node_id = node_change['id']
                    changes = node_change['changes']
                    report_lines.append(f" - ID: {node_id}")
                    for key, change in changes.items():
                        if key == 'interfaces':
                            added_interfaces = self.get_added_interfaces(change['old'], change['new'])
                            removed_interfaces = self.get_removed_interfaces(change['old'], change['new'])
                            modified_interfaces = self.get_modified_interfaces(change['old'], change['new'])
                            if added_interfaces:
                                report_lines.append(f"   * 接口新增:")
                                for iface in added_interfaces:
                                    report_lines.append(f"     - ID: {iface['id']}, Name: {iface.get('name', 'N/A')}, Type: {iface.get('type', 'N/A')}, Network ID: {iface.get('network_id', 'N/A')}")
                            if removed_interfaces:
                                report_lines.append(f"   * 接口删除:")
                                for iface in removed_interfaces:
                                    report_lines.append(f"     - ID: {iface['id']}, Name: {iface.get('name', 'N/A')}, Type: {iface.get('type', 'N/A')}, Network ID: {iface.get('network_id', 'N/A')}")
                            if modified_interfaces:
                                report_lines.append(f"   * 接口修改:")
                                for iface_change in modified_interfaces:
                                    iface_id = iface_change['id']
                                    iface_changes = iface_change['changes']
                                    report_lines.append(f"     - ID: {iface_id}")
                                    for iface_key, iface_change_detail in iface_changes.items():
                                        report_lines.append(f"       * {iface_key}: {iface_change_detail['old']} → {iface_change_detail['new']}")
                        else:
                            report_lines.append(f"   * {key}: {change['old']} → {change['new']}")

            # 新增网络
            if 'added_networks' in differences:
                report_lines.append("新增链路:")
                for network in differences['added_networks']:
                    report_lines.append(f" - ID: {network['id']}, Name: {network.get('name', 'N/A')}, Type: {network.get('type', 'N/A')}")
            # 删除网络
            if 'removed_networks' in differences:
                report_lines.append("删除链路:")
                for network in differences['removed_networks']:
                    report_lines.append(f" - ID: {network['id']}, Name: {network.get('name', 'N/A')}, Type: {network.get('type', 'N/A')}")
            # 修改网络
            if 'modified_networks' in differences:
                report_lines.append("修改链路:")
                for network_change in differences['modified_networks']:
                    network_id = network_change['id']
                    changes = network_change['changes']
                    report_lines.append(f" - ID: {network_id}")
                    for key, change in changes.items():
                        report_lines.append(f"   * {key}: {change['old']} → {change['new']}")

        report_lines.append("=" * 50)
        return "\n".join(report_lines)

    def get_added_interfaces(self, old_interfaces, new_interfaces):
        """识别新增的接口"""
        old_ids = {iface['id'] for iface in old_interfaces}
        added = [iface for iface in new_interfaces if iface['id'] not in old_ids]
        return added

    def get_removed_interfaces(self, old_interfaces, new_interfaces):
        """识别删除的接口"""
        new_ids = {iface['id'] for iface in new_interfaces}
        removed = [iface for iface in old_interfaces if iface['id'] not in new_ids]
        return removed

    def get_modified_interfaces(self, old_interfaces, new_interfaces):
        """识别修改的接口"""
        old_dict = {iface['id']: iface for iface in old_interfaces}
        new_dict = {iface['id']: iface for iface in new_interfaces}
        modified = []
        for iface_id in new_dict:
            if iface_id in old_dict:
                filtered_old = self.filter_ignored_fields(old_dict[iface_id])
                filtered_new = self.filter_ignored_fields(new_dict[iface_id])
                if filtered_old != filtered_new:
                    changes = self.find_changes(filtered_old, filtered_new)
                    modified.append({
                        'id': iface_id,
                        'changes': changes
                    })
        return modified

    def filter_ignored_fields(self, item):
        """返回一个新的字典，移除被忽略的字段"""
        IGNORED_FIELDS = {'left', 'top', 'uuid'}
        return {k: v for k, v in item.items() if k not in IGNORED_FIELDS}

    def find_changes(self, old, new):
        """找到两个字典之间的不同"""
        changes = {}
        for key in new:
            if key not in old:
                changes[key] = {'old': None, 'new': new[key]}
            elif new[key] != old[key]:
                changes[key] = {'old': old[key], 'new': new[key]}
        for key in old:
            if key not in new:
                changes[key] = {'old': old[key], 'new': None}
        return changes

    def write_output(self, content, additional_content=None):
        """将内容写入输出文件"""
        try:
            with open(self.output_path, 'w', encoding='utf-8') as f:
                f.write(content)
                if additional_content:
                    f.write("\n\n")
                    f.write(additional_content)
            logging.info(f"变更内容已输出到 {self.output_path}")
            print(f"变更内容已输出到 {self.output_path}")
        except Exception as e:
            logging.error(f"无法写入输出文件 {self.output_path}。错误信息: {e}")
            raise IOError(f"无法写入输出文件 {self.output_path}。错误信息: {e}")

class TopologyChangeDetector:
    """主控制类，协调各个组件完成拓扑变更检测"""

    def __init__(self, input_path, output_path):
        self.input_path = input_path
        self.output_path = output_path

    def run(self):
        start_time = datetime.now()
        execution_result = "成功"

        try:
            # 读取param.json
            param_reader = ParamReader(self.input_path)
            param = param_reader.read_param()

            lab_id = param.get('labId')
            if lab_id is None:
                raise ValueError("param.json 中缺少 'labId' 字段。")

            # 解析.unl文件
            unl_parser = UnlParser(lab_id)
            current_json = unl_parser.parse_unl_to_json()

            # 加载历史记录
            history_manager = HistoryManager(lab_id)
            history_json = history_manager.load_history()

            # 比较当前JSON与历史JSON
            comparator = TopologyComparator(current_json, history_json)
            differences = comparator.compare()

            return differences, start_time, execution_result

        except Exception as e:
            execution_result = "失败"
            end_time = datetime.now()
            formatted_diff = self.format_error_report(start_time, end_time, str(e))
            logging.error(f"拓扑变更检测失败。错误信息: {e}")
            return {"error": str(e)}, start_time, "失败"

    def format_error_report(self, start_time, end_time, error_message):
        report_lines = []
        report_lines.append("=" * 50)
        report_lines.append("拓扑变更检测报告")
        report_lines.append(f"执行时间：{start_time.strftime('%Y-%m-%d %H:%M:%S')} 至 {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        report_lines.append("执行结果：失败")
        report_lines.append("变更内容：")
        report_lines.append("-" * 50)
        report_lines.append(f"错误信息: {error_message}")
        report_lines.append("=" * 50)
        return "\n".join(report_lines)

class InterfaceStatusWriter:
    """将接口状态写入 data.txt"""

    def __init__(self, data_txt_path, mapping: Dict[str, Any]):
        self.data_txt_path = data_txt_path
        self.mapping = mapping

    def write_interface_status(self):
        """
        将接口状态写入 data.txt，格式如下：
        节点: sysname1 (host:port)
            接口: Ethernet1/0/0接口配置状态: 已配置IP地址
        节点: sysname2 (host:port)
            接口: Ethernet1/0/2接口配置状态: 未配置IP地址
        """
        try:
            with open(self.data_txt_path, 'w', encoding='utf-8') as f:
                telnet_devices = self.mapping.get("telnet_devices", {})
                for host_port, device_info in telnet_devices.items():
                    sysname = device_info.get("sysname", "未知节点")
                    interface_status_str = device_info.get("interface_status", "")
                    f.write(f"节点: {sysname} ({host_port})\n")
                    f.write(interface_status_str)
                    f.write("\n")  # 在设备之间添加空行
            logging.info(f"接口状态已写入 {self.data_txt_path}")
        except IOError as e:
            logging.error(f"写入 {self.data_txt_path} 时出错: {e}")
            sys.exit(1)

class CombinedScriptManager:
    """协调路由器管理和拓扑变更检测的主控类"""

    def __init__(self, input_path: str, output_path: str):
        self.input_path = input_path
        self.output_path = output_path
        self.mapping = {}

    def find_latest_folder(self, base_path: str) -> str:
        try:
            all_folders = [f for f in os.listdir(base_path) if f.isdigit()]
            if not all_folders:
                raise ValueError("在基础路径中未找到编号文件夹。")
            latest_folder = max(all_folders, key=int)
            logging.info(f"识别到最新文件夹: {latest_folder}")
            return latest_folder
        except FileNotFoundError:
            logging.error(f"基础路径未找到: {base_path}")
            sys.exit(1)
        except ValueError as ve:
            logging.error(ve)
            sys.exit(1)

    def load_telnet_info(self, input_path: str) -> Dict[str, Any]:
        try:
            with open(input_path, 'r', encoding='utf-8') as f:
                telnet_info = json.load(f)
            logging.info(f"成功从 {input_path} 加载 telnet_info")
            return telnet_info
        except FileNotFoundError:
            logging.error(f"param.json 文件未找到，路径: {input_path}")
            sys.exit(1)
        except json.JSONDecodeError as e:
            logging.error(f"解析 param.json 时出错: {e}")
            sys.exit(1)

    def write_output_json(self, output_path: str, data: Dict[str, Any]):
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)  # 使用 ensure_ascii=False 支持中文
            logging.info(f"映射结果已写入 {output_path}")
        except IOError as e:
            logging.error(f"写入输出文件 {output_path} 时出错: {e}")
            sys.exit(1)

    def run(self):
        # 如果路径中包含 {t}，则替换为最新文件夹编号
        base_path = "/uploadPath/reasoning"
        if "{t}" in self.input_path or "{t}" in self.output_path:
            latest_folder = self.find_latest_folder(base_path)
            self.input_path = self.input_path.replace("{t}", latest_folder)
            self.output_path = self.output_path.replace("{t}", latest_folder)
            logging.debug(f"解析后的 input_path: {self.input_path}")
            logging.debug(f"解析后的 output_path: {self.output_path}")

        # 加载telnet_info
        telnet_info = self.load_telnet_info(self.input_path)
        router_manager = RouterManager(telnet_info)

        # 读取并解析UNL文件
        lab_id = telnet_info.get("labId")
        if lab_id is not None:
            router_manager.read_unl_file(lab_id)
        else:
            logging.warning("telnet_info中未找到 'labId' 字段。")

        # 连接并获取sysnames和配置
        router_manager.connect_and_get_sysnames_and_configs()
        self.mapping = router_manager.collect_results()

        # 写入输出JSON
        self.write_output_json(self.output_path, self.mapping)

        # 写入接口状态到 data.txt
        output_dir = os.path.dirname(self.output_path)
        data_txt_path = os.path.join(output_dir, "data.txt")
        interface_writer = InterfaceStatusWriter(data_txt_path, self.mapping)
        interface_writer.write_interface_status()

        # 运行拓扑变更检测
        detector = TopologyChangeDetector(self.input_path, self.output_path)
        topology_differences, start_time, execution_result = detector.run()

        # 获取拓扑变更检测报告
        if topology_differences is None:
            topology_report = "首次运行，无历史记录进行比较。"
        elif 'error' in topology_differences:
            topology_report = f"拓扑变更检测失败。错误信息: {topology_differences['error']}"
        elif not topology_differences:
            topology_report = "无拓扑变化。"
        else:
            # 使用 OutputWriter 格式化拓扑变化
            output_writer = OutputWriter(self.output_path)
            topology_report = output_writer.format_diff(topology_differences, start_time, execution_result)

        # 将拓扑变化信息添加到主要的输出 JSON 中
        self.mapping['topology_changes'] = topology_differences if topology_differences else {}

        # 重新写入包含拓扑变化信息的 JSON
        self.write_output_json(self.output_path, self.mapping)

        # 如果需要，也可以将拓扑变化报告写入 data.txt 或其他文件
        # 这里选择将其追加到 data.txt 中
        try:
            with open(data_txt_path, 'a', encoding='utf-8') as f:
                f.write("\n拓扑变更检测报告:\n")
                f.write(topology_report)
            logging.info(f"拓扑变更检测报告已追加到 {data_txt_path}")
        except IOError as e:
            logging.error(f"无法将拓扑变更检测报告追加到 {data_txt_path}。错误信息: {e}")
            sys.exit(1)

        logging.info("所有任务已完成。")
        logging.info(json.dumps(self.mapping, indent=4, ensure_ascii=False))

def main():
    # 解析命令行参数
    arg_parser = ArgumentParserCustom()
    args = arg_parser.parse()

    # 运行整合后的脚本管理器
    combined_manager = CombinedScriptManager(args.input, args.output)
    combined_manager.run()

if __name__ == "__main__":
    main()
