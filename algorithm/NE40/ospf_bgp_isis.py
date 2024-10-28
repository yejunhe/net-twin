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
from datetime import datetime
import xml.etree.ElementTree as ET

# 配置日志记录
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)


class IsisFaultDetector:
    def __init__(self, tn: telnetlib.Telnet, host: str, port: int):
        self.tn = tn
        self.host = host
        self.port = port
        self.fault_info = {
            "total_peers": 0,
            "l1_lsp_overflow": False,
            "l2_lsp_overflow": False,
            "level1_avoid_redistribute_loop": False,
            "level2_avoid_redistribute_loop": False,
            "ipv4_state_issues": []
        }

    def send_command(self, command: str) -> str:
        try:
            self.tn.write(command.encode('ascii') + b'\n')
            time.sleep(1)
            output = self.tn.read_very_eager().decode('ascii', errors='ignore')
            logging.debug(f"[{self.host}:{self.port}] Command '{command}' output:\n{output}")
            return output
        except Exception as e:
            logging.error(f"[{self.host}:{self.port}] Error sending command '{command}': {e}")
            return ""

    def parse_display_isis_peer(self, output: str):
        total_peers = 0
        for line in output.splitlines():
            if line.strip().startswith("Total Peer(s):"):
                try:
                    total_peers = int(line.strip().split(":")[-1])
                    self.fault_info["total_peers"] = total_peers
                    logging.info(f"[{self.host}:{self.port}] Total ISIS Peers: {total_peers}")
                except ValueError:
                    logging.error(f"[{self.host}:{self.port}] Unable to parse total peers from line: {line}")
        if total_peers == 0:
            logging.warning(f"[{self.host}:{self.port}] No ISIS peers detected.")

    def parse_display_isis_brief(self, output: str):
        for line in output.splitlines():
            if "L1 Lsp Over Flow:" in line:
                self.fault_info["l1_lsp_overflow"] = "true" in line.lower()
            elif "L2 Lsp Over Flow:" in line:
                self.fault_info["l2_lsp_overflow"] = "true" in line.lower()
            elif "Level-1 Avoid Redistribute Loop Capability:" in line:
                self.fault_info["level1_avoid_redistribute_loop"] = "true" in line.lower()
            elif "Level-2 Avoid Redistribute Loop Capability:" in line:
                self.fault_info["level2_avoid_redistribute_loop"] = "true" in line.lower()
        logging.info(f"[{self.host}:{self.port}] Parsed ISIS brief information.")

    def parse_display_isis_interface(self, output: str):
        ipv4_state_issues = []
        lines = output.splitlines()
        header_found = False
        for line in lines:
            if "Interface information for ISIS" in line:
                header_found = False  # Reset for new table
            if "Interface" in line and "IPV4.State" in line:
                header_found = True
                continue
            if header_found:
                if line.strip() == "":
                    break
                parts = line.split()
                if len(parts) >= 4:
                    interface = parts[0]
                    ipv4_state = parts[2]
                    if "DN" in ipv4_state.upper() or "DOWN" in ipv4_state.upper():
                        ipv4_state_issues.append({
                            "interface": interface,
                            "ipv4_state": ipv4_state
                        })
        self.fault_info["ipv4_state_issues"] = ipv4_state_issues
        if ipv4_state_issues:
            logging.warning(f"[{self.host}:{self.port}] IPV4 State issues detected: {ipv4_state_issues}")
        else:
            logging.info(f"[{self.host}:{self.port}] No IPV4 State issues detected.")

    def detect_faults(self):
        # 1. display isis peer
        peer_output = self.send_command("display isis peer")
        self.parse_display_isis_peer(peer_output)

        # 2. display isis brief
        brief_output = self.send_command("display isis brief")
        self.parse_display_isis_brief(brief_output)

        # 3. display isis interface
        interface_output = self.send_command("display isis interface")
        self.parse_display_isis_interface(interface_output)

        return self.fault_info


