#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler
import telnetlib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import re
from typing import Optional, Dict, Any, List

# ===========================
# 配置日志记录
# ===========================
def setup_logging(log_file: str = "combined_detector.log"):
    """配置日志记录"""
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)  # 设置为DEBUG以记录所有级别的日志

    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

    # 文件处理器，限制日志文件大小为5MB，保留5个备份
    try:
        file_handler = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=5)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except Exception as e:
        print(f"无法创建日志文件处理器。错误信息: {e}")
        sys.exit(1)

    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)


# ===========================
# 参数解析
# ===========================
class ArgumentParserCustom:
    """解析命令行参数"""

    def __init__(self):
        self.parser = argparse.ArgumentParser(description='综合Eveng平台拓扑变更检测和路由器配置收集脚本')
        self.parser.add_argument('-i', '--input', required=True, help='param.json文件的输入路径')
        self.parser.add_argument('-o', '--output', required=True, help='输出目录路径')
        self.args = None

    def parse(self):
        self.args = self.parser.parse_args()
        return self.args


# ===========================
# 通用工具函数
# ===========================
def filter_ignored_fields(item: Dict[str, Any], ignored_fields: set) -> Dict[str, Any]:
    """返回一个新的字典，移除被忽略的字段"""
    return {k: v for k, v in item.items() if k not in ignored_fields}

