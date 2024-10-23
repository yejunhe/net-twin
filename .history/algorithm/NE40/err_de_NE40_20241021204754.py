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
        self.telnet_configurations: Dict[str, str] = {}
        self.max_workers = max_workers
        self.telnet_lock = Lock()
        # 定义不同设备类型的命令序列
        self.commands_map = {
            "huaweine40": (
                [
                    'scr 0 t',
                    'display ip routing-table',
                    'display ospf brief',
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
                # 添加命令标记
                tn.write(f"---COMMAND: {cmd}---\n".encode('ascii'))
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
                    self.telnet_configurations[key] = self.clean_configuration_output(output)
        else:
            logging.warning(f"[{tn.host}:{tn.port}] No output received from Telnet commands.")
        return output

    def clean_configuration_output(self, output: str) -> str:
        """
        清理Telnet命令输出，保留关键信息，去除无用部分。
        根据不同命令应用不同的清理规则。
        """
        cleaned_output = ""
        # 分割输出为多个命令的部分
        command_sections = re.split(r"---COMMAND:\s+(.+?)---", output)
        # re.split会返回一个列表，其中奇数索引为命令名，偶数索引为命令输出
        for i in range(1, len(command_sections), 2):
            command = command_sections[i].strip()
            section_output = command_sections[i+1].strip() if (i+1) < len(command_sections) else ""
            logging.debug(f"Processing command: {command}")
            if not section_output:
                cleaned_section = "没有相应的配置信息"
            else:
                if command == 'display ip routing-table':
                    cleaned_section = self.clean_routing_table(section_output)
                elif command == 'display ospf brief':
                    cleaned_section = self.clean_ospf_brief(section_output)
                elif command == 'display bgp all summary':
                    cleaned_section = self.clean_bgp_summary(section_output)
                elif command == 'display ip interface brief':
                    cleaned_section = self.clean_ip_interface_brief(section_output)
                else:
                    # 默认保留所有内容
                    cleaned_section = section_output
            cleaned_output += f"--- {command} ---\n{cleaned_section}\n\n"
        return cleaned_output.strip()

    def clean_routing_table(self, output: str) -> str:
        """
        按协议类型对路由表进行分类。
        """
        lines = output.splitlines()
        cleaned_lines = []
        protocol_routes = {}
        headers = []
        for line in lines:
            # 跳过空行和分隔线
            if not line.strip() or re.match(r"=+", line) or re.match(r"-+", line):
                continue
            # 检查是否是表头
            if re.match(r"Destination/Mask\s+Proto\s+Pre\s+Cost\s+Flags\s+NextHop\s+Interface", line):
                headers = re.split(r"\s{2,}", line)
                continue
            # 解析路由条目
            if headers:
                parts = re.split(r"\s{2,}", line)
                if len(parts) < 7:
                    # 处理多行条目，例如OSPF协议有两行
                    continue
                dest_mask, proto, pre, cost, flags, next_hop, interface = parts[:7]
                protocol = proto.strip()
                if protocol not in protocol_routes:
                    protocol_routes[protocol] = []
                protocol_routes[protocol].append({
                    "Destination/Mask": dest_mask.strip(),
                    "Pre": pre.strip(),
                    "Cost": cost.strip(),
                    "Flags": flags.strip(),
                    "NextHop": next_hop.strip(),
                    "Interface": interface.strip()
                })
        if not protocol_routes:
            return "没有相应的配置信息"
        # 构建分类后的路由表
        for proto, routes in protocol_routes.items():
            cleaned_lines.append(f"协议类型: {proto}")
            for route in routes:
                cleaned_lines.append(f"  {route['Destination/Mask']} | Pre: {route['Pre']} | Cost: {route['Cost']} | Flags: {route['Flags']} | NextHop: {route['NextHop']} | Interface: {route['Interface']}")
            cleaned_lines.append("")  # 添加空行分隔不同协议
        return "\n".join(cleaned_lines).strip()

    def clean_ospf_brief(self, output: str) -> str:
        """
        仅保留OSPF可达的节点信息。
        """
        lines = output.splitlines()
        cleaned_lines = []
        ospf_reachable = False
        for line in lines:
            # 检查OSPF进程开始
            if re.match(r"OSPF Process", line):
                ospf_reachable = True
                cleaned_lines.append(line.strip())
                continue
            if ospf_reachable:
                # 跳过不需要的行
                if re.match(r"RouterID|OSPF Protocol Information|.*Session Car.*|.*Micro-isolation Protocol-car.*|.*Authtype.*|.*Router ID conflict state.*", line):
                    continue
                # 匹配接口信息
                match = re.match(r"Interface:\s+(.+)", line)
                if match:
                    cleaned_lines.append(line.strip())
                    continue
                # 匹配Cost信息
                match = re.match(r"Cost:\s+(\d+)\s+State:\s+(\w+)\s+Type:\s+(\w+)", line)
                if match:
                    cost, state, type_ = match.groups()
                    cleaned_lines.append(f"  Cost: {cost} | State: {state} | Type: {type_}")
                    continue
                # 匹配Timers信息
                if "Timers:" in line:
                    timers = line.split("Timers:")[1].strip()
                    cleaned_lines.append(f"  Timers: {timers}")
                    continue
                # 结束条件
                if re.match(r"Total nonempty ACL number", line):
                    break
                # 其他需要保留的行
                if line.strip():
                    cleaned_lines.append(line.strip())
        if not cleaned_lines:
            return "没有相应的配置信息"
        return "\n".join(cleaned_lines).strip()

    def clean_bgp_summary(self, output: str) -> str:
        """
        仅保留BGP邻居信息。
        """
        lines = output.splitlines()
        cleaned_lines = []
        bgp_reachable = False
        headers = []
        for line in lines:
            # 检查BGP进程开始
            if re.match(r"BGP local router ID", line):
                bgp_reachable = True
                continue
            if bgp_reachable:
                # 跳过分隔线和无关信息
                if re.match(r"=+", line) or re.match(r"-+", line) or "Address Family:Ipv4 Unicast" in line:
                    continue
                # 记录总数信息
                if re.match(r"Total number of peers", line):
                    continue
                # 检查表头
                if re.match(r"Peer\s+AS\s+MsgRcvd\s+MsgSent\s+OutQ\s+Up/Down\s+State\s+RtRcv\s+RtAdv", line):
                    headers = re.split(r"\s{2,}", line)
                    cleaned_lines.append("BGP邻居信息:")
                    continue
                # 解析BGP邻居信息
                if headers:
                    parts = re.split(r"\s{2,}", line)
                    if len(parts) < 8:
                        continue
                    peer, as_num, msg_recvd, msg_sent, outq, up_down, state, rtrcv, rtadv = parts[:9]
                    cleaned_lines.append(f"  Peer: {peer} | AS: {as_num} | MsgRcvd: {msg_recvd} | MsgSent: {msg_sent} | OutQ: {outq} | Up/Down: {up_down} | State: {state} | RtRcv: {rtrcv} | RtAdv: {rtadv}")
        if not cleaned_lines:
            return "没有相应的配置信息"
        return "\n".join(cleaned_lines).strip()

    def clean_ip_interface_brief(self, output: str) -> str:
        """
        删除IP地址为'unassigned'的接口信息。
        """
        lines = output.splitlines()
        cleaned_lines = []
        headers = []
        for line in lines:
            # 跳过空行和分隔线
            if not line.strip() or re.match(r"=+", line) or re.match(r"-+", line):
                continue
            # 检查是否是表头
            if re.match(r"Interface\s+IP Address/Mask\s+Physical\s+Protocol\s+VPN", line):
                headers = re.split(r"\s{2,}", line)
                cleaned_lines.append(line.strip())
                continue
            # 解析接口条目
            if headers:
                parts = re.split(r"\s{2,}", line)
                if len(parts) < 5:
                    continue
                interface, ip_addr_mask, physical, protocol, vpn = parts[:5]
                if ip_addr_mask.lower() == "unassigned":
                    continue  # 跳过IP地址为'unassigned'的接口
                cleaned_lines.append(line.strip())
        if not cleaned_lines:
            return "没有相应的配置信息"
        return "\n".join(cleaned_lines).strip()

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
                host_port: {"sysname": sysname, "configuration": self.telnet_configurations.get(host_port, 'No config')}
                for host_port, sysname in self.telnet_sysnames.items()
            }
        }

    def write_configurations_to_file(self, output_path: str):
        try:
            with open(output_path, 'w') as f:
                for host_port, configuration in self.telnet_configurations.items():
                    f.write(f"Device: {host_port}\n")
                    f.write(configuration)
                    f.write("\n" + "="*40 + "\n")
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
            json.dump(data, f, indent=4, ensure_ascii=False)
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
    logging.info(json.dumps(mapping, indent=4, ensure_ascii=False))
    write_output(output_path, mapping)
    router_manager.write_configurations_to_file(output_path.replace('.json', '_configurations.txt'))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process router configurations from param.json.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json, use {t} for latest folder number.")
    parser.add_argument("-o", "--output", required=True, help="Output path for process information, use {t} for latest folder number.")
    args = parser.parse_args()
    main(args.input, args.output)
