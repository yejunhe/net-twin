import xml.etree.ElementTree as ET
import json
import telnetlib
import os
import argparse
import datetime
import sys
import time
import re
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import logging
from typing import Optional, Dict, Any

# Configure logging for better traceability and control
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

class UNLParser:
    def __init__(self, unl_file):
        self.unl_file = unl_file
        self.nodes = {}
        self.networks = {}

    def parse(self):
        """解析.unl文件，提取节点和网络信息。"""
        tree = ET.parse(self.unl_file)
        root = tree.getroot()

        # 提取节点信息
        for node in root.findall(".//node"):
            node_id = node.get('id')
            node_name = node.get('name')
            interfaces = []

            for interface in node.findall("interface"):
                interfaces.append({
                    'id': interface.get('id'),
                    'name': interface.get('name'),
                    'network_id': interface.get('network_id')
                })

            self.nodes[node_id] = {
                'name': node_name,
                'interfaces': interfaces
            }

        # 提取网络信息
        for network in root.findall(".//network"):
            network_id = network.get('id')
            network_name = network.get('name')
            self.networks[network_id] = network_name

        logging.info("Parsed UNL File:")
        logging.info(f"Nodes: {self.nodes}")
        logging.info(f"Networks: {self.networks}")

class RouterTelnetManager:
    def __init__(self, telnet_info):
        self.telnet_info = telnet_info
        self.connections = {}  # 存储每个路由器的Telnet连接
        self.sysnames = {}
        self.telnet_lock = Lock()

    def connect(self, host, port):
        """建立Telnet连接并禁用分页。"""
        try:
            tn = telnetlib.Telnet(host, port, timeout=10)
            tn.host = host  # 添加host属性以便在其他地方使用
            tn.port = port
            tn.write(b'\n')  # 发送回车确保进入命令提示符
            tn.read_until(b'>', timeout=5)  # 等待提示符
            tn.write(b'scr 0 t\n')  # 禁用分页
            tn.read_until(b'>', timeout=5)  # 等待提示符确认
            with self.telnet_lock:
                self.connections[host + ':' + str(port)] = tn
            logging.info(f"Telnet connection established to {host}:{port}")
            return tn
        except Exception as e:
            logging.error(f"Error connecting to {host}:{port} - {e}")
            return None

    def get_sysname(self, tn, host, port):
        """通过 Telnet 获取节点的 sysname"""
        try:
            tn.write(b'\n')  # 发送回车确保进入命令提示符
            output = tn.read_until(b'>', timeout=5).decode('ascii').splitlines()
            sysname = None

            for line in output:
                if line.startswith('<') and line.endswith('>'):
                    sysname = line.strip('<> ').strip()
                    break

            if sysname:
                self.sysnames[host + ':' + str(port)] = sysname
                logging.info(f"Connected to {host}:{port} - Sysname: {sysname}")
                return sysname
            else:
                logging.warning(f"Could not determine sysname from output: {output}")
                return None

        except Exception as e:
            logging.error(f"Error retrieving sysname from {host}:{port} - {e}")
            return None

    def connect_and_get_sysnames(self):
        """连接每个路由器并获取其 sysname"""
        nodes = self.telnet_info.get("node", [])
        if not nodes:
            logging.warning("No nodes found in telnet_info.")
            return

        for node in nodes:
            host = node.get("hostip")
            port = node.get("port")
            if not host or not port:
                logging.warning(f"Host IP or port missing for node. Skipping.")
                continue
            tn = self.connect(host, port)
            if tn:
                sysname = self.get_sysname(tn, host, port)
                if not sysname:
                    logging.warning(f"Failed to retrieve sysname for {host}:{port}")
            else:
                logging.warning(f"Failed to establish Telnet connection for {host}:{port}")

    def send_command(self, host_port, command):
        """发送命令并返回输出"""
        tn = self.connections.get(host_port)
        if not tn:
            logging.warning(f"No active Telnet connection for {host_port}")
            return None
        try:
            tn.write(command.encode('ascii') + b'\n')
            output = tn.read_until(b'>', timeout=10).decode('ascii')
            return output
        except Exception as e:
            logging.error(f"Error sending command to {host_port} - {e}")
            return None

    def close_all(self):
        """关闭所有Telnet连接"""
        for host_port, tn in self.connections.items():
            tn.close()
            logging.info(f"Closed Telnet connection to {host_port}")