class UNLParser:
    def __init__(self, unl_file):
        self.unl_file = unl_file
        self.nodes = {}
        self.networks = {}

    def parse(self):
        """Parse the .unl file to extract node and network information."""
        try:
            tree = ET.parse(self.unl_file)
            root = tree.getroot()

            # Extract node information
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

            # Extract network information
            for network in root.findall(".//network"):
                network_id = network.get('id')
                network_name = network.get('name')
                self.networks[network_id] = network_name

            logging.info("Parsed UNL File:")
            logging.info(f"Nodes: {self.nodes}")
            logging.info(f"Networks: {self.networks}")
        except Exception as e:
            logging.error(f"Error parsing UNL file {self.unl_file}: {e}")
            sys.exit(1)


class OSPFDiagnostic:
    def __init__(self, tn: telnetlib.Telnet, host: str, port: int):
        self.tn = tn
        self.host = host
        self.port = port
        self.ospf_info = {
            'peer': [],
            'interface': [],
            'brief': []
        }
        self.faults = []

    def send_command(self, command: bytes, wait_prompt=b'>', timeout=5) -> str:
        try:
            self.tn.write(command + b'\n')
            output = self.tn.read_until(wait_prompt, timeout).decode('ascii', errors='ignore')
            logging.debug(f"[{self.host}:{self.port}] Command '{command.decode()}' output:\n{output}")
            return output
        except Exception as e:
            logging.error(f"[{self.host}:{self.port}] Error sending command '{command.decode()}': {e}")
            return ""

    def collect_ospf_info(self):
        """Collect OSPF information from the router."""
        # Disable paging if necessary
        self.send_command(b'set cli page-length disable')

        # Collect OSPF neighbor state
        peer_output = self.send_command(b'display ospf peer')
        self.ospf_info['peer'] = self.parse_output(peer_output)

        # Collect OSPF interface state
        interface_output = self.send_command(b'display ospf interface')
        self.ospf_info['interface'] = self.parse_output(interface_output)

        # Collect OSPF brief
        brief_output = self.send_command(b'display ospf brief')
        self.ospf_info['brief'] = self.parse_output(brief_output)

    @staticmethod
    def parse_output(output: str) -> List[str]:
        """Parse the command output to extract meaningful information."""
        return output.splitlines()

    def analyze_ospf_status(self):
        """Analyze the collected OSPF information for possible faults."""
        logging.info(f"[{self.host}:{self.port}] Analyzing OSPF status...")
        
        # Analyze OSPF peer status
        for line in self.ospf_info['peer']:
            if any(state in line for state in ['Init', 'Down', 'Attempt']):
                fault = f"OSPF邻居问题检测到 {self.host}:{self.port}: {line}"
                self.faults.append(fault)
                logging.warning(fault)

        # Analyze OSPF interface status
        for line in self.ospf_info['interface']:
            if "state" in line.lower():
                if any(bad_state in line for bad_state in ['Down', 'Init', 'Attempt', 'ExStart', 'Loading']):
                    fault = f"OSPF接口异常在 {self.host}:{self.port}: {line}"
                    self.faults.append(fault)
                    logging.warning(fault)
                elif "state: " in line.lower() and "full" not in line.lower():
                    fault = f"OSPF接口状态非Full在 {self.host}:{self.port}: {line}"
                    self.faults.append(fault)
                    logging.warning(fault)

        # Analyze OSPF brief information for interface states
        capture_interface_info = False
        for line in self.ospf_info['brief']:
            if "Interface" in line and "IP Address" in line:
                capture_interface_info = True
                continue
            if capture_interface_info and line.strip():
                interface_details = line.split()
                if len(interface_details) >= 5:
                    interface_name = interface_details[0]
                    interface_ip = interface_details[1]
                    interface_type = interface_details[2]
                    interface_state = interface_details[3]

                    if interface_state.lower() == "down":
                        fault = f"OSPF概要信息接口异常在 {self.host}:{self.port}: 接口 {interface_name} ({interface_ip}) 状态Down"
                        self.faults.append(fault)
                        logging.warning(fault)
                    elif interface_state.lower() not in ["full", "p-2-p", "bdr", "dr"]:
                        fault = f"OSPF概要信息接口状态异常在 {self.host}:{self.port}: 接口 {interface_name} ({interface_ip}) 状态 {interface_state}"
                        self.faults.append(fault)
                        logging.warning(fault)

        logging.info(f"[{self.host}:{self.port}] OSPF状态分析完成。")

        return self.faults


