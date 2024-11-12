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

# Configure logging for better traceability and control
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)


class RouterManager:
    def __init__(self, telnet_info: Dict[str, Any], max_workers: int = 20):
        self.telnet_info = telnet_info
        self.telnet_sysnames: Dict[str, str] = {}
        self.telnet_interfaces: Dict[str, Dict[str, str]] = {}  # 存储路由器接口和IP的映射
        self.telnet_routing_tables: Dict[str, List[Dict[str, str]]] = {}  # 存储路由表信息
        self.max_workers = max_workers
        self.telnet_lock = Lock()
        # Define command sequences for different device types
        self.commands_map = {
            "huaweine40": (['scr 0 t', 'display ip interface brief', 'display ip routing-table'], b'q\n')
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
                # 增加等待时间以确保命令执行完成
                if cmd == 'display ip interface brief':
                    time.sleep(3)
                elif cmd == 'display ip routing-table':
                    time.sleep(4)
                else:
                    time.sleep(2)
                cmd_output = tn.read_very_eager().decode('ascii', errors='ignore')
                output += cmd_output
                logging.debug(f"[{tn.host}:{tn.port}] Output for '{cmd}':\n{cmd_output}")

            prompt = self.get_prompt(tn)
            if prompt and not ((prompt.startswith('<') and prompt.endswith('>')) or
                              (prompt.startswith('[') and prompt.endswith(']'))):
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
                line = line.strip()
                if (line.startswith('<') and line.endswith('>')) or (line.startswith('[') and line.endswith(']')):
                    # 去除尖括号或方括号
                    if line.startswith('<') and line.endswith('>'):
                        sysname = line.strip('<> ').strip()
                    else:
                        sysname = line.strip('[] ').strip()
                    logging.info(f"[{tn.host}:{tn.port}] Detected sysname: {sysname}")
                    return sysname
            logging.warning(f"[{tn.host}:{tn.port}] No sysname detected.")
            return None
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] Telnet Error while getting sysname: {e}")
            return None

    def get_interfaces_via_telnet(self, tn: telnetlib.Telnet, sysname: str) -> Optional[Dict[str, str]]:
        try:
            tn.write(b'display ip interface brief\n')
            logging.info(f"[{tn.host}:{tn.port}] Sending command: display ip interface brief")
            time.sleep(3)  # 增加等待时间以确保命令执行完成
            output = tn.read_very_eager().decode('ascii', errors='ignore')
            logging.debug(f"[{tn.host}:{tn.port}] Output for 'display ip interface brief':\n{output}")

            interfaces = {}
            parsing = False
            headers = {}
            for line in output.splitlines():
                line = line.rstrip()
                if not parsing:
                    if line.startswith("Interface") and "IP Address/Mask" in line:
                        parsing = True
                        # 记录每个字段的起始位置
                        headers = self.parse_headers(line)
                        continue
                else:
                    if not line or line.startswith("-") or line.startswith("The number of"):
                        continue
                    interface = self.extract_field(line, headers, 'Interface')
                    ip_address = self.extract_field(line, headers, 'IP Address/Mask')
                    # physical = self.extract_field(line, headers, 'Physical')  # 可选
                    # protocol = self.extract_field(line, headers, 'Protocol')  # 可选
                    # vpn = self.extract_field(line, headers, 'VPN')  # 可选
                    if interface and ip_address != "unassigned":
                        interfaces[interface] = ip_address
            with self.telnet_lock:
                self.telnet_interfaces[sysname] = interfaces
            return interfaces
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] Error getting interfaces: {e}")
            return None

    def parse_headers(self, header_line: str) -> Dict[str, int]:
        """
        解析表头行，返回每个字段的起始位置
        """
        headers = {}
        fields = ["Interface", "IP Address/Mask", "Physical", "Protocol", "VPN"]
        for field in fields:
            index = header_line.find(field)
            if index != -1:
                headers[field] = index
        return headers

    def extract_field(self, line: str, headers: Dict[str, int], field_name: str) -> str:
        """
        根据表头的起始位置，从数据行中提取对应字段的值
        """
        start = headers.get(field_name, -1)
        if start == -1:
            return ""
        # Determine the end position
        fields_sorted = sorted(headers.items(), key=lambda x: x[1])
        field_names_sorted = [field for field, pos in fields_sorted]
        current_index = field_names_sorted.index(field_name)
        if current_index + 1 < len(field_names_sorted):
            next_field = field_names_sorted[current_index + 1]
            end = headers[next_field]
        else:
            end = len(line)
        return line[start:end].strip()

    def get_routing_table_via_telnet(self, tn: telnetlib.Telnet, sysname: str) -> Optional[List[Dict[str, str]]]:
        try:
            tn.write(b'display ip routing-table\n')
            logging.info(f"[{tn.host}:{tn.port}] Sending command: display ip routing-table")
            time.sleep(4)  # 增加等待时间以确保命令执行完成
            output = tn.read_very_eager().decode('ascii', errors='ignore')
            logging.debug(f"[{tn.host}:{tn.port}] Output for 'display ip routing-table':\n{output}")

            routing_table = []
            parsing = False
            headers = {}
            for line in output.splitlines():
                line = line.rstrip()
                if not parsing:
                    if line.startswith("Destination/Mask") and "NextHop" in line and "Interface" in line:
                        parsing = True
                        headers = self.parse_routing_headers(line)
                        continue
                else:
                    if not line or line.startswith("-") or line.startswith("Route Flags") or line.startswith(
                            "Routing Table"):
                        continue
                    # 处理路由条目
                    routing_entry = self.parse_routing_entry(line, headers)
                    if routing_entry:
                        routing_table.append(routing_entry)
            with self.telnet_lock:
                self.telnet_routing_tables[sysname] = routing_table
            return routing_table
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] Error getting routing table: {e}")
            return None

    def parse_routing_headers(self, header_line: str) -> Dict[str, int]:
        """
        解析路由表表头行，返回每个字段的起始位置
        """
        headers = {}
        fields = ["Destination/Mask", "Proto", "Pre", "Cost", "Flags", "NextHop", "Interface"]
        for field in fields:
            index = header_line.find(field)
            if index != -1:
                headers[field] = index
        return headers

    def parse_routing_entry(self, line: str, headers: Dict[str, int]) -> Optional[Dict[str, str]]:
        """
        根据表头的起始位置，从数据行中提取对应字段的值
        """
        try:
            destination = self.extract_field(line, headers, 'Destination/Mask')
            proto = self.extract_field(line, headers, 'Proto')
            pre = self.extract_field(line, headers, 'Pre')
            cost = self.extract_field(line, headers, 'Cost')
            flags = self.extract_field(line, headers, 'Flags')
            next_hop = self.extract_field(line, headers, 'NextHop')
            interface = self.extract_field(line, headers, 'Interface')

            # 过滤条件：
            # 1. Protocol 为 "Direct"
            # 2. Destination 地址最后一个字段为 0（例如 10.0.13.0/24）
            if proto.lower() == "direct":
                dest_octets = destination.split('/')[0].split('.')
                if dest_octets[-1] == '0':
                    return None  # 跳过符合条件的路由条目

            if destination and next_hop and interface:
                return {
                    "Destination": destination,
                    "Protocol": proto,
                    "Preference": pre,
                    "Cost": cost,
                    "Flags": flags,
                    "NextHop": next_hop,
                    "Interface": interface
                }
            else:
                return None
        except Exception as e:
            logging.error(f"Error parsing routing entry: {e}")
            return None

    def connect_and_get_data(self):
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
                        future = executor.submit(self.process_router, tn)
                        future_to_node[future] = node
                    except Exception as e:
                        logging.error(f"Failed to connect to {host}:{port} via Telnet: {e}")

            for future in as_completed(future_to_node):
                node = future_to_node[future]
                host, port = node.get("hostip"), node.get("port")
                try:
                    result = future.result()
                    msg = "successful" if result else "failed"
                    logging.info(f"[{host}:{port}] Data retrieval {msg}.")
                except Exception as e:
                    logging.error(f"[{host}:{port}] Exception during data retrieval: {e}")

    def process_router(self, tn: telnetlib.Telnet) -> bool:
        sysname = self.get_sysname_via_telnet(tn)
        if not sysname:
            return False
        interfaces = self.get_interfaces_via_telnet(tn, sysname)
        if interfaces is None:
            return False
        routing_table = self.get_routing_table_via_telnet(tn, sysname)
        if routing_table is None:
            return False
        return True

    def collect_results(self) -> Dict[str, Any]:
        # 去除冗余信息，仅保留接口名和IP地址，路由表仅保留关键字段
        cleaned_devices = {}
        for sysname, interfaces in self.telnet_interfaces.items():
            routing_table = self.telnet_routing_tables.get(sysname, [])
            cleaned_routing_table = []
            seen_routes = set()
            for route in routing_table:
                # 过滤协议为 "Direct" 且 Destination 地址最后一个字段为 0 的路由条目
                proto = route.get("Protocol", "").lower()
                destination = route.get("Destination", "")
                if proto == "direct":
                    dest_octets = destination.split('/')[0].split('.')
                    if dest_octets[-1] == '0':
                        continue  # 跳过符合条件的路由条目

                key = (route["Destination"], route["NextHop"], route["Interface"])
                if key not in seen_routes:
                    cleaned_routing_table.append({
                        "Destination": route["Destination"],
                        "NextHop": route["NextHop"],
                        "Interface": route["Interface"]
                    })
                    seen_routes.add(key)
            cleaned_devices[sysname] = {
                "interfaces": interfaces,
                "routing_table": cleaned_routing_table
            }
        return {
            "telnet_devices": cleaned_devices
        }



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
        with open(input_path, 'r') as f:
            telnet_info = json.load(f)
        logging.info(f"Successfully loaded telnet_info from {input_path}")
        return telnet_info
    except FileNotFoundError:
        logging.error(f"param.json file not found at path: {input_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logging.error(f"Error decoding JSON from param.json: {e}")
        sys.exit(1)


def write_json(output_path: str, data: Dict[str, Any]):
    try:
        with open(output_path, 'w') as f:
            json.dump(data, f, indent=4)
        logging.info(f"Cleaned data written to {output_path}")
    except IOError as e:
        logging.error(f"Error writing to JSON file: {e}")
        sys.exit(1)


def main(input_path: str, report_path: str):
    base_path = "/uploadPath/reasoning"
    if "{t}" in input_path or "{t}" in report_path:
        latest_folder = find_latest_folder(base_path)
        input_path = input_path.replace("{t}", latest_folder)
        report_path = report_path.replace("{t}", latest_folder)
        logging.debug(f"Resolved input_path: {input_path}")
        logging.debug(f"Resolved report_path: {report_path}")

    telnet_info = load_telnet_info(input_path)
    router_manager = RouterManager(telnet_info)
    start_time = datetime.now()
    router_manager.connect_and_get_data()
    mapping = router_manager.collect_results()
    end_time = datetime.now()
    execution_time = f"{start_time.strftime('%Y-%m-%d %H:%M:%S')} - {end_time.strftime('%Y-%m-%d %H:%M:%S')}"

    # 保存清理后的数据到 /tmp/loop.json
    write_json("/tmp/loop.json", mapping)

    logging.info("Collected router configurations:")
    logging.info(json.dumps(mapping, indent=4))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Process router configurations from param.json.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json, use {t} for latest folder number.")
    parser.add_argument("-o", "--output", required=True,
                        help="Output path for report.txt, use {t} for latest folder number.")
    args = parser.parse_args()
    main(args.input, args.output)