class TopologyMapper:
    def __init__(self, unl_parser, telnet_manager):
        self.unl_parser = unl_parser
        self.telnet_manager = telnet_manager

    def map_topology(self):
        """将Telnet获取的sysnames映射到UNL拓扑中的节点。"""
        mapping = {}
        for node_id, node_info in self.unl_parser.nodes.items():
            node_name = node_info['name']
            for host_port, sysname in self.telnet_manager.sysnames.items():
                if node_name == sysname:
                    mapping[node_id] = {
                        'node_name': node_name,
                        'host_port': host_port,
                        'sysname': sysname
                    }

        logging.info("Mapping between UNL topology and Telnet sysnames:")
        logging.info(mapping)
        return mapping

class OSPFDiagnostic:
    def __init__(self, telnet_manager):
        self.telnet_manager = telnet_manager
        self.ospf_data = {}

    def collect_ospf_info(self, host_port):
        """收集OSPF信息。"""
        output_peer = self.telnet_manager.send_command(host_port, 'display ospf peer')
        output_interface = self.telnet_manager.send_command(host_port, 'display ospf interface')
        output_brief = self.telnet_manager.send_command(host_port, 'display ospf brief')

        if output_peer and output_interface and output_brief:
            self.ospf_data[host_port] = {
                'peer': self.parse_output_peer(output_peer),
                'interface': self.parse_output_interface(output_interface),
                'brief': self.parse_output_brief(output_brief)
            }
            logging.info(f"Collected OSPF info from {host_port}")
        else:
            logging.warning(f"Failed to collect OSPF info from {host_port}")

    @staticmethod
    def parse_output_peer(output):
        """解析OSPF邻居状态输出"""
        return output.splitlines()

    @staticmethod
    def parse_output_interface(output):
        """解析OSPF接口状态输出（列格式）"""
        return output.splitlines()

    @staticmethod
    def parse_output_brief(output):
        """解析OSPF简要信息输出（行格式）"""
        return output.splitlines()

    def analyze_ospf_status(self):
        """分析收集到的OSPF信息，检测可能的故障。"""
        faults = []

        for router, info in self.ospf_data.items():
            logging.info(f"\nAnalyzing OSPF status for router {router}...")

            # 检查OSPF邻居状态
            peer_status = info.get('peer', [])
            logging.info(f"Peer Status for {router}:")
            for line in peer_status:
                if any(state in line for state in ['Init', 'Down', 'Attempt']):
                    faults.append(f"OSPF邻居问题检测到 {router}: {line}")
                    logging.warning(f"Detected Neighbor Issue: {line}")

            # 检查OSPF接口配置（列格式）
            interface_status = info.get('interface', [])
            logging.info(f"Interface Status for {router}:")
            for line in interface_status:
                logging.debug(f"Analyzing interface line: {line}")
                if "State" in line:  # 假设状态信息在列标题下
                    continue  # 跳过标题行
                parts = line.split()
                if len(parts) >= 5:
                    interface_state = parts[3].lower()
                    if interface_state == "down":
                        faults.append(
                            f"OSPF接口异常在 {router}: 接口 {parts[0]} ({parts[1]}) 状态Down")
                        logging.warning(f"Detected Interface Down: {parts[0]} ({parts[1]})")
                    elif interface_state not in ["full", "p-2-p", "bdr", "dr"]:
                        faults.append(
                            f"OSPF接口状态异常在 {router}: 接口 {parts[0]} ({parts[1]}) 状态 {parts[3]}")
                        logging.warning(f"Detected Abnormal Interface State: {parts[0]} ({parts[1]}) - {parts[3]}")

            # 检查OSPF简要信息中的接口状态（行格式）
            brief_status = info.get('brief', [])
            logging.info(f"Brief Status for {router}:")
            for line in brief_status:
                logging.debug(f"Analyzing brief line: {line}")

                if "State:" in line:
                    # 示例行: "State: Down"
                    state = line.split("State:")[1].strip().lower()
                    if state == "down":
                        faults.append(
                            f"OSPF概要信息接口异常在 {router}: 状态Down")
                        logging.warning(f"Detected Interface Down in Brief: State Down")
                    elif state not in ["full", "p-2-p", "bdr", "dr"]:
                        faults.append(
                            f"OSPF概要信息接口状态异常在 {router}: 状态 {state}")
                        logging.warning(f"Detected Abnormal Interface State in Brief: State {state}")

            logging.info(f"Finished analyzing OSPF status for router {router}.")

        if faults:
            logging.info("\n检测到OSPF故障:")
            for fault in faults:
                logging.info(fault)
        else:
            logging.info("\n未检测到OSPF故障。")

        return faults

    def collect_route_table(self, host_port):
        """从每个路由器收集OSPF的路由表信息。"""
        output = self.telnet_manager.send_command(host_port, 'display ip routing-table')
        if output:
            if host_port not in self.ospf_data:
                self.ospf_data[host_port] = {}
            self.ospf_data[host_port]['route'] = self.parse_output_route(output)
            logging.info(f"Collected OSPF route info from {host_port}")
        else:
            logging.warning(f"Failed to collect OSPF route info from {host_port}")

    @staticmethod
    def parse_output_route(output):
        """解析路由表输出"""
        return output.splitlines()

    def analyze_route_table(self):
        """分析路由表，确认是否有OSPF生成的路由。"""
        faults = []

        for router, info in self.ospf_data.items():
            route_table = info.get('route', [])
            logging.info(f"\nAnalyzing OSPF route table for router {router}...")

            # 检查是否存在由OSPF生成的路由
            ospf_routes = [line for line in route_table if 'OSPF' in line]
            if not ospf_routes:
                faults.append(f"在路由表中未检测到OSPF生成的路由 {router}")
                logging.warning(f"OSPF routes not found in routing table for {router}")
            else:
                logging.info(f"OSPF routes found for {router}:")
                for route in ospf_routes:
                    logging.debug(route)

        return faults

