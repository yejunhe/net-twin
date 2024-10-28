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
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s [%(levelname)s] %(message)s', handlers=[logging.StreamHandler(sys.stdout)])

class RouterManager:
    def __init__(self, telnet_info: Dict[str, Any], max_workers: int = 20):
        self.telnet_info = telnet_info
        self.data = {
            "telnet_sysnames": {}, "telnet_configurations": {}, "telnet_ospf_interfaces": {},
            "telnet_router_ids": {}, "telnet_ospf_peers": {}, "telnet_isis_interfaces": {},
            "telnet_isis_peers_count": {}, "telnet_bgp_info": {}, "node_interfaces": {}
        }
        self.network_connections = None
        self.max_workers = max_workers
        self.telnet_lock = Lock()
        self.commands_map = {
            "huaweine40": ([
                'scr 0 t', 'display ip interface brief', 'display ospf interface',
                'display ospf peer', 'display isis interface', 'display isis peer',
                'display bgp all summary'
            ], b'q\n')
        }

    def execute_telnet_commands(self, tn: telnetlib.Telnet, commands: List[str], quit_cmd: bytes) -> Dict[str, str]:
        command_outputs = {}
        try:
            tn.write(b'\n')
            time.sleep(1)
            tn.read_very_eager()
            for cmd in commands:
                tn.write(cmd.encode('ascii') + b'\n')
                time.sleep(2)
                command_outputs[cmd] = tn.read_very_eager().decode('ascii', errors='ignore')
            tn.write(quit_cmd)
            time.sleep(1)
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] Telnet Error: {e}")
        return command_outputs

    def get_configuration_via_telnet(self, tn: telnetlib.Telnet, image_type: str) -> Optional[Dict[str, str]]:
        matched_key = next((key for key in self.commands_map if key in image_type), None)
        if not matched_key:
            return None
        commands, quit_cmd = self.commands_map[matched_key]
        command_outputs = self.execute_telnet_commands(tn, commands, quit_cmd)
        sysname = self.get_sysname_via_telnet(tn)
        if sysname:
            key = f"{tn.host}:{tn.port}"
            with self.telnet_lock:
                self.data['telnet_sysnames'][key] = sysname
                self.parse_command_outputs(command_outputs, key)
        return command_outputs

    def parse_command_outputs(self, command_outputs: Dict[str, str], key: str):
        parsers = {
            'display ip interface brief': self.parse_display_ip_interface_brief,
            'display ospf interface': self.parse_display_ospf_interface,
            'display ospf peer': self.parse_display_ospf_peer,
            'display isis interface': self.parse_display_isis_interface,
            'display isis peer': self.parse_display_isis_peer,
            'display bgp all summary': self.parse_display_bgp_all_summary
        }
        for command, parser in parsers.items():
            if command in command_outputs:
                parsed_data = parser(command_outputs[command])
                if parsed_data is not None:
                    self.store_parsed_data(key, command, parsed_data)

    def store_parsed_data(self, key: str, command: str, data):
        if command == 'display ip interface brief':
            self.data['telnet_configurations'][key] = data
        elif command == 'display ospf interface':
            self.data['telnet_ospf_interfaces'][key] = data
        elif command == 'display ospf peer':
            router_id, neighbors = data
            if router_id:
                self.data['telnet_router_ids'][key] = router_id
            if neighbors is not None:
                self.data['telnet_ospf_peers'][key] = neighbors
        elif command == 'display isis interface':
            self.data['telnet_isis_interfaces'][key] = data
        elif command == 'display isis peer':
            self.data['telnet_isis_peers_count'][key] = data
        elif command == 'display bgp all summary':
            self.data['telnet_bgp_info'][key] = data

    def parse_display_ip_interface_brief(self, output: str) -> List[Dict[str, str]]:
        interfaces = []
        for line in output.splitlines():
            match = re.match(r'^(?P<interface>\S+)\s+(?P<ip_address>\S+)\s+(?P<physical>\S+)\s+(?P<protocol>\S+)\s+(?P<vpn>\S+)', line)
            if match and match.group('ip_address').lower() != 'unassigned':
                interfaces.append(match.groupdict())
        return interfaces

    def parse_display_ospf_interface(self, output: str) -> List[str]:
        return [line.split()[0] for line in output.splitlines() if re.match(r'^\S+\s+[\d\.]+', line)]

    def parse_display_ospf_peer(self, output: str) -> (Optional[str], Optional[List[str]]):
        router_id = None
        neighbors = []
        for line in output.splitlines():
            if "OSPF Process" in line:
                router_id = re.search(r'Router ID (\S+)', line).group(1)
            elif "Router ID:" in line:
                neighbors.append(re.search(r'Router ID:\s+(\S+)', line).group(1))
        return router_id, neighbors

    def parse_display_isis_interface(self, output: str) -> List[str]:
        return [line.split()[0] for line in output.splitlines() if line.strip() and not line.startswith('Interface')]

    def parse_display_isis_peer(self, output: str) -> Optional[int]:
        match = re.search(r'Total Peer\(s\):\s+(\d+)', output)
        return int(match.group(1)) if match else None

    def parse_display_bgp_all_summary(self, output: str) -> Optional[Dict[str, Any]]:
        bgp_info = {
            "local_router_id": None, "local_as_number": None,
            "total_peers": 0, "established_peers": 0, "non_established_peers": []
        }
        for line in output.splitlines():
            if "BGP local router ID" in line:
                bgp_info["local_router_id"] = re.search(r'ID:\s+(\S+)', line).group(1)
            elif "Local AS number" in line:
                bgp_info["local_as_number"] = re.search(r'number:\s+(\d+)', line).group(1)
            elif "Total number of peers" in line:
                bgp_info["total_peers"] = int(re.search(r'peers:\s+(\d+)', line).group(1))
            elif "Peers in established state" in line:
                bgp_info["established_peers"] = int(re.search(r'established state:\s+(\d+)', line).group(1))
        return bgp_info if bgp_info["local_router_id"] else None

    def connect_and_get_sysnames_and_configs(self):
        nodes = self.telnet_info.get("node", [])
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(self.get_configuration_via_telnet, telnetlib.Telnet(node.get("hostip"), node.get("port"), timeout=10), node.get("image_type", "").lower()): node for node in nodes if node.get("hostip") and node.get("port")}
            for future in as_completed(futures):
                host_port = f"{futures[future]['hostip']}:{futures[future]['port']}"
                logging.info(f"[{host_port}] Configuration retrieval {'successful' if future.result() else 'failed'}.")

    def read_unl_file(self, lab_id: int):
        try:
            with open(f"/opt/unetlab/labs/{lab_id}.unl", 'r', encoding='utf-8') as f:
                self.parse_unl_file(f.read())
        except (FileNotFoundError, IOError) as e:
            logging.error(f"Error reading UNL file: {e}")

    def parse_unl_file(self, unl_content: str):
        try:
            root = ET.fromstring(unl_content)
            for node in root.findall(".//node"):
                node_name = node.get("name")
                for interface in node.findall("interface"):
                    self.data['node_interfaces'].setdefault(node_name, []).append({"name": interface.get("name"), "type": interface.get("type", "ethernet")})
            self.network_connections = [
                {"network_id": network_id, "node1": interfaces[0]["node_name"], "interface1": interfaces[0]["interface_name"],
                 "node2": interfaces[1]["node_name"], "interface2": interfaces[1]["interface_name"]}
                for network_id, interfaces in {
                    interface.get("network_id"): [{"node_name": node.get("name"), "interface_name": interface.get("name")} for node in root.findall(".//node") for interface in node.findall("interface") if interface.get("network_id")]
                    for interface in interfaces if len(interfaces) == 2
                }
            ]
        except ET.ParseError as e:
            logging.error(f"Error parsing UNL file: {e}")

    def collect_results(self) -> Dict[str, Any]:
        return {"telnet_devices": {
            host_port: {
                "sysname": sysname, "router_id": self.data['telnet_router_ids'].get(host_port, "未知 Router ID"),
                "ospf_neighbors": self.data['telnet_ospf_peers'].get(host_port, []), "interfaces": self.data['telnet_configurations'].get(host_port, []),
                "ospf_interfaces": self.data['telnet_ospf_interfaces'].get(host_port, []), "isis_interfaces": self.data['telnet_isis_interfaces'].get(host_port, []),
                "interface_status": [f"{iface['name']} 接口配置状态: 已配置IP地址, OSPF{'' if iface['name'] in self.data['telnet_ospf_interfaces'].get(host_port, []) else '未'}配置, ISIS{'' if iface['name'] in self.data['telnet_isis_interfaces'].get(host_port, []) else '未'}配置" for iface in self.data['node_interfaces'].get(sysname, [])],
                "ospf_status": "OSPF 配置正常" if host_port in self.data['telnet_router_ids'] else "OSPF 未配置", "isis_status": "ISIS 配置正常" if host_port in self.data['telnet_isis_peers_count'] else "ISIS 未配置",
                "bgp_info": self.data['telnet_bgp_info'].get(host_port, {})
            } for host_port, sysname in self.data['telnet_sysnames'].items()}, "network_connections": self.network_connections}


