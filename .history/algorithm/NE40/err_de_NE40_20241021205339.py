import json
import telnetlib
import os
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import logging
from typing import Optional, Dict, Any
import re  # 引入正则表达式模块

# 配置日志记录
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

class RouterManager:
    def __init__(self, telnet_info: Dict[str, Any], max_workers: int = 20):
        self.telnet_info = telnet_info
        self.telnet_sysnames: Dict[str, str] = {}
        self.telnet_configurations: Dict[str, Any] = {}  # 修改为Dict[str, Any]以存储分类后的路由
        self.max_workers = max_workers
        self.telnet_lock = Lock()
        # 定义不同设备类型的命令序列
        self.commands_map = {
            "huaweine40": (
                [
                    'scr 0 t',
                    'display ip routing-table',
                    'display ospf brief',
                    'display acl all',
                    'display bgp all summary',
                    'display ip interface brief'
                ],
                b'q\n'
            )
            # 可以在此处添加更多设备类型及其对应的命令序列
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
        # 根据部分image_type查找匹配的设备类型
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
                    # 解析并分类路由表
                    self.telnet_configurations[key] = self.parse_routing_table(output)
        else:
            logging.warning(f"[{tn.host}:{tn.port}] No output received from Telnet commands.")
        return output

    def parse_routing_table(self, output: str) -> Dict[str, Any]:
        """
        解析路由表并按协议类型分类。
        """
        routing_table = {}
        current_section = None

        # 使用正则表达式找到路由表开始
        routing_table_start = re.search(r"Routing Table : _public_", output)
        if not routing_table_start:
            logging.warning("Routing Table section not found in output.")
            return routing_table

        # 提取Routing Table部分
        routing_table_text = output.split("Routing Table : _public_")[1]
        # 找到路由条目的起始点
        routing_entries_start = re.search(r"Destination/Mask\s+Proto\s+Pre\s+Cost\s+Flags\s+NextHop\s+Interface", routing_table_text)
        if not routing_entries_start:
            logging.warning("Routing entries header not found.")
            return routing_table

        # 获取所有路由条目
        routing_entries = routing_table_text.split("Destination/Mask")[1]
        routing_entries = routing_entries.strip().splitlines()

        for line in routing_entries:
            # 跳过空行或格式不正确的行
            if not line.strip() or line.startswith("OSPF") or line.startswith("BGP"):
                continue

            # 使用正则表达式解析每一行
            match = re.match(r"(\S+/\d+)\s+(\S+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)", line)
            if match:
                dest_mask, proto, pre, cost, flags, next_hop, interface = match.groups()
                if proto not in routing_table:
                    routing_table[proto] = []
                routing_table[proto].append({
                    "Destination/Mask": dest_mask,
                    "Pre": int(pre),
                    "Cost": int(cost),
                    "Flags": flags,
                    "NextHop": next_hop,
                    "Interface": interface
                })
            else:
                # 处理如“OSPF    10   2             D   10.0.23.1                                Ethernet1/0/0”这样的行
                match = re.match(r"(\S+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)", line)
                if match:
                    dest_mask, proto, pre, cost, flags, next_hop = match.groups()
                    interface = "N/A"  # 如果接口信息缺失，可以设置为N/A
                    if proto not in routing_table:
                        routing_table[proto] = []
                    routing_table[proto].append({
                        "Destination/Mask": dest_mask,
                        "Pre": int(pre),
                        "Cost": int(cost),
                        "Flags": flags,
                        "NextHop": next_hop,
                        "Interface": interface
                    })
                else:
                    logging.debug(f"Unmatched routing entry line: {line}")

        return routing_table

    def clean_configuration_output(self, output: str) -> str:
        """
        清理Telnet命令输出，保留关键信息，去除无用部分。
        """
        # 定义需要保留的关键部分的模式
        keep_patterns = [
            r"Routing Table : _public_",
            r"Destination/Mask\s+Proto\s+Pre\s+Cost\s+Flags\s+NextHop\s+Interface",
            r"OSPF Process",
            r"BGP local router ID",
            r"Address Family:Ipv4 Unicast",
            r"Peer\s+AS\s+MsgRcvd\s+MsgSent\s+OutQ\s+Up/Down\s+State\s+RtRcv\s+RtAdv",
            r"Interface\s+IP Address/Mask\s+Physical\s+Protocol\s+VPN"
        ]

        # 将输出按行分割
        lines = output.splitlines()
        cleaned_lines = []
        keep_section = False

        for line in lines:
            # 检查是否进入需要保留的部分
            if any(re.search(pattern, line) for pattern in keep_patterns):
                keep_section = True

            # 如果当前行是分隔线，则跳过
            if re.match(r"=+", line) or re.match(r"-+", line):
                continue

            # 如果进入了需要保留的部分，则保留该行
            if keep_section:
                # 排除以特定字符开头的行
                if not re.match(r"^(Device:|Route Flags:|.*\*\w+|!down:|\^down:|.*\(.*\)|Helper support capability|Multi-VPN-Instance|TCP Port)", line):
                    # 排除空行
                    if line.strip():
                        cleaned_lines.append(line.strip())

            # 如果遇到另一个设备标识，则停止保留部分
            if re.match(r"Device:", line):
                keep_section = False

        # 将保留的行重新组合成字符串
        cleaned_output = "\n".join(cleaned_lines)
        return cleaned_output

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
        return {
            "telnet_devices": {
                host_port: {
                    "sysname": sysname,
                    "routing_table": self.telnet_configurations.get(host_port, {})
                }
                for host_port, sysname in self.telnet_sysnames.items()
            }
        }

    def write_configurations_to_file(self, output_path: str):
        try:
            with open(output_path, 'w') as f:
                for host_port, configuration in self.telnet_configurations.items():
                    f.write(f"Device: {host_port}\n")
                    # 写入分类后的路由表
                    for proto, routes in configuration.items():
                        f.write(f"Protocol: {proto}\n")
                        for route in routes:
                            dest_mask = route.get("Destination/Mask", "N/A")
                            pre = route.get("Pre", "N/A")
                            cost = route.get("Cost", "N/A")
                            flags = route.get("Flags", "N/A")
                            next_hop = route.get("NextHop", "N/A")
                            interface = route.get("Interface", "N/A")
                            f.write(f"  Destination/Mask: {dest_mask}, Pre: {pre}, Cost: {cost}, Flags: {flags}, NextHop: {next_hop}, Interface: {interface}\n")
                        f.write("\n")
                    f.write("="*40 + "\n")
            logging.info(f"Configurations written to {output_path}")
        except IOError as e:
            logging.error(f"Error writing configurations to file: {e}")

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

def write_output(output_path: str, data: Dict[str, Any]):
    try:
        with open(output_path, 'w') as f:
            json.dump(data, f, indent=4)
        logging.info(f"Mapping results written to {output_path}")
    except IOError as e:
        logging.error(f"Error writing to output file: {e}")
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
    router_manager.connect_and_get_sysnames_and_configs()
    mapping = router_manager.collect_results()

    logging.info("Collected router configurations:")
    logging.info(json.dumps(mapping, indent=4))
    write_output(output_path, mapping)
    router_manager.write_configurations_to_file(output_path.replace('.json', '_configurations.txt'))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process router configurations from param.json.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json, use {t} for latest folder number.")
    parser.add_argument("-o", "--output", required=True, help="Output path for process information, use {t} for latest folder number.")
    args = parser.parse_args()
    main(args.input, args.output)