class ISISDiagnostic:
    def __init__(self, telnet_manager):
        self.telnet_manager = telnet_manager
        self.isis_data = {}

    def collect_isis_info(self, host_port):
        """收集ISIS信息。"""
        output_peer = self.telnet_manager.send_command(host_port, 'display isis peer')
        output_interface = self.telnet_manager.send_command(host_port, 'display isis interface')
        output_brief = self.telnet_manager.send_command(host_port, 'display isis brief')

        if output_peer and output_interface and output_brief:
            self.isis_data[host_port] = {
                'peer': self.parse_output_peer(output_peer),
                'interface': self.parse_output_interface(output_interface),
                'brief': self.parse_output_brief(output_brief)
            }
            logging.info(f"Collected ISIS info from {host_port}")
        else:
            logging.warning(f"Failed to collect ISIS info from {host_port}")

    @staticmethod
    def parse_output_peer(output):
        """解析ISIS邻居状态输出"""
        return output.splitlines()

    @staticmethod
    def parse_output_interface(output):
        """解析ISIS接口状态输出（列格式）"""
        return output.splitlines()

    @staticmethod
    def parse_output_brief(output):
        """解析ISIS简要信息输出（行格式）"""
        return output.splitlines()

    def analyze_isis_status(self):
        """分析收集到的ISIS信息，检测可能的故障。"""
        faults = []

        for router, info in self.isis_data.items():
            logging.info(f"\nAnalyzing ISIS status for router {router}...")

            # 检查ISIS邻居状态
            peer_status = info.get('peer', [])
            logging.info(f"Peer Status for {router}:")
            for line in peer_status:
                if any(state in line for state in ['Down', 'Init']):
                    faults.append(f"ISIS邻居问题检测到 {router}: {line}")
                    logging.warning(f"Detected Neighbor Issue: {line}")

            # 检查ISIS接口配置（列格式）
            interface_status = info.get('interface', [])
            logging.info(f"Interface Status for {router}:")
            for line in interface_status:
                logging.debug(f"Analyzing interface line: {line}")
                if "State" in line.lower():
                    continue  # 跳过标题行
                parts = line.split()
                if len(parts) >= 5:
                    interface_state = parts[3].lower()
                    if interface_state == "down":
                        faults.append(
                            f"ISIS接口异常在 {router}: 接口 {parts[0]} ({parts[1]}) 状态Down")
                        logging.warning(f"Detected Interface Down: {parts[0]} ({parts[1]})")
                    elif interface_state not in ["up", "point-to-point", "broadcast", "multi-access"]:
                        faults.append(
                            f"ISIS接口状态异常在 {router}: 接口 {parts[0]} ({parts[1]}) 状态 {parts[3]}")
                        logging.warning(f"Detected Abnormal Interface State: {parts[0]} ({parts[1]}) - {parts[3]}")

            # 检查ISIS简要信息中的接口状态（行格式）
            brief_status = info.get('brief', [])
            logging.info(f"Brief Status for {router}:")
            for line in brief_status:
                logging.debug(f"Analyzing brief line: {line}")

                if "State:" in line:
                    # 示例行: "State: Down"
                    state = line.split("State:")[1].strip().lower()
                    if state == "down":
                        faults.append(
                            f"ISIS概要信息接口异常在 {router}: 状态Down")
                        logging.warning(f"Detected Interface Down in Brief: State Down")
                    elif state not in ["up", "point-to-point", "broadcast", "multi-access"]:
                        faults.append(
                            f"ISIS概要信息接口状态异常在 {router}: 状态 {state}")
                        logging.warning(f"Detected Abnormal Interface State in Brief: State {state}")

            logging.info(f"Finished analyzing ISIS status for router {router}.")

        if faults:
            logging.info("\n检测到ISIS故障:")
            for fault in faults:
                logging.info(fault)
        else:
            logging.info("\n未检测到ISIS故障。")

        return faults

    def collect_route_table(self, host_port):
        """从每个路由器收集ISIS的路由表信息。"""
        output = self.telnet_manager.send_command(host_port, 'display ip routing-table')
        if output:
            if host_port not in self.isis_data:
                self.isis_data[host_port] = {}
            self.isis_data[host_port]['route'] = self.parse_output_route(output)
            logging.info(f"Collected ISIS route info from {host_port}")
        else:
            logging.warning(f"Failed to collect ISIS route info from {host_port}")

    @staticmethod
    def parse_output_route(output):
        """解析路由表输出"""
        return output.splitlines()

    def analyze_route_table(self):
        """分析路由表，确认是否有ISIS生成的路由。"""
        faults = []

        for router, info in self.isis_data.items():
            route_table = info.get('route', [])
            logging.info(f"\nAnalyzing ISIS route table for router {router}...")

            # 检查是否存在由ISIS生成的路由
            isis_routes = [line for line in route_table if any(proto in line for proto in ['ISIS', 'ISIS-L1', 'ISIS-L2', 'ISIS-L1-L2'])]
            if not isis_routes:
                faults.append(f"在路由表中未检测到ISIS生成的路由 {router}")
                logging.warning(f"ISIS routes not found in routing table for {router}")
            else:
                logging.info(f"ISIS routes found for {router}:")
                for route in isis_routes:
                    logging.debug(route)

        return faults

