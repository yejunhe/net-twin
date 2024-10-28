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
    level=logging.DEBUG,  # Set to DEBUG for more detailed logs
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

class RouterManager:
    def __init__(self, telnet_info: Dict[str, Any], max_workers: int = 20):
        self.telnet_info = telnet_info
        self.telnet_sysnames: Dict[str, str] = {}
        self.telnet_configurations: Dict[str, List[Dict[str, str]]] = {}
        self.telnet_ospf_interfaces: Dict[str, List[str]] = {}
        self.telnet_router_ids: Dict[str, str] = {}
        self.telnet_ospf_peers: Dict[str, List[str]] = {}
        self.telnet_isis_interfaces: Dict[str, List[str]] = {}
        self.telnet_isis_peers_count: Dict[str, int] = {}
        self.telnet_bgp_info: Dict[str, Dict[str, Any]] = {}
        self.network_connections: Optional[List[Dict[str, Any]]] = None
        self.node_interfaces: Dict[str, List[Dict[str, str]]] = {}
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
        try:
            tn.write(b'\n')
            time.sleep(1)
            initial_output = tn.read_very_eager().decode('ascii', errors='ignore')
            logging.debug(f"[{tn.host}:{tn.port}] Initial Telnet output:\n{initial_output}")

            command_outputs = {}
            for cmd in commands:
                tn.write(cmd.encode('ascii') + b'\n')
                logging.info(f"[{tn.host}:{tn.port}] Sending command: {cmd}")
                time.sleep(2)
                cmd_output = tn.read_very_eager().decode('ascii', errors='ignore')
                command_outputs[cmd] = cmd_output
                logging.debug(f"[{tn.host}:{tn.port}] Output for '{cmd}':\n{cmd_output}")

            prompt = self.get_prompt(tn)
            if prompt and not (prompt.startswith('<') and prompt.endswith('>')):
                tn.write(quit_cmd)
                logging.info(f"[{tn.host}:{tn.port}] Sending quit command.")
                time.sleep(1)
                command_outputs['quit'] = tn.read_very_eager().decode('ascii', errors='ignore')

            return command_outputs
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] Telnet Error: {e}")
            return {}

    def get_prompt(self, tn: telnetlib.Telnet) -> Optional[str]:
        try:
            time.sleep(1)
            output = tn.read_very_eager().decode('ascii', errors='ignore')
            prompt = output.strip().splitlines()[-1] if output.strip() else None
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
            sysname = next((line.strip('<> ').strip() for line in output.splitlines()
                            if line.startswith('<') and line.endswith('>')), None)
            if sysname:
                logging.info(f"[{tn.host}:{tn.port}] Detected sysname: {sysname}")
            else:
                logging.warning(f"[{tn.host}:{tn.port}] No sysname detected.")
            return sysname
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] Telnet Error while getting sysname: {e}")
            return None

    def get_configuration_via_telnet(self, tn: telnetlib.Telnet, image_type: str) -> Optional[Dict[str, str]]:
        matched_key = next((key for key in self.commands_map if key in image_type), None)
        if not matched_key:
            logging.warning(f"[{tn.host}:{tn.port}] Unsupported image_type '{image_type}'. Skipping.")
            return None

        commands, quit_cmd = self.commands_map[matched_key]
        command_outputs = self.execute_telnet_commands(tn, commands, quit_cmd)
        if not command_outputs:
            logging.warning(f"[{tn.host}:{tn.port}] No output received from Telnet commands.")
            return None

        sysname = self.get_sysname_via_telnet(tn)
        if not sysname:
            return None

        key = f"{tn.host}:{tn.port}"
        with self.telnet_lock:
            self.telnet_sysnames[key] = sysname
            self.telnet_configurations[key] = self.parse_display_ip_interface_brief(command_outputs.get('display ip interface brief', ''))
            self.telnet_ospf_interfaces[key] = self.parse_display_ospf_interface(command_outputs.get('display ospf interface', ''))
            self.telnet_router_ids[key], self.telnet_ospf_peers[key] = self.parse_display_ospf_peer(command_outputs.get('display ospf peer', ''))
            self.telnet_isis_interfaces[key] = self.parse_display_isis_interface(command_outputs.get('display isis interface', ''))
            self.telnet_isis_peers_count[key] = self.parse_display_isis_peer(command_outputs.get('display isis peer', ''))
            bgp_info = self.parse_display_bgp_all_summary(command_outputs.get('display bgp all summary', ''))
            if bgp_info:
                self.telnet_bgp_info[key] = bgp_info

        return command_outputs

    def parse_display_ospf_peer(self, output: str) -> (Optional[str], Optional[List[str]]):
        router_id = None
        neighbors = []
        router_id_match = re.search(r'OSPF Process \d+ with Router ID (\d+\.\d+\.\d+\.\d+)', output)
        if router_id_match:
            router_id = router_id_match.group(1)
            neighbors = re.findall(r'Router ID:\s+(\d+\.\d+\.\d+\.\d+)', output)
            logging.info(f"Extracted Router ID: {router_id} with Neighbors: {neighbors}")
        else:
            logging.info("No OSPF Process information found in 'display ospf peer' output.")
        return router_id, neighbors if neighbors else None

    def parse_display_isis_interface(self, output: str) -> List[str]:
        interfaces = []
        for line in output.splitlines():
            if line.strip().startswith("Interface") or not line.strip() or re.match(r'^[-=]+$', line):
                continue
            iface = line.split()[0]
            if iface.lower().startswith('eth'):
                iface_formatted = f"Ethernet{iface[3:]}"
            else:
                iface_formatted = iface.capitalize()
            interfaces.append(iface_formatted)
            logging.debug(f"Detected ISIS-configured interface: {iface_formatted}")
        logging.debug(f"Parsed ISIS interfaces: {interfaces}")
        return interfaces

    def parse_display_isis_peer(self, output: str) -> Optional[int]:
        match = re.search(r'^Total Peer\(s\):\s+(\d+)', output, re.IGNORECASE | re.MULTILINE)
        total_peers = int(match.group(1)) if match else None
        if total_peers is not None:
            logging.info(f"Extracted total ISIS peers: {total_peers}")
        else:
            logging.info("No 'Total Peer(s):' line found in 'display isis peer' output.")
        return total_peers

    def parse_display_bgp_all_summary(self, output: str) -> Optional[Dict[str, Any]]:
        bgp_info = {
            "local_router_id": None,
            "local_as_number": None,
            "total_peers": 0,
            "established_peers": 0,
            "non_established_peers": []
        }

        key_value_matches = re.findall(r'(\w+(?:\s+\w+)*)\s*:\s*(\d+)', output, re.IGNORECASE)
        for key, value in key_value_matches:
            key_lower = key.strip().lower()
            if key_lower == 'bgp local router id':
                bgp_info["local_router_id"] = value
            elif key_lower == 'local as number':
                bgp_info["local_as_number"] = value
            elif key_lower == 'total number of peers':
                bgp_info["total_peers"] = int(value)
            elif key_lower == 'peers in established state':
                bgp_info["established_peers"] = int(value)

        non_established_peers = re.findall(
            r'Peer IP:\s+(\S+),\s+AS:\s+(\d+),\s+State:\s+(\w+)', output, re.IGNORECASE)
        bgp_info["non_established_peers"] = [
            {"peer_ip": ip, "peer_as": asn, "state": state.capitalize()}
            for ip, asn, state in non_established_peers
        ]

        if bgp_info["local_router_id"] and bgp_info["local_as_number"]:
            logging.info(f"Extracted BGP information: {bgp_info}")
            return bgp_info
        else:
            logging.warning("Incomplete BGP information extracted.")
            return None

    def parse_display_ip_interface_brief(self, output: str) -> List[Dict[str, str]]:
        interfaces = []
        interface_regex = re.compile(
            r'^\s*(?P<interface>\S+)\s+(?P<ip_address>(?:\d{1,3}\.){3}\d{1,3}/\d{1,2}|unassigned)\s+(?P<physical>up|down)\s+(?P<protocol>up|down)\s+\S+'
        )

        for line in output.splitlines():
            match = interface_regex.match(line)
            if match and match.group('ip_address').lower() != 'unassigned':
                iface = match.group('interface')
                iface_formatted = f"Ethernet{iface[3:]}" if iface.lower().startswith('eth') else iface.capitalize()
                interfaces.append({
                    'Interface': iface_formatted,
                    'IP Address/Mask': match.group('ip_address'),
                    'Physical': match.group('physical'),
                    'Protocol': match.group('protocol')
                })
                logging.debug(f"Parsed interface: {iface_formatted}")
        logging.debug(f"Parsed Telnet interfaces: {interfaces}")
        return interfaces

    def parse_display_ospf_interface(self, output: str) -> List[str]:
        interfaces = []
        parsing = False
        interface_regex = re.compile(r'^\s*(?P<interface>\S+)\s+[\d\.]+')

        for line in output.splitlines():
            if "Interfaces" in line:
                parsing = True
                continue
            if parsing:
                if not line.strip() or re.match(r'^[-=]+$', line):
                    continue
                match = interface_regex.match(line)
                if match:
                    iface = match.group('interface')
                    iface_formatted = f"Ethernet{iface[3:]}" if iface.lower().startswith('eth') else iface.capitalize()
                    interfaces.append(iface_formatted)
                    logging.debug(f"Formatted OSPF interface name: {iface_formatted}")
        logging.debug(f"Parsed OSPF interfaces: {interfaces}")
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
        interface_status = {}
        ospf_status = {}
        isis_status = {}
        bgp_info_dict = {}

        for host_port, sysname in self.telnet_sysnames.items():
            device_info = self.telnet_configurations.get(host_port, [])
            ospf_ifaces = self.telnet_ospf_interfaces.get(host_port, [])
            isis_ifaces = self.telnet_isis_interfaces.get(host_port, [])
            isis_peers = self.telnet_isis_peers_count.get(host_port)
            router_id = self.telnet_router_ids.get(host_port)
            ospf_neighbors = self.telnet_ospf_peers.get(host_port, [])
            bgp_info = self.telnet_bgp_info.get(host_port, {})

            telnet_if_names = {iface['Interface'].lower() for iface in device_info}
            ospf_if_names = {iface.lower() for iface in ospf_ifaces}
            isis_if_names = {iface.lower() for iface in isis_ifaces}

            logging.debug(f"[{host_port}] Telnet fetched interfaces: {telnet_if_names}")
            logging.debug(f"[{host_port}] OSPF-configured interfaces: {ospf_if_names}")
            logging.debug(f"[{host_port}] ISIS-configured interfaces: {isis_if_names}")
            if bgp_info:
                logging.debug(f"[{host_port}] BGP info: {bgp_info}")

            interface_status[host_port] = []
            for iface in self.node_interfaces.get(sysname, []):
                iface_name = iface['name']
                iface_formatted = f"Ethernet{iface_name[1:]}" if iface_name.lower().startswith('e') else f"{iface['type'].capitalize()}{iface_name}"
                iface_lower = iface_formatted.lower()
                config_status = "已配置IP地址" if iface_lower in telnet_if_names else "未配置IP地址"
                ospf_status_str = "OSPF已配置" if iface_lower in ospf_if_names else "OSPF未配置"
                isis_status_str = "ISIS已配置" if iface_lower in isis_if_names else "ISIS未配置"
                status = f"{iface_formatted}接口配置状态: {config_status}, {ospf_status_str}, {isis_status_str}"
                interface_status[host_port].append(status)
                logging.info(f"[{host_port}] {status}")

            # OSPF Status
            if router_id:
                if ospf_neighbors:
                    ospf_status[host_port] = f"OSPF 配置正常，邻居 Router IDs: {', '.join(ospf_neighbors)}"
                elif ospf_ifaces:
                    ospf_status[host_port] = "OSPF 配置问题：未检测到邻居 Router ID"
                else:
                    ospf_status[host_port] = "OSPF 未配置"
            else:
                ospf_status[host_port] = "OSPF 配置问题：未检测到 Router ID" if ospf_ifaces else "OSPF 未配置"

            # ISIS Status
            if isis_peers is not None:
                if isis_peers > 0 and isis_ifaces:
                    isis_status[host_port] = f"ISIS 配置正常，邻居数量: {isis_peers}"
                elif isis_ifaces and isis_peers == 0:
                    isis_status[host_port] = "ISIS 配置问题：接口配置了 ISIS 但未检测到邻居 Router ID"
                elif not isis_ifaces and isis_peers > 0:
                    isis_status[host_port] = "ISIS 配置问题：存在 ISIS 邻居但未配置 ISIS 接口"
                else:
                    isis_status[host_port] = "ISIS 配置问题：未知情况"
            else:
                isis_status[host_port] = "ISIS 配置错误：接口配置了 ISIS 但未检测到邻居 Router ID" if isis_ifaces else "ISIS 未配置"

            # BGP Info
            bgp_info_dict[host_port] = {
                "bgp_local_router_id": bgp_info.get("local_router_id", "未知"),
                "bgp_local_as_number": bgp_info.get("local_as_number", "未知"),
                "bgp_total_peers": bgp_info.get("total_peers", 0),
                "bgp_established_peers": bgp_info.get("established_peers", 0),
                "bgp_non_established_peers": bgp_info.get("non_established_peers", [])
            }

            # Log BGP status
            if bgp_info.get("local_router_id"):
                logging.info(f"[{host_port}] BGP 本地 Router ID: {bgp_info['bgp_local_router_id']}")
                logging.info(f"[{host_port}] BGP 本地 AS Number: {bgp_info['bgp_local_as_number']}")
                logging.info(f"[{host_port}] BGP 总邻居数量: {bgp_info['bgp_total_peers']}")
                logging.info(f"[{host_port}] BGP 建立状态的邻居数量: {bgp_info['bgp_established_peers']}")
                if bgp_info["bgp_non_established_peers"]:
                    logging.info(f"[{host_port}] BGP 未建立状态的邻居: {bgp_info['bgp_non_established_peers']}")
                else:
                    logging.info(f"[{host_port}] 所有 BGP peers 均处于 Established 状态。")
            else:
                logging.info(f"[{host_port}] BGP 未配置")

        return {
            "telnet_devices": {
                host_port: {
                    "sysname": sysname,
                    "router_id": self.telnet_router_ids.get(host_port, "未知 Router ID"),
                    "ospf_neighbors": self.telnet_ospf_peers.get(host_port, []),
                    "interfaces": self.telnet_configurations.get(host_port, []),
                    "ospf_interfaces": self.telnet_ospf_interfaces.get(host_port, []),
                    "isis_interfaces": self.telnet_isis_interfaces.get(host_port, []),
                    "interface_status": interface_status.get(host_port, []),
                    "ospf_status": ospf_status.get(host_port, "未配置 OSPF"),
                    "isis_status": isis_status.get(host_port, "未配置 ISIS"),
                    "bgp_info": bgp_info_dict.get(host_port, {})
                }
                for host_port, sysname in self.telnet_sysnames.items()
            },
            "network_connections": self.network_connections
        }

    def read_unl_file(self, lab_id: int):
        unl_file_path = f"/opt/unetlab/labs/{lab_id}.unl"
        try:
            with open(unl_file_path, 'r', encoding='utf-8') as f:
                self.parse_unl_file(f.read())
            logging.info(f"Successfully read UNL file from {unl_file_path}")
        except FileNotFoundError:
            logging.error(f"UNL file not found at path: {unl_file_path}")
        except IOError as e:
            logging.error(f"Error reading UNL file: {e}")

    def parse_unl_file(self, unl_content: str):
        try:
            root = ET.fromstring(unl_content)
            nodes = {node.get("id"): node.get("name") for node in root.findall(".//node")}
            network_to_interfaces = {}
            for node in root.findall(".//node"):
                node_name = nodes.get(node.get("id"))
                for interface in node.findall("interface"):
                    network_id = interface.get("network_id")
                    if network_id:
                        network_to_interfaces.setdefault(network_id, []).append({
                            "node_name": node_name,
                            "interface_name": interface.get("name"),
                            "type": interface.get("type", "ethernet")
                        })
                        self.node_interfaces.setdefault(node_name, []).append({
                            "name": interface.get("name"),
                            "type": interface.get("type", "ethernet")
                        })

            connections = [
                {
                    "network_id": net_id,
                    "node1": ifaces[0]["node_name"],
                    "interface1": ifaces[0]["interface_name"],
                    "node2": ifaces[1]["node_name"],
                    "interface2": ifaces[1]["interface_name"]
                }
                for net_id, ifaces in network_to_interfaces.items() if len(ifaces) == 2
            ]

            for conn in connections:
                logging.info(f"Connected {conn['node1']}:{conn['interface1']} <-> {conn['node2']}:{conn['interface2']} via network_id {conn['network_id']}")

            self.network_connections = connections
            logging.info("Successfully parsed UNL file into network connections.")
        except ET.ParseError as e:
            logging.error(f"Error parsing UNL file: {e}")