def find_changes(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
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


# ===========================
# 参数读取
# ===========================
class ParamReader:
    """读取和解析param.json文件"""

    def __init__(self, param_path: str):
        self.param_path = param_path
        self.param = None

    def read_param(self) -> Dict[str, Any]:
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


# ===========================
# .unl文件解析
# ===========================
class UnlParser:
    """查找并解析.unl文件，将其转换为JSON格式"""

    LABS_DIR = '/opt/unetlab/labs'

    def __init__(self, lab_id: int):
        self.lab_id = lab_id
        self.unl_path = self.find_unl_file()
        self.lab_json = None

    def find_unl_file(self) -> str:
        unl_filename = f"{self.lab_id}.unl"
        unl_path = os.path.join(self.LABS_DIR, unl_filename)
        logging.info(f"查找.unl文件: {unl_path}")
        if not os.path.exists(unl_path):
            logging.error(f"未找到对应的.unl文件: {unl_path}")
            raise FileNotFoundError(f"未找到对应的.unl文件: {unl_path}")
        logging.info(f"找到.unl文件: {unl_path}")
        return unl_path

    def parse_unl_to_json(self) -> Dict[str, Any]:
        try:
            logging.info(f"解析.unl文件: {self.unl_path}")
            tree = ET.parse(self.unl_path)
            root = tree.getroot()
            lab_json = {
                'lab': {
                    'name': root.attrib.get('name'),
                    'id': root.attrib.get('id'),
                    'version': root.attrib.get('version'),
                    'scripttimeout': root.attrib.get('scripttimeout'),
                    'lock': root.attrib.get('lock')
                }
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
                        interfaces = [iface.attrib.copy() for iface in node.findall('interface')]
                        node_dict['interfaces'] = interfaces
                        nodes.append(node_dict)
                lab_json['topology'] = {'nodes': nodes}

                # 解析网络
                networks = [network.attrib.copy() for network in topology.find('networks').findall('network')] if topology.find('networks') else []
                lab_json['topology']['networks'] = networks

            self.lab_json = lab_json
            logging.info("解析.unl文件成功。")
            return lab_json
        except ET.ParseError as e:
            logging.error(f"无法解析XML文件 {self.unl_path}。错误信息: {e}")
            raise ValueError(f"无法解析XML文件 {self.unl_path}。错误信息: {e}")


# ===========================
# 历史记录管理
# ===========================
class HistoryManager:
    """管理历史记录，包括加载和保存历史JSON文件"""

    HISTORY_DIR = '/opt/unetlab/labs_history'

    def __init__(self, lab_id: int):
        self.lab_id = lab_id
        self.history_path = self.get_history_path()

    def get_history_path(self) -> str:
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

    def load_history(self) -> Optional[Dict[str, Any]]:
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

    def save_history(self, data: Dict[str, Any]):
        try:
            with open(self.history_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            logging.info(f"历史记录已保存到 {self.history_path}")
        except Exception as e:
            logging.error(f"无法保存历史记录到 {self.history_path}。错误信息: {e}")
            raise IOError(f"无法保存历史记录到 {self.history_path}。错误信息: {e}")


# ===========================
# 拓扑比较
# ===========================
class TopologyComparator:
    """比较当前JSON与历史JSON，检测拓扑变化"""

    IGNORED_FIELDS = {'left', 'top', 'uuid'}

    def __init__(self, current: Dict[str, Any], history: Optional[Dict[str, Any]]):
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

    def compare(self) -> Optional[Dict[str, Any]]:
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
                filtered_current = filter_ignored_fields(current_nodes[node_id], self.IGNORED_FIELDS)
                filtered_history = filter_ignored_fields(history_nodes[node_id], self.IGNORED_FIELDS)
                if filtered_current != filtered_history:
                    changes = find_changes(filtered_history, filtered_current)
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
                filtered_current = filter_ignored_fields(current_networks[network_id], self.IGNORED_FIELDS)
                filtered_history = filter_ignored_fields(history_networks[network_id], self.IGNORED_FIELDS)
                if filtered_current != filtered_history:
                    changes = find_changes(filtered_history, filtered_current)
                    self.differences['modified_networks'].append({
                        'id': network_id,
                        'changes': changes
                    })

        # 移除空的类别
        self.differences = {k: v for k, v in self.differences.items() if v}
        return self.differences


# ===========================
# 输出写入
# ===========================
class OutputWriter:
    """将检测到的变化内容和路由器配置信息输出到指定路径"""

    def __init__(self, base_output_dir: str):
        self.base_output_dir = base_output_dir
        os.makedirs(self.base_output_dir, exist_ok=True)

    def format_diff(self, differences: Optional[Dict[str, Any]], execution_time: datetime, execution_result: str) -> str:
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

    def write_change_report(self, content: str):
        """将变更报告写入文件"""
        report_path = os.path.join(self.base_output_dir, "change_report.txt")
        try:
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write(content)
            logging.info(f"拓扑变更报告已写入 {report_path}")
        except Exception as e:
            logging.error(f"无法写入变更报告到 {report_path}。错误信息: {e}")
            raise IOError(f"无法写入变更报告到 {report_path}。错误信息: {e}")

    def get_added_interfaces(self, old_interfaces, new_interfaces) -> List[Dict[str, str]]:
        """识别新增的接口"""
        old_ids = {iface['id'] for iface in old_interfaces}
        return [iface for iface in new_interfaces if iface['id'] not in old_ids]

    def get_removed_interfaces(self, old_interfaces, new_interfaces) -> List[Dict[str, str]]:
        """识别删除的接口"""
        new_ids = {iface['id'] for iface in new_interfaces}
        return [iface for iface in old_interfaces if iface['id'] not in new_ids]

    def get_modified_interfaces(self, old_interfaces, new_interfaces) -> List[Dict[str, Any]]:
        """识别修改的接口"""
        old_dict = {iface['id']: iface for iface in old_interfaces}
        new_dict = {iface['id']: iface for iface in new_interfaces}
        modified = []
        for iface_id in new_dict:
            if iface_id in old_dict:
                filtered_old = filter_ignored_fields(old_dict[iface_id], {'left', 'top', 'uuid'})
                filtered_new = filter_ignored_fields(new_dict[iface_id], {'left', 'top', 'uuid'})
                if filtered_old != filtered_new:
                    changes = find_changes(filtered_old, filtered_new)
                    modified.append({
                        'id': iface_id,
                        'changes': changes
                    })
        return modified

    def write_json_output(self, path: str, data: Dict[str, Any]):
        """将JSON数据写入文件"""
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)  # 使用ensure_ascii=False以支持中文
            logging.info(f"路由器配置信息已写入 {path}")
        except IOError as e:
            logging.error(f"无法写入JSON输出到 {path}。错误信息: {e}")
            raise IOError(f"无法写入JSON输出到 {path}。错误信息: {e}")

    def write_interface_status(self, data_txt_path: str, mapping: Dict[str, Any]):
        """
        将接口状态写入 data.txt，格式如下：
        节点: sysname1 (host:port)
            接口: Ethernet1/0/0接口配置状态: 已配置IP地址
        节点: sysname2 (host:port)
            接口: Ethernet1/0/2接口配置状态: 未配置IP地址
        """
        try:
            with open(data_txt_path, 'w', encoding='utf-8') as f:
                telnet_devices = mapping.get("telnet_devices", {})
                for host_port, device_info in telnet_devices.items():
                    sysname = device_info.get("sysname", "未知节点")
                    interface_status_str = device_info.get("interface_status", "")
                    f.write(f"节点: {sysname} ({host_port})\n")
                    f.write(interface_status_str)
                    f.write("\n")  # 在设备之间添加空行
            logging.info(f"接口状态已写入 {data_txt_path}")
        except IOError as e:
            logging.error(f"写入 {data_txt_path} 时出错: {e}")
            raise IOError(f"写入 {data_txt_path} 时出错: {e}")


# ===========================
# Telnet 管理器
# ===========================
class RouterManager:
    def __init__(self, telnet_info: Dict[str, Any], max_workers: int = 20):
        self.telnet_info = telnet_info
        self.telnet_sysnames: Dict[str, str] = {}
        self.telnet_configurations: Dict[str, List[Dict[str, str]]] = {}  # Structured interface info
        self.network_connections: Optional[List[Dict[str, Any]]] = None
        self.node_interfaces: Dict[str, List[Dict[str, str]]] = {}  # Node interface info from UNL
        self.max_workers = max_workers
        self.telnet_lock = Lock()
        # Define command sequences for different device types
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
        else:
            logging.warning(f"[{tn.host}:{tn.port}] No output received from Telnet commands.")
        return command_outputs

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
        Collect all results and output interface configuration status.
        """
        # Store interface status information
        interface_status = {}

        for host_port, sysname in self.telnet_sysnames.items():
            node_interfaces = self.node_interfaces.get(sysname, [])
            telnet_interfaces = self.telnet_configurations.get(host_port, [])

            # Extract interface names from Telnet and convert to lowercase for comparison
            telnet_interface_names = [iface['Interface'].lower() for iface in telnet_interfaces if isinstance(iface, dict)]

            logging.debug(f"[{host_port}] Telnet fetched interfaces: {telnet_interface_names}")

            interface_status[host_port] = ""

            for iface in node_interfaces:
                # Format interface name, e.g., type="ethernet" name="e1/0/0" => "Ethernet1/0/0"
                iface_name = iface['name']
                if iface_name.lower().startswith('e'):
                    iface_number = iface_name[1:]  # Remove 'e' prefix
                    iface_formatted = f"Ethernet{iface_number}"
                else:
                    # If interface name does not start with 'e', format as per type
                    iface_formatted = f"{iface['type'].capitalize()}{iface['name']}"

                iface_formatted_lower = iface_formatted.lower()
                logging.debug(f"[{host_port}] Formatted interface name: {iface_formatted}")

                # Determine if interface has IP configured
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


# ===========================
# 主控制类
# ===========================
class TopologyAndConfigDetector:
    """主控制类，协调拓扑变更检测和路由器配置收集"""

    def __init__(self, input_path: str, base_output_dir: str):
        self.input_path = input_path
        self.base_output_dir = base_output_dir
        self.output_writer = OutputWriter(self.base_output_dir)

    def run(self):
        start_time = datetime.now()
        execution_result = "成功"

        try:
            # 读取param.json
            param_reader = ParamReader(self.input_path)
            param = param_reader.read_param()

            # 提取labId和telnet_info
            lab_id = param.get('labId')
            telnet_info = param.get('telnet_info')
            if lab_id is None:
                raise ValueError("param.json 中缺少 'labId' 字段。")
            if telnet_info is None:
                raise ValueError("param.json 中缺少 'telnet_info' 字段。")

            # 解析.unl文件
            unl_parser = UnlParser(lab_id)
            current_json = unl_parser.parse_unl_to_json()

            # 拓扑变更检测
            history_manager = HistoryManager(lab_id)
            history_json = history_manager.load_history()
            comparator = TopologyComparator(current_json, history_json)
            differences = comparator.compare()
            formatted_diff = self.output_writer.format_diff(differences, start_time, execution_result)
            self.output_writer.write_change_report(formatted_diff)
            history_manager.save_history(current_json)

            # 路由器配置收集
            router_manager = RouterManager(telnet_info)
            # 读取UNL文件以获取网络连接和节点接口信息
            router_manager.read_unl_file(lab_id)
            router_manager.connect_and_get_sysnames_and_configs()
            mapping = router_manager.collect_results()

            # 将路由器配置信息写入JSON文件
            mapping_output_path = os.path.join(self.base_output_dir, "mapping.json")
            self.output_writer.write_json_output(mapping_output_path, mapping)

            # 将接口状态写入data.txt
            data_txt_path = os.path.join(self.base_output_dir, "data.txt")
            self.output_writer.write_interface_status(data_txt_path, mapping)

        except Exception as e:
            execution_result = "失败"
            end_time = datetime.now()
            formatted_diff = self.format_error_report(start_time, end_time, str(e))
            try:
                self.output_writer.write_change_report(formatted_diff)
            except Exception as write_error:
                logging.error(f"无法写入错误报告到 {self.base_output_dir}。错误信息: {write_error}")
            logging.error(f"脚本运行失败。错误信息: {e}")
            print(f"错误: {e}")
            sys.exit(1)

    def format_error_report(self, start_time: datetime, end_time: datetime, error_message: str) -> str:
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


# ===========================
# 主函数
# ===========================
def main():
    # 配置日志
    setup_logging()

    # 解析命令行参数
    arg_parser = ArgumentParserCustom()
    args = arg_parser.parse()

    # 确保输出目录存在
    base_output_dir = args.output
    os.makedirs(base_output_dir, exist_ok=True)

    # 运行拓扑和配置检测
    detector = TopologyAndConfigDetector(args.input, base_output_dir)
    detector.run()


if __name__ == "__main__":
    main()