class RouterManager:
    def __init__(self, telnet_info: Dict[str, Any], max_workers: int = 20):
        self.telnet_info = telnet_info
        self.telnet_bgp_errors: Dict[str, Dict[str, int]] = {}
        self.max_workers = max_workers
        self.telnet_lock = Lock()

    # 错误条目的中文映射
    error_translation_map = {
        "Routes received with cluster ID loop": "收到的路由具有集群ID循环",
        "Routes received with as path count over limit": "收到的路由AS路径计数超过限制",
        "Routes advertised with as path count over limit": "通告的路由AS路径计数超过限制",
        "Routes received with As loop": "收到的路由存在AS环路",
        "Routes received with Zero RD(0:0)": "收到的路由具有零RD(0:0)",
        "Routes received with no prefix": "收到的路由没有前缀",
        "Routes received with error path-attribute": "收到的路由路径属性错误",
        "Routes received with originator ID loop": "收到的路由具有发起者ID循环",
        "Routes received with total number over limit": "收到的路由总数超过限制",
        "Routes received with error router id": "收到的路由具有错误的路由器ID"
    }

    def execute_telnet_commands(self, tn: telnetlib.Telnet, command: str) -> str:
        """
        Executes a single command on a Telnet session and retrieves the output.
        """
        try:
            tn.write(b'\n')
            time.sleep(1)
            output = tn.read_very_eager().decode('ascii', errors='ignore')
            logging.debug(f"[{tn.host}:{tn.port}] Initial Telnet output:\n{output}")

            # Send the command
            tn.write(command.encode('ascii') + b'\n')
            logging.info(f"[{tn.host}:{tn.port}] Sending command: {command}")
            time.sleep(1)
            cmd_output = tn.read_very_eager().decode('ascii', errors='ignore')
            output += cmd_output
            logging.debug(f"[{tn.host}:{tn.port}] Output for '{command}':\n{cmd_output}")

            return output
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] Telnet Error: {e}")
            return ""

    def get_bgp_error_statistics(self, tn: telnetlib.Telnet) -> Optional[Dict[str, int]]:
        """
        Retrieves BGP error statistics by executing the 'display bgp error discard' command.
        """
        command = 'display bgp error discard'
        output = self.execute_telnet_commands(tn, command)
        if output:
            # Parse the BGP error statistics from the output
            return self.parse_bgp_error_statistics(output)
        else:
            logging.warning(f"[{tn.host}:{tn.port}] No output received for BGP error statistics.")
            return None

    def parse_bgp_error_statistics(self, output: str) -> Dict[str, int]:
        """
        Parses the BGP error statistics from the Telnet output using regular expressions.
        """
        # Define regular expressions to capture BGP error statistics
        pattern = re.compile(
            r'Routes received with cluster ID loop\s*:\s*(\d+)|'
            r'Routes received with as path count over limit\s*:\s*(\d+)|'
            r'Routes advertised with as path count over limit\s*:\s*(\d+)|'
            r'Routes received with As loop\s*:\s*(\d+)|'
            r'Routes received with Zero RD\(0:0\)\s*:\s*(\d+)|'
            r'Routes received with no prefix\s*:\s*(\d+)|'
            r'Routes received with error path-attribute\s*:\s*(\d+)|'
            r'Routes received with originator ID loop\s*:\s*(\d+)|'
            r'Routes received with total number over limit\s*:\s*(\d+)|'
            r'Routes received with error router id\s*:\s*(\d+)'
        )

        # Initialize a dictionary to store error counts
        error_counts = {
            "Routes received with cluster ID loop": 0,
            "Routes received with as path count over limit": 0,
            "Routes advertised with as path count over limit": 0,
            "Routes received with As loop": 0,
            "Routes received with Zero RD(0:0)": 0,
            "Routes received with no prefix": 0,
            "Routes received with error path-attribute": 0,
            "Routes received with originator ID loop": 0,
            "Routes received with total number over limit": 0,
            "Routes received with error router id": 0,
        }

        # Iterate over all matches and update the error counts
        for match in pattern.findall(output):
            for idx, value in enumerate(match):
                if value:
                    error_type = list(error_counts.keys())[idx]
                    error_counts[error_type] = int(value)

        return error_counts

    def connect_and_get_bgp_errors(self):
        """
        Connects to routers and retrieves BGP error statistics.
        """
        nodes = self.telnet_info.get("node", [])
        if not nodes:
            logging.warning("No nodes found in telnet_info.")
            return

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_node = {}
            for node in nodes:
                host, port = node.get("hostip"), node.get("port")
                if not host or not port:
                    logging.warning(f"Host IP or port missing for node. Skipping.")
                    continue
                try:
                    tn = telnetlib.Telnet(host, port, timeout=10)
                    tn.host, tn.port = host, port
                    # Disable pagination
                    tn.write(b'scr 0 t\n')
                    tn.read_until(b'>', timeout=5)
                    future = executor.submit(self.get_bgp_error_statistics, tn)
                    future_to_node[future] = tn
                except Exception as e:
                    logging.error(f"Failed to connect to {host}:{port} via Telnet: {e}")

            for future in as_completed(future_to_node):
                tn = future_to_node[future]
                host, port = tn.host, tn.port
                bgp_errors = future.result()
                key = f"{host}:{port}"
                with self.telnet_lock:
                    self.telnet_bgp_errors[key] = bgp_errors or {}
                logging.info(f"[{host}:{port}] BGP error retrieval {'successful' if bgp_errors else 'failed'}.")

                tn.close()
                logging.info(f"Closed Telnet connection to {host}:{port}")

    def generate_report(self) -> str:
        """
        Generates a formatted BGP fault detection report in Chinese.
        """
        report_lines = [
            "BGP故障检测报告",
            f"执行时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "执行结果：",
            "------------------------------"
        ]

        total_faults = 0

        for host_port, error_info in self.telnet_bgp_errors.items():
            report_lines.append(f"[{host_port}]")
            has_fault = False
            for error_type, count in error_info.items():
                # 获取中文描述
                error_description = self.error_translation_map.get(error_type, error_type)
                report_lines.append(f"{error_description} : {count}")
                if count > 0:
                    has_fault = True
                    total_faults += count

            if not has_fault:
                report_lines.append("没有故障信息")
            report_lines.append("------------------------------")

        # Summary line
        if total_faults > 0:
            report_lines.append(f"检测结果：发现 {total_faults} 个故障条目")
        else:
            report_lines.append("检测结果：没有故障信息")

        return "\n".join(report_lines)

    def generate_report_section(self) -> str:
        """
        Generates the BGP report section.
        """
        return self.generate_report()