def find_latest_folder(base_path: str) -> str:
    try:
        all_folders = [f for f in os.listdir(base_path) if f.isdigit()]
        latest_folder = max(all_folders, key=int)
        logging.info(f"Latest folder identified: {latest_folder}")
        return latest_folder
    except (FileNotFoundError, ValueError):
        logging.error("No numbered folders found or base path not found.")
        sys.exit(1)

def load_telnet_info(input_path: str) -> Dict[str, Any]:
    try:
        with open(input_path, 'r', encoding='utf-8') as f:
            telnet_info = json.load(f)
        logging.info(f"Successfully loaded telnet_info from {input_path}")
        return telnet_info
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logging.error(f"Error loading param.json: {e}")
        sys.exit(1)

def write_output(output_path: str, data: Dict[str, Any]):
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        logging.info(f"Mapping results written to {output_path}")
    except IOError as e:
        logging.error(f"Error writing to output file: {e}")
        sys.exit(1)

def write_interface_status(data_txt_path: str, mapping: Dict[str, Any]):
    try:
        with open(data_txt_path, 'w', encoding='utf-8') as f:
            for host_port, device in mapping.get("telnet_devices", {}).items():
                f.write(f"节点: {device.get('sysname', '未知节点')} ({host_port})\n")
                for status in device.get("interface_status", []):
                    f.write(f"    接口: {status}\n")
                f.write(f"    OSPF 状态: {device.get('ospf_status', '未配置 OSPF')}\n")
                f.write(f"    ISIS 状态: {device.get('isis_status', '未配置 ISIS')}\n")

                bgp = device.get("bgp_info", {})
                if bgp.get("bgp_local_router_id") != "未知":
                    f.write(f"    BGP 本地 Router ID: {bgp.get('bgp_local_router_id')}\n")
                    f.write(f"    BGP 本地 AS Number: {bgp.get('bgp_local_as_number')}\n")
                    f.write(f"    BGP 总邻居数量: {bgp.get('bgp_total_peers')}\n")
                    f.write(f"    BGP 建立状态的邻居数量: {bgp.get('bgp_established_peers')}\n")
                    non_established = bgp.get("bgp_non_established_peers", [])
                    if non_established:
                        f.write(f"    BGP 未建立状态的邻居:\n")
                        for peer in non_established:
                            f.write(f"        Peer IP: {peer.get('peer_ip', '未知')}, AS: {peer.get('peer_as', '未知')}, State: {peer.get('state', '未知')}\n")
                    else:
                        f.write(f"    BGP 未建立状态的邻居: 无\n")
                else:
                    f.write(f"    BGP 未配置\n")
                f.write("\n")
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

    output_dir = os.path.dirname(output_path)
    data_txt_path = os.path.join(output_dir, "data.txt")
    write_interface_status(data_txt_path, mapping)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process router configurations from param.json.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json, use {t} for latest folder number.")
    parser.add_argument("-o", "--output", required=True, help="Output path for process information, use {t} for latest folder number.")
    args = parser.parse_args()
    main(args.input, args.output)