class RouterManager:
    def __init__(self, telnet_info: Dict[str, Any], max_workers: int = 20):
        self.telnet_info = telnet_info
        self.telnet_sysnames: Dict[str, str] = {}
        self.telnet_configurations: Dict[str, str] = {}
        self.isis_faults: Dict[str, Any] = {}
        self.ospf_faults: Dict[str, List[str]] = {}
        self.max_workers = max_workers
        self.telnet_lock = Lock()
        # 定义不同设备类型的命令序列
        self.commands_map = {
            "huaweine40": (['scr 0 t', 'display ip routing-table'], b'q\n')
        }

    def execute_telnet_commands(self, tn: telnetlib.Telnet, commands: list, quit_cmd: bytes) -> str:
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
        # 根据image_type匹配设备类型
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
                    self.telnet_configurations[key] = output
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
                        future = executor.submit(self.process_node, tn, node)
                        future_to_node[future] = node
                    except Exception as e:
                        logging.error(f"Failed to connect to {host}:{port} via Telnet: {e}")

            for future in as_completed(future_to_node):
                node = future_to_node[future]
                host, port = node.get("hostip"), node.get("port")
                try:
                    future.result()
                    logging.info(f"[{host}:{port}] Configuration and ISIS fault retrieval successful.")
                except Exception as e:
                    logging.error(f"[{host}:{port}] Error processing node: {e}")
                    logging.info(f"[{host}:{port}] Configuration and ISIS fault retrieval failed.")

    def process_node(self, tn: telnetlib.Telnet, node: Dict[str, Any]):
        host, port = node.get("hostip"), node.get("port")
        image_type = node.get("image_type", "").lower()
        config_output = self.get_configuration_via_telnet(tn, image_type)
        if config_output:
            self.detect_isis_faults(tn, host, port, config_output)
            self.detect_ospf_faults(tn, host, port)

    def detect_isis_faults(self, tn: telnetlib.Telnet, host: str, port: int, routing_table_output: str):
        # 检查路由表中是否存在ISIS相关字段
        if any(protocol in routing_table_output for protocol in ["ISIS-L1", "ISIS-L2", "ISIS-L1-L2"]):
            logging.info(f"[{host}:{port}] ISIS protocols detected in routing table. Proceeding with fault detection.")
            isis_detector = IsisFaultDetector(tn, host, port)
            fault_info = isis_detector.detect_faults()
            key = f"{host}:{port}"
            with self.telnet_lock:
                self.isis_faults[key] = fault_info
        else:
            logging.info(f"[{host}:{port}] No ISIS protocols detected in routing table. Skipping ISIS fault detection.")

    def detect_ospf_faults(self, tn: telnetlib.Telnet, host: str, port: int):
        # 检查路由表中是否存在OSPF相关字段
        tn.write(b'display ip routing-table | include OSPF\n')
        routing_table_output = tn.read_very_eager().decode('ascii', errors='ignore')
        if any(protocol in routing_table_output for protocol in ["OSPF"]):
            logging.info(f"[{host}:{port}] OSPF protocols detected in routing table. Proceeding with OSPF diagnostics.")
            ospf_diagnostic = OSPFDiagnostic(tn, host, port)
            ospf_diagnostic.collect_ospf_info()
            faults = ospf_diagnostic.analyze_ospf_status()
            key = f"{host}:{port}"
            with self.telnet_lock:
                self.ospf_faults[key] = faults
        else:
            logging.info(f"[{host}:{port}] No OSPF protocols detected in routing table. Skipping OSPF diagnostics.")

    def collect_results(self) -> Dict[str, Any]:
        return {
            "telnet_devices": {
                host_port: {
                    "sysname": sysname,
                    "configuration": self.telnet_configurations.get(host_port, 'No config'),
                    "isis_fault_info": self.isis_faults.get(host_port, 'No ISIS Fault Information'),
                    "ospf_fault_info": self.ospf_faults.get(host_port, 'No OSPF Fault Information')
                }
                for host_port, sysname in self.telnet_sysnames.items()
            }
        }

    def generate_report(self, output_path: str):
        report_lines = []
        report_lines.append("========================================")
        report_lines.append("             网络故障检测报告")
        report_lines.append("========================================")
        execution_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        report_lines.append(f"执行时间：{execution_time}")
        report_lines.append("----------------------------------------")

        # 确定整体执行结果
        execution_result = "成功" if self.isis_faults or self.ospf_faults else "未执行故障检测"
        report_lines.append(f"执行结果：{execution_result}")
        report_lines.append("----------------------------------------")
        report_lines.append("执行内容：")

        # ISIS故障检测部分
        if self.isis_faults:
            report_lines.append("=== ISIS故障检测 ===")
            for host_port, fault_info in self.isis_faults.items():
                sysname = self.telnet_sysnames.get(host_port, "未知设备")
                report_lines.append(f"设备: {sysname} ({host_port})")
                report_lines.append(f"  1. 总邻居数量: {fault_info.get('total_peers', 0)}")
                report_lines.append(f"  2. L1 LSP溢出: {'是' if fault_info.get('l1_lsp_overflow') else '否'}")
                report_lines.append(f"  3. L2 LSP溢出: {'是' if fault_info.get('l2_lsp_overflow') else '否'}")
                report_lines.append(
                    f"  4. Level-1 Avoid Redistribute Loop Capability: {'启用' if fault_info.get('level1_avoid_redistribute_loop') else '未启用'}")
                report_lines.append(
                    f"  5. Level-2 Avoid Redistribute Loop Capability: {'启用' if fault_info.get('level2_avoid_redistribute_loop') else '未启用'}")

                ipv4_issues = fault_info.get('ipv4_state_issues', [])
                if ipv4_issues:
                    report_lines.append(f"  6. IPV4 State 存在问题的接口:")
                    for issue in ipv4_issues:
                        report_lines.append(f"     - 接口: {issue['interface']}, IPV4.State: {issue['ipv4_state']}")
                else:
                    report_lines.append("  6. IPV4 State 无问题接口。")

                report_lines.append("----------------------------------------")
        else:
            report_lines.append("未检查到ISIS相关配置，跳过ISIS故障检测。")

        # OSPF故障检测部分
        if self.ospf_faults:
            report_lines.append("=== OSPF故障检测 ===")
            for host_port, faults in self.ospf_faults.items():
                sysname = self.telnet_sysnames.get(host_port, "未知设备")
                report_lines.append(f"设备: {sysname} ({host_port})")
                if faults:
                    for fault in faults:
                        report_lines.append(f"  - {fault}")
                else:
                    report_lines.append("  未检测到OSPF故障。")
                report_lines.append("----------------------------------------")
        else:
            report_lines.append("未检查到OSPF相关配置，跳过OSPF故障检测。")

        # 写入报告文件
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                for line in report_lines:
                    f.write(line + '\n')
            logging.info(f"报告已成功写入到 {output_path}")
        except IOError as e:
            logging.error(f"写入报告文件时出错: {e}")
            sys.exit(1)