class OSPFISISDiagnosticReport:
    def __init__(self, ospf_faults, ospf_route_faults, isis_faults, isis_route_faults):
        self.ospf_faults = ospf_faults
        self.ospf_route_faults = ospf_route_faults
        self.isis_faults = isis_faults
        self.isis_route_faults = isis_route_faults

    def generate_report(self) -> str:
        """生成OSPF和ISIS的综合诊断报告。"""
        report_lines = [
            "网络协议诊断报告",
            f"报告时间：{datetime.now().strftime('%Y年%m月%d日 %H:%M:%S')}",
            "==============================",
            "检测内容：OSPF和ISIS协议",
            "------------------------------"
        ]

        if self.ospf_faults:
            report_lines.append("OSPF故障:")
            for fault in self.ospf_faults:
                report_lines.append(fault)
        else:
            report_lines.append("未检测到OSPF故障。")

        if self.ospf_route_faults:
            report_lines.append("\nOSPF路由表故障:")
            for fault in self.ospf_route_faults:
                report_lines.append(fault)
        else:
            report_lines.append("\nOSPF路由表未检测到故障。")

        if self.isis_faults:
            report_lines.append("\nISIS故障:")
            for fault in self.isis_faults:
                report_lines.append(fault)
        else:
            report_lines.append("\n未检测到ISIS故障。")

        if self.isis_route_faults:
            report_lines.append("\nISIS路由表故障:")
            for fault in self.isis_route_faults:
                report_lines.append(fault)
        else:
            report_lines.append("\nISIS路由表未检测到故障。")

        report_lines.append("==============================")

        return "\n".join(report_lines)

