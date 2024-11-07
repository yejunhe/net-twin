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
    level=logging.INFO,  # Set to DEBUG for more detailed logs
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
    def __init__(self, telnet_info: Dict[str, Any], max_workers: int = 20, max_threads: int = 10):
        self.telnet_info = telnet_info
        self.telnet_sysnames: Dict[str, str] = {}
        self.telnet_configurations: Dict[str, List[Dict[str, str]]] = {}  # Structured interface info
        self.telnet_ospf_routes: Dict[str, List[str]] = {}  # OSPF routes per device
        self.telnet_router_ids: Dict[str, str] = {}  # Router IDs per device
        self.telnet_ospf_peers: Dict[str, List[str]] = {}  # OSPF neighbor Router IDs per device
        self.telnet_bgp_info: Dict[str, Dict[str, Any]] = {}  # BGP info per device
        self.network_connections: Optional[List[Dict[str, Any]]] = None
        self.node_interfaces: Dict[str, List[Dict[str, str]]] = {}  # Node interface info from UNL
        self.max_workers = max_workers
        self.max_threads = max_threads  # For NQA tests
        self.telnet_lock = Lock()
        # Define command sequences for different device types
        self.commands_map = {
            "huaweine40": (
                [
                    'scr 0 t',
                    'display ip interface brief',
                    'display ip routing-table ospf',
                    'display ospf peer',
                    'display bgp all summary'
                ],
                b'q\n'
            )
        }
        # Initialize NQA-related attributes
        self.performance_metrics: Dict[str, Dict[str, Any]] = {}
        self.performance_summaries: Dict[str, str] = {}
        # Thread pool executor for NQA tests
        self.nqa_executor = ThreadPoolExecutor(max_workers=self.max_threads)

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
                
                    # Parse 'display ip routing-table ospf' to get OSPF routes
                    if 'display ip routing-table ospf' in command_outputs:
                        ospf_routes = self.parse_display_ip_routing_table(command_outputs['display ip routing-table ospf'])
                        self.telnet_ospf_routes[key] = ospf_routes
                    else:
                        logging.warning(f"[{tn.host}:{tn.port}] Missing 'display ip routing-table ospf' output.")
                
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
                
                    # Parse 'display bgp all summary'
                    if 'display bgp all summary' in command_outputs:
                        bgp_output = command_outputs['display bgp all summary']
                        bgp_info = self.parse_display_bgp_all_summary(bgp_output)
                        if bgp_info:
                            self.telnet_bgp_info[key] = bgp_info
                    else:
                        logging.warning(f"[{tn.host}:{tn.port}] Missing 'display bgp all summary' output.")

                    # After gathering all configurations, perform NQA tests asynchronously
                    if self.telnet_ospf_routes.get(key):
                        self.nqa_executor.submit(self.perform_nqa_tests, key)
        else:
            logging.warning(f"[{tn.host}:{tn.port}] No output received from Telnet commands.")
        return command_outputs

    def parse_display_ip_routing_table(self, output: str) -> List[str]:
        """
        Parse the output of 'display ip routing-table ospf' to extract OSPF destination networks.

        :param output: Command output.
        :return: List of destination IPs (without subnet masks).
        """
        ospf_routes = []
        lines = output.splitlines()
        logging.debug("Parsing 'display ip routing-table ospf' output.")

        # Example line:
        # O        10.0.0.0/24 [110/2] via 192.168.1.2, Ethernet1/0/0, 00:00:12
        routing_entry_regex = re.compile(r'^O\s+(\d+\.\d+\.\d+\.\d+)/\d+\s+\[.*\]\s+via\s+(\d+\.\d+\.\d+\.\d+),\s+(\S+),.*$')

        for line in lines:
            match = routing_entry_regex.match(line.strip())
            if match:
                dest_ip = match.group(1)
                next_hop = match.group(2)
                interface = match.group(3)
                ospf_routes.append(dest_ip)
                logging.debug(f"Detected OSPF route: Destination={dest_ip}, Next-hop={next_hop}, Interface={interface}")

        logging.info(f"Parsed OSPF routes: {ospf_routes}")
        return ospf_routes

    def perform_nqa_tests(self, host_port: str):
        """
        Perform NQA tests on the router to evaluate network performance.

        :param host_port: Identifier for the router (host:port).
        """
        sysname = self.telnet_sysnames.get(host_port, "未知节点")
        ospf_routes = self.telnet_ospf_routes.get(host_port, [])

        if not ospf_routes:
            logging.info(f"[{host_port}] 未找到 OSPF 路由，跳过 NQA 测试。")
            return

        # For simplicity, perform NQA tests on all OSPF routes
        for dest_ip in ospf_routes:
            try:
                tn = telnetlib.Telnet(host=host_port.split(':')[0], port=int(host_port.split(':')[1]), timeout=10)
                tn.host, tn.port = host_port.split(':')[0], int(host_port.split(':')[1])
                logging.info(f"[{host_port}] 连接成功，开始执行 NQA 测试到 {dest_ip}")

                # 进入 system-view 模式
                tn.write(b'system-view\n')
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
                    tn.read_until(b']', timeout=3)

                # 发送命令开始测试
                tn.write(b'start now\n')
                tn.read_until(b']', timeout=3)

                # 尝试获取 NQA 测试结果
                max_attempts = 5
                attempt_count = 0
                result = ""
                while attempt_count < max_attempts:
                    tn.write(b'display nqa results test-instance admin perfor_test\n')
                    partial_output = tn.read_until(b'>', timeout=5).decode('ascii')
                    result += partial_output

                    if "The test is finished" in partial_output:
                        logging.info(f"[{host_port}] NQA 测试完成于尝试 {attempt_count + 1}")
                        break

                    attempt_count += 1
                    logging.info(f"[{host_port}] 尝试 {attempt_count}/{max_attempts} 获取 NQA 结果到 {dest_ip}...")

                    if attempt_count >= max_attempts:
                        logging.warning(f"[{host_port}] 达到最大尝试次数，NQA 测试结果可能不完整。")
                        break

                # 记录并解析结果
                logging.info(f"[{host_port}] NQA 测试结果到 {dest_ip}:\n{result}")
                metrics = self.parse_nqa_result(result)
                if metrics:
                    evaluation = self.evaluate_network_performance(metrics)
                    summary = self.generate_performance_summary(evaluation)
                    with self.telnet_lock:
                        self.performance_metrics[f"{host_port}:{dest_ip}"] = metrics
                        self.performance_summaries[f"{host_port}:{dest_ip}"] = summary
                    logging.info(f"[{host_port}] 性能评估: {summary}")
                else:
                    logging.warning(f"[{host_port}] 无法解析 NQA 测试结果到 {dest_ip}。")

                # 执行结束和清理命令
                tn.write(b'stop\n')
                tn.read_until(b']', timeout=3)

                tn.write(b'undo nqa test-instance admin perfor_test\n')
                tn.read_until(b']', timeout=3)

                tn.write(b'commit\n')
                tn.read_until(b']', timeout=3)

                tn.close()
            except Exception as e:
                logging.error(f"[{host_port}] 执行 NQA 测试到 {dest_ip} 时发生错误: {e}")

    def parse_nqa_result(self, nqa_result: str) -> Optional[Dict[str, Any]]:
        """解析 NQA 结果，提取性能指标。"""
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
                    logging.debug(f"解析延迟 (平均 RTT): {latency_avg} ms")
                except ValueError as e:
                    logging.error(f"无法解析延迟，行: '{line}'。错误: {e}")
                    metrics["latency"] = None

            # 解析抖动 (Jitter)
            jitter_match = jitter_pattern.search(line)
            if jitter_match:
                try:
                    jitter_value = float(jitter_match.group(1))
                    metrics["jitter"] = jitter_value
                    logging.debug(f"解析抖动 (平均): {jitter_value} ms")
                except ValueError as e:
                    logging.error(f"无法解析抖动，行: '{line}'。错误: {e}")
                    metrics["jitter"] = None

            # 解析丢包率 (Packet Loss Ratio)
            packet_loss_match = packet_loss_pattern.search(line)
            if packet_loss_match:
                try:
                    packet_loss = float(packet_loss_match.group(1))
                    metrics["packet_loss"] = packet_loss
                    logging.debug(f"解析丢包率: {packet_loss} %")
                except ValueError as e:
                    logging.error(f"无法解析丢包率，行: '{line}'。错误: {e}")
                    metrics["packet_loss"] = None

        # 如果所有指标都无法解析，记录原始 NQA 结果以便调试
        if all(value is None for value in metrics.values()):
            logging.warning("所有性能指标均为 None。原始 NQA 结果:")
            logging.warning(nqa_result)

        return metrics

    def evaluate_network_performance(self, metrics: Dict[str, Any]) -> Dict[str, str]:
        """根据性能指标评估网络性能，并以中文表述结果。"""
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
        """根据性能评估结果生成综合的网络性能评价。"""
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

    def parse_display_bgp_all_summary(self, output: str) -> Optional[Dict[str, Any]]:
        """
        Parse the output of 'display bgp all summary' to extract BGP information.

        :param output: Command output.
        :return: Dictionary containing BGP local Router ID, local AS number, total peers,
                 established peers, and list of non-established peers. Returns None if parsing fails.
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

        # Define regex patterns
        key_value_regex = re.compile(r'(\w+(?:\s+\w+)*)\s*:\s*(\d+)', re.IGNORECASE)
        # Define neighbor entry regex
        peer_entry_regex = re.compile(
            r'^(?P<peer_ip>\S+)\s+'
            r'(?P<peer_as>\d+)\s+'
            r'(?P<msg_rcvd>\d+)\s+'
            r'(?P<msg_sent>\d+)\s+'
            r'(?P<out_q>\d+)\s+'
            r'(?P<up_down>\S+)\s+'
            r'(?P<state>\S+)', re.IGNORECASE
        )

        in_peer_table = False  # Flag to indicate if parsing is in peer table

        for line in lines:
            # Extract key-value pairs
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

            # Identify start of peer table
            if line.strip().startswith("Peer"):
                in_peer_table = True
                continue

            if in_peer_table:
                # Identify end of peer table
                if re.match(r'^[-=]+$', line.strip()):
                    in_peer_table = False
                    continue

                # Parse peer entries
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

        # Check if parsing was successful
        if bgp_info["bgp_local_router_id"] and bgp_info["bgp_local_as_number"]:
            logging.info(f"Extracted BGP info: {bgp_info}")
            return bgp_info
        else:
            logging.warning("Failed to extract some BGP information from 'display bgp all summary' output.")
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

    def collect_results(self) -> Dict[str, Any]:
        """
        Collect all results, perform interface matching, and output interface configuration status including OSPF, ISIS, and BGP statuses.
        Additionally, consolidate all unconfigured information and provide suggestions on which nodes need protocol configuration.
        """
        # Store interface status information
        interface_status = {}
        ospf_status = {}
        bgp_info_dict = {}
        recommendations = {
            "未配置 OSPF 接口": [],
            "未配置 BGP": [],
            "接口缺少 IP 配置": []
        }

        for host_port, sysname in self.telnet_sysnames.items():
            ospf_routes = self.telnet_ospf_routes.get(host_port, [])
            telnet_interfaces = self.telnet_configurations.get(host_port, [])
            ospf_neighbors = self.telnet_ospf_peers.get(host_port, [])
            bgp_info = self.telnet_bgp_info.get(host_port)

            # Extract interface names from Telnet and convert to lowercase for comparison
            telnet_interface_names = [iface['Interface'].lower() for iface in telnet_interfaces if isinstance(iface, dict)]
            logging.debug(f"[{host_port}] Telnet fetched interfaces: {telnet_interface_names}")

            interface_status[host_port] = []

            for iface in telnet_interfaces:
                iface_name = iface['Interface']
                iface_formatted = normalize_interface_name(iface_name)
                iface_formatted_lower = iface_formatted.lower()

                # Determine if interface has IP configured
                has_ip = iface['IP Address/Mask'].lower() != 'unassigned'
                config_status = "已配置IP地址" if has_ip else "未配置IP地址"
                if not has_ip:
                    recommendations["接口缺少 IP 配置"].append(f"{sysname} ({host_port}) - {iface_formatted}")

                # Determine OSPF configuration status
                ospf_iface_status = "OSPF已配置" if iface_formatted_lower in [route.lower() for route in ospf_routes] else "OSPF未配置"
                if ospf_iface_status == "OSPF未配置":
                    recommendations["未配置 OSPF 接口"].append(f"{sysname} ({host_port}) - {iface_formatted}")

                status = f"{iface_formatted}接口配置状态: {config_status}, {ospf_iface_status}"
                interface_status[host_port].append(status)
                logging.info(f"[{host_port}] {status}")

            # Determine OSPF status based on 'display ospf peer' and OSPF routes
            if ospf_neighbors:
                neighbors_str = ', '.join(ospf_neighbors)
                ospf_status[host_port] = f"OSPF 配置正常，邻居 Router IDs: {neighbors_str}"
            else:
                ospf_status[host_port] = "OSPF 未配置或无邻居"
                if ospf_routes:
                    recommendations["未配置 OSPF 接口"].append(f"{sysname} ({host_port}) - OSPF 已配置但无邻居")

            # Extract BGP info
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

                # 如果 BGP 未配置或没有 peers，则添加建议
                if bgp_total_peers == 0:
                    recommendations["未配置 BGP"].append(f"{sysname} ({host_port})")
            else:
                bgp_info_dict[host_port] = {
                    "bgp_local_router_id": "未知",
                    "bgp_local_as_number": "未知",
                    "bgp_total_peers": 0,
                    "bgp_established_peers": 0,
                    "bgp_non_established_peers": []
                }
                recommendations["未配置 BGP"].append(f"{sysname} ({host_port})")

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

            ospf_status_text = ospf_status.get(host_port, "未配置 OSPF")
            bgp_info_data = bgp_info_dict.get(host_port, {})
            performance_summary = self.performance_summaries.get(host_port, "未进行性能评估。")

            # Store in interface_status dictionary
            interface_status[host_port].append(f"OSPF 状态: {ospf_status_text}")
            interface_status[host_port].append(f"BGP 信息: {json.dumps(bgp_info_data, ensure_ascii=False)}")
            interface_status[host_port].append(f"网络性能评估: {performance_summary}")

        return {
            "telnet_devices": {
                host_port: {
                    "sysname": sysname,
                    "ospf_routes": self.telnet_ospf_routes.get(host_port, []),
                    "ospf_neighbors": self.telnet_ospf_peers.get(host_port, []),
                    "interfaces": self.telnet_configurations.get(host_port, []),
                    "ospf_status": ospf_status.get(host_port, "未配置 OSPF"),
                    "bgp_info": bgp_info_dict.get(host_port, {}),
                    "performance_metrics": self.performance_metrics.get(host_port, {}),
                    "performance_summary": self.performance_summaries.get(host_port, "未进行性能评估。"),
                    "interface_status": interface_status.get(host_port, [])
                }
                for host_port, sysname in self.telnet_sysnames.items()
            },
            "network_connections": self.network_connections,
            "recommendations": recommendations
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

    def find_latest_folder(self, base_path: str) -> str:
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

    def load_telnet_info(self, input_path: str) -> Dict[str, Any]:
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

    def write_output(self, output_path: str, data: Dict[str, Any]):
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)  # Use ensure_ascii=False to support Chinese
            logging.info(f"Mapping results written to {output_path}")
        except IOError as e:
            logging.error(f"Error writing to output file: {e}")
            sys.exit(1)

    def write_interface_status(self, data_txt_path: str, mapping: Dict[str, Any]):
        """
        Write interface status and protocol status to data.txt in the following format:
        Node: sysname1 (host:port)
            Interface: Ethernet1/0/0接口配置状态: 已配置IP地址, OSPF已配置
            Interface: Ethernet1/0/1接口配置状态: 未配置IP地址, OSPF未配置
            OSPF Status: OSPF 配置正常，邻居 Router IDs: 2.2.2.2, 1.1.1.1
            BGP Information: {...}
            网络性能评估: 该设备网络性能良好。

        Recommendations:
            - 未配置 OSPF 接口:
                - Router1 (192.168.1.1:23) - Ethernet1/0/1
            - 未配置 BGP:
                - Router2 (192.168.1.2:23)
            - 接口缺少 IP 配置:
                - Router3 (192.168.1.3:23) - Ethernet1/0/2
        """
        try:
            with open(data_txt_path, 'w', encoding='utf-8') as f:
                telnet_devices = mapping.get("telnet_devices", {})
                for host_port, device_info in telnet_devices.items():
                    sysname = device_info.get("sysname", "未知节点")
                    interface_status_list = device_info.get("interface_status", [])
                    ospf_status = device_info.get("ospf_status", "未配置 OSPF")
                    bgp_info = device_info.get("bgp_info", {})
                    performance_summary = device_info.get("performance_summary", "未进行性能评估。")

                    f.write(f"节点: {sysname} ({host_port})\n")
                    for status in interface_status_list:
                        f.write(f"    接口: {status}\n")
                    f.write(f"    OSPF 状态: {ospf_status}\n")

                    # Write BGP information
                    if bgp_info and bgp_info.get("bgp_local_router_id") != "未知":
                        f.write(f"    BGP 信息:\n")
                        f.write(f"        本地 Router ID: {bgp_info.get('bgp_local_router_id')}\n")
                        f.write(f"        本地 AS Number: {bgp_info.get('bgp_local_as_number')}\n")
                        f.write(f"        总邻居数量: {bgp_info.get('bgp_total_peers')}\n")
                        f.write(f"        建立状态的邻居数量: {bgp_info.get('bgp_established_peers')}\n")
                        non_established_peers = bgp_info.get("bgp_non_established_peers", [])
                        if non_established_peers:
                            f.write(f"        未建立状态的邻居:\n")
                            for peer in non_established_peers:
                                peer_ip = peer.get("peer_ip", "未知")
                                peer_as = peer.get("peer_as", "未知")
                                state = peer.get("state", "未知")
                                f.write(f"            Peer IP: {peer_ip}, AS: {peer_as}, State: {state}\n")
                        else:
                            f.write(f"        未建立状态的邻居: 无\n")
                    else:
                        f.write(f"    BGP 信息: 未配置\n")

                    # Write Performance Summary
                    f.write(f"    网络性能评估: {performance_summary}\n")

                    f.write("\n")  # Add empty line between devices

                # Write Recommendations
                recommendations = mapping.get("recommendations", {})
                f.write("建议:\n")
                for category, items in recommendations.items():
                    if items:
                        f.write(f"    - {category}:\n")
                        for item in items:
                            f.write(f"        - {item}\n")
                logging.info(f"接口状态已写入 {data_txt_path}")
        except IOError as e:
            logging.error(f"写入 {data_txt_path} 时出错: {e}")
            sys.exit(1)

    def process_router(self, node: Dict[str, Any]) -> Dict[str, Any]:
        """处理单个路由器的连接和测试，返回结果字典。"""
        host = node.get("hostip")
        port = node.get("port")
        result_data = {"host": host, "port": port, "sysname": None, "ospf_routes": None, "ospf_peers": None, "bgp_info": None, "performance_metrics": None, "performance_summary": None, "interface_status": None}

        if not host or not port:
            logging.warning(f"无效的节点配置: {node}")
            result_data["performance_summary"] = "无效的节点配置。"
            return result_data

        try:
            tn = telnetlib.Telnet(host, port, timeout=10)
            tn.host, tn.port = host, port
            # Get sysname and configurations
            self.get_configuration_via_telnet(tn, node.get("image_type", "").lower())
            tn.close()
        except Exception as e:
            logging.error(f"无法连接到 {host}:{port} - {e}")
            result_data["performance_summary"] = "无法连接到路由器，无法进行网络性能评估。"
            return result_data

        key = f"{host}:{port}"
        sysname = self.telnet_sysnames.get(key)
        if sysname:
            result_data["sysname"] = sysname
            result_data["ospf_routes"] = self.telnet_ospf_routes.get(key, [])
            result_data["ospf_peers"] = self.telnet_ospf_peers.get(key, [])
            result_data["bgp_info"] = self.telnet_bgp_info.get(key, {})
            result_data["interface_status"] = self.telnet_configurations.get(key, [])

            # Collect performance metrics and summaries
            performance_metrics = self.performance_metrics.get(key, {})
            performance_summary = self.performance_summaries.get(key, "未进行性能评估。")
            result_data["performance_metrics"] = performance_metrics
            result_data["performance_summary"] = performance_summary
        else:
            logging.warning(f"无法检索 {host}:{port} 的 sysname")
            result_data["performance_summary"] = "无法检索 sysname，无法进行网络性能评估。"

        return result_data

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

    def main(self, input_path: str, output_path: str):
        base_path = "/uploadPath/reasoning"
        if "{t}" in input_path or "{t}" in output_path:
            latest_folder = self.find_latest_folder(base_path)
            input_path = input_path.replace("{t}", latest_folder)
            output_path = output_path.replace("{t}", latest_folder)
            logging.debug(f"Resolved input_path: {input_path}")
            logging.debug(f"Resolved output_path: {output_path}")

        telnet_info = self.load_telnet_info(input_path)
        self.telnet_info = telnet_info

        # Read UNL file based on labId
        lab_id = telnet_info.get("labId")
        if lab_id is not None:
            self.read_unl_file(lab_id)
        else:
            logging.warning("labId not found in telnet_info.")

        self.connect_and_get_sysnames_and_configs()
        mapping = self.collect_results()

        logging.info("Collected router configurations:")
        logging.info(json.dumps(mapping, indent=4, ensure_ascii=False))
        self.write_output(output_path, mapping)

        # Define the path for data.txt, placed in the same directory as output_path
        output_dir = os.path.dirname(output_path)
        data_txt_path = os.path.join(output_dir, "data.txt")
        self.write_interface_status(data_txt_path, mapping)

if __name__ == "__main__":
    # 设置参数解析
    parser = argparse.ArgumentParser(description="Process router configurations from param.json and perform network performance evaluation.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json, use {t} for latest folder number.")
    parser.add_argument("-o", "--output", required=True, help="Output path for process information, use {t} for latest folder number.")
    args = parser.parse_args()

    # Initialize RouterManager and execute main functionality
    router_manager = RouterManager({})
    router_manager.main(args.input, args.output)