class TopologyMapper:
    def __init__(self, unl_parser: UNLParser, telnet_manager: RouterManager):
        self.unl_parser = unl_parser
        self.telnet_manager = telnet_manager

    def map_topology(self):
        """Map the sysnames retrieved from Telnet to the nodes in the UNL topology."""
        mapping = {}
        for node_id, node_info in self.unl_parser.nodes.items():
            node_name = node_info['name']
            for host_port, sysname in self.telnet_manager.telnet_sysnames.items():
                if node_name == sysname:
                    mapping[node_id] = {
                        'node_name': node_name,
                        'host_port': host_port,
                        'sysname': sysname
                    }

        logging.info("Mapping between UNL topology and Telnet sysnames:")
        logging.info(mapping)
        return mapping


class UNLTopologyManager:
    def __init__(self, unl_file: str, telnet_info: Dict[str, Any]):
        self.unl_parser = UNLParser(unl_file)
        self.telnet_manager = RouterManager(telnet_info)

    def process_topology(self):
        # 解析UNL文件
        self.unl_parser.parse()

        # 获取sysnames和配置
        self.telnet_manager.connect_and_get_sysnames_and_configs()

        # 映射拓扑
        mapper = TopologyMapper(self.unl_parser, self.telnet_manager)
        mapping = mapper.map_topology()

        # 写入映射结果到临时文件
        try:
            with open('/tmp/network_mapping.json', 'w', encoding='utf-8') as tmp_file:
                json.dump(mapping, tmp_file, indent=4, ensure_ascii=False)
            logging.info("Router information written to /tmp/network_mapping.json")
        except IOError as e:
            logging.error(f"写入临时文件时出错: {e}")
            sys.exit(1)

    def generate_combined_report(self, output_path: str):
        """生成包含ISIS和OSPF故障检测的综合报告。"""
        self.telnet_manager.generate_report(output_path)