def write_diagnostic_report(output_path, ospf_isis_report, bgp_report):
    """将诊断报告写入输出文件，使用UTF-8编码并结构化格式。"""
    try:
        with open(output_path, 'w', encoding='utf-8') as output_file:
            output_file.write(ospf_isis_report)
            output_file.write("\n\n")
            output_file.write(bgp_report)
        logging.info(f"诊断结果已写入 {output_path}")
    except IOError as e:
        logging.error(f"Error writing to output file: {e}")
        sys.exit(1)

def find_latest_folder(base_path):
    """在给定的基路径下查找最新的数字文件夹。"""
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

def main(input_path, output_path):
    # 处理输入和输出路径中的{t}占位符
    base_path = "/uploadPath/reasoning"
    if "{t}" in input_path or "{t}" in output_path:
        latest_folder = find_latest_folder(base_path)
        input_path = input_path.replace("{t}", latest_folder)
        output_path = output_path.replace("{t}", latest_folder)
        logging.debug(f"Resolved input_path: {input_path}")
        logging.debug(f"Resolved output_path: {output_path}")

    # 加载param.json
    try:
        with open(input_path, 'r', encoding='utf-8') as f:
            telnet_info = json.load(f)
        logging.info(f"Successfully loaded telnet_info from {input_path}")
    except FileNotFoundError:
        logging.error(f"param.json file not found at path: {input_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logging.error(f"Error decoding JSON from param.json: {e}")
        sys.exit(1)

    # 提取实验ID并构造UNL文件路径
    lab_id = telnet_info.get("labId")
    if not lab_id:
        logging.error("labId not found in param.json.")
        sys.exit(1)
    unl_file_path = f"/opt/unetlab/labs/{lab_id}.unl"

    # 解析UNL文件
    unl_parser = UNLParser(unl_file_path)
    try:
        unl_parser.parse()
    except Exception as e:
        logging.error(f"Failed to parse UNL file: {e}")
        sys.exit(1)

    # 管理Telnet连接
    telnet_manager = RouterTelnetManager(telnet_info)
    telnet_manager.connect_and_get_sysnames()

    # 拓扑映射
    mapper = TopologyMapper(unl_parser, telnet_manager)
    mapping = mapper.map_topology()

    # 将路由器信息写入临时文件
    temp_json_path = '/tmp/ospf_tmp.json'
    try:
        with open(temp_json_path, 'w', encoding='utf-8') as tmp_file:
            json.dump(mapping, tmp_file, indent=4, ensure_ascii=False)
        logging.info(f"Router information written to {temp_json_path}")
    except IOError as e:
        logging.error(f"Error writing to temporary file: {e}")
        sys.exit(1)

    # 初始化诊断实例
    ospf_diagnostic = OSPFDiagnostic(telnet_manager)
    isis_diagnostic = ISISDiagnostic(telnet_manager)

    all_faults = []

    # 遍历所有路由器，基于路由表判断使用的协议并执行相应的诊断
    for node_id, node_info in mapping.items():
        host_port = node_info['host_port']
        logging.info(f"\nProcessing router {host_port}...")

        # 获取路由表
        route_output = telnet_manager.send_command(host_port, 'display ip routing-table')
        if not route_output:
            logging.warning(f"Failed to retrieve routing table for {host_port}")
            continue

        # 解析路由表中的协议
        route_lines = route_output.splitlines()
        protocols_in_use = set()
        for line in route_lines:
            if line.strip() == "" or line.startswith("Destination/Mask") or line.startswith("-"):
                continue
            parts = line.split()
            if len(parts) < 5:
                continue
            proto = parts[1]
            if proto.startswith("OSPF"):
                protocols_in_use.add("OSPF")
            elif any(proto.startswith(p) for p in ['ISIS', 'ISIS-L1', 'ISIS-L2', 'ISIS-L1-L2']):
                protocols_in_use.add("ISIS")

        logging.info(f"Protocols detected for {host_port}: {protocols_in_use}")

        # 根据协议执行诊断
        if "OSPF" in protocols_in_use:
            ospf_diagnostic.collect_ospf_info(host_port)
            ospf_diagnostic.collect_route_table(host_port)
        if "ISIS" in protocols_in_use:
            isis_diagnostic.collect_isis_info(host_port)
            isis_diagnostic.collect_route_table(host_port)

    # 分析OSPF和ISIS故障
    ospf_faults = ospf_diagnostic.analyze_ospf_status()
    ospf_route_faults = ospf_diagnostic.analyze_route_table()
    isis_faults = isis_diagnostic.analyze_isis_status()
    isis_route_faults = isis_diagnostic.analyze_route_table()

    # 生成OSPF和ISIS诊断报告
    ospf_isis_report_generator = OSPFISISDiagnosticReport(ospf_faults, ospf_route_faults, isis_faults, isis_route_faults)
    ospf_isis_report = ospf_isis_report_generator.generate_report()

    # BGP诊断
    router_manager = RouterManager(telnet_info)
    router_manager.connect_and_get_bgp_errors()
    bgp_report = router_manager.generate_report_section()

    # 输出诊断报告
    write_diagnostic_report(output_path, ospf_isis_report, bgp_report)

    # 关闭所有Telnet连接
    telnet_manager.close_all()

if __name__ == "__main__":
    # 设置命令行参数解析
    parser = argparse.ArgumentParser(description="Process network topology from UNL and param.json, including OSPF, ISIS, and BGP diagnostics.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json, use {t} for latest folder number.")
    parser.add_argument("-o", "--output", required=True, help="Output path for diagnostic report, use {t} for latest folder number.")
    args = parser.parse_args()

    main(args.input, args.output)