def main(input_path: str, output_path: str):
    base_path = "/uploadPath/reasoning"
    if "{t}" in input_path or "{t}" in output_path:
        latest_folder = max([f for f in os.listdir(base_path) if f.isdigit()], key=int)
        input_path, output_path = input_path.replace("{t}", latest_folder), output_path.replace("{t}", latest_folder)
    telnet_info = load_telnet_info(input_path)
    router_manager = RouterManager(telnet_info)
    router_manager.read_unl_file(telnet_info.get("labId"))
    router_manager.connect_and_get_sysnames_and_configs()
    mapping = router_manager.collect_results()
    write_output(output_path, mapping)
    write_interface_status(os.path.join(os.path.dirname(output_path), "data.txt"), mapping)


def load_telnet_info(input_path: str) -> Dict[str, Any]:
    try:
        with open(input_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logging.error(f"Error loading telnet_info: {e}")
        sys.exit(1)


def write_output(output_path: str, data: Dict[str, Any]):
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except IOError as e:
        logging.error(f"Error writing output: {e}")
        sys.exit(1)


def write_interface_status(data_txt_path: str, mapping: Dict[str, Any]):
    try:
        with open(data_txt_path, 'w', encoding='utf-8') as f:
            for host_port, device_info in mapping.get("telnet_devices", {}).items():
                f.write(f"节点: {device_info['sysname']} ({host_port})\n")
                for status in device_info.get("interface_status", []):
                    f.write(f"    接口: {status}\n")
                f.write(f"    OSPF 状态: {device_info.get('ospf_status')}\n")
                f.write(f"    ISIS 状态: {device_info.get('isis_status')}\n")
                if "bgp_local_router_id" in device_info.get("bgp_info", {}):
                    for key, value in device_info["bgp_info"].items():
                        f.write(f"    BGP {key.replace('_', ' ').title()}: {value}\n")
                else:
                    f.write(f"    BGP 未配置\n")
                f.write("\n")
    except IOError as e:
        logging.error(f"Error writing interface status: {e}")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process router configurations from param.json.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json, use {t} for latest folder number.")
    parser.add_argument("-o", "--output", required=True, help="Output path for process information, use {t} for latest folder number.")
    args = parser.parse_args()
    main(args.input, args.output)