def find_latest_folder(base_path: str) -> str:
    try:
        all_folders = [f for f in os.listdir(base_path) if f.isdigit()]
        if not all_folders:
            raise ValueError("在基础路径中未找到编号文件夹。")
        latest_folder = max(all_folders, key=int)
        logging.info(f"最新文件夹已识别: {latest_folder}")
        return latest_folder
    except FileNotFoundError:
        logging.error(f"基础路径未找到: {base_path}")
        sys.exit(1)
    except ValueError as ve:
        logging.error(ve)
        sys.exit(1)


def load_telnet_info(input_path: str) -> Dict[str, Any]:
    try:
        with open(input_path, 'r', encoding='utf-8') as f:
            telnet_info = json.load(f)
        logging.info(f"成功从 {input_path} 加载 telnet_info")
        return telnet_info
    except FileNotFoundError:
        logging.error(f"param.json 文件未在路径中找到: {input_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logging.error(f"从 param.json 解码 JSON 时出错: {e}")
        sys.exit(1)


def write_output_report(output_path: str, topology_manager: UNLTopologyManager):
    # 生成综合报告
    topology_manager.generate_combined_report(output_path)


def main(input_path: str, output_path: str):
    base_path = "/uploadPath/reasoning"
    if "{t}" in input_path or "{t}" in output_path:
        latest_folder = find_latest_folder(base_path)
        input_path = input_path.replace("{t}", latest_folder)
        output_path = output_path.replace("{t}", latest_folder)
        logging.debug(f"已解析 input_path: {input_path}")
        logging.debug(f"已解析 output_path: {output_path}")

    telnet_info = load_telnet_info(input_path)

    # 提取实验ID并构建UNL文件路径
    lab_id = telnet_info.get("labId")
    if not lab_id:
        logging.error("telnet_info 中未找到 'labId'。")
        sys.exit(1)
    unl_file_path = f"/opt/unetlab/labs/{lab_id}.unl"

    # 初始化并处理拓扑
    topology_manager = UNLTopologyManager(unl_file_path, telnet_info)
    topology_manager.process_topology()

    # 生成并写入报告
    write_output_report(output_path, topology_manager)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="处理 param.json 中的路由器配置并检测 ISIS 和 OSPF 故障。")
    parser.add_argument("-i", "--input", required=True, help="param.json 文件的路径，使用 {t} 代表最新文件夹编号。")
    parser.add_argument("-o", "--output", required=True, help="报告输出文件的路径，使用 {t} 代表最新文件夹编号。")
    args = parser.parse_args()
    main(args.input, args.output)
