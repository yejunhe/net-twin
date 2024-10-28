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
        self.telnet_configurations: Dict[str, List[Dict[str, str]]] = {}  # Structured interface info
        self.telnet_ospf_interfaces: Dict[str, List[str]] = {}  # Interfaces configured with OSPF
        self.telnet_router_ids: Dict[str, str] = {}  # Router IDs per device
        self.telnet_ospf_peers: Dict[str, List[str]] = {}  # OSPF neighbor Router IDs per device
        self.telnet_isis_interfaces: Dict[str, List[str]] = {}  # Interfaces configured with ISIS
        self.telnet_isis_peer_counts: Dict[str, int] = {}  # Number of ISIS peers per device
        self.network_connections: Optional[List[Dict[str, Any]]] = None
        self.node_interfaces: Dict[str, List[Dict[str, str]]] = {}  # Node interface info from UNL
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
                    
                    # Parse 'display ospf interface'
                    if 'display ospf interface' in command_outputs:
                        ospf_interfaces = self.parse_display_ospf_interface(command_outputs['display ospf interface'])
                        self.telnet_ospf_interfaces[key] = ospf_interfaces
                    else:
                        logging.warning(f"[{tn.host}:{tn.port}] Missing 'display ospf interface' output.")
                    
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
                    
                    # Parse 'display isis interface'
                    if 'display isis interface' in command_outputs:
                        isis_output = command_outputs['display isis interface']
                        isis_interfaces = self.parse_display_isis_interface(isis_output)
                        self.telnet_isis_interfaces[key] = isis_interfaces
                    else:
                        logging.warning(f"[{tn.host}:{tn.port}] Missing 'display isis interface' output.")
                    
                    # Parse 'display isis peer'
                    if 'display isis peer' in command_outputs:
                        isis_peer_output = command_outputs['display isis peer']
                        isis_peer_count = self.parse_display_isis_peer(isis_peer_output)
                        if isis_peer_count is not None:
                            self.telnet_isis_peer_counts[key] = isis_peer_count
                    else:
                        logging.warning(f"[{tn.host}:{tn.port}] Missing 'display isis peer' output.")
        else:
            logging.warning(f"[{tn.host}:{tn.port}] No output received from Telnet commands.")
        return command_outputs

    def parse_display_ospf_peer(self, output: str) -> (Optional[str], Optional[List[str]]):
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

    def parse_display_isis_interface(self, output: str) -> List[str]:
        """
        Parse the output of 'display isis interface' to extract ISIS-configured interfaces.

        :param output: Output of the command.
        :return: List of ISIS-configured interface names in standardized format.
        """
        interfaces = []
        lines = output.splitlines()
        logging.debug("Parsing 'display isis interface' output.")
        
        # Skip header lines until the data starts
        data_started = False
        for line in lines:
            if line.strip().startswith("Interface"):
                data_started = True
                continue
            if not data_started:
                continue
            if not line.strip() or re.match(r'^[-=]+$', line):
                continue
            # Example line:
            # Eth1/0/0          001         Up          Mtu:Dn/Lnk:Dn/IP:Dn 1497 L1/L2 No/No
            parts = line.split()
            if len(parts) < 1:
                continue
            iface = parts[0]
            # Map interface name to standardized format
            if iface.lower().startswith('eth'):
                iface_number = iface[3:]  # Remove 'Eth' prefix
                iface_formatted = f"Ethernet{iface_number}"
            else:
                iface_formatted = iface.capitalize()  # e.g., 'Loop0' stays as 'Loop0'
            interfaces.append(iface_formatted)
            logging.debug(f"Detected ISIS-configured interface: {iface_formatted}")
        
        logging.debug(f"Parsed ISIS interfaces: {interfaces}")
        return interfaces

    def parse_display_isis_peer(self, output: str) -> Optional[int]:
        """
        Parse the output of 'display isis peer' to extract the total number of ISIS peers.

        :param output: Output of the command.
        :return: Number of ISIS peers. Returns None if parsing fails.
        """
        total_peers = None
        lines = output.splitlines()
        logging.debug("Parsing 'display isis peer' output.")
        total_peers_regex = re.compile(r'Total Peer\(s\):\s+(\d+)')

        for line in lines:
            match = total_peers_regex.search(line)
            if match:
                total_peers = int(match.group(1))
                logging.debug(f"Detected Total ISIS Peers: {total_peers}")
                break

        if total_peers is not None:
            logging.info(f"Extracted Total ISIS Peers: {total_peers}")
            return total_peers
        else:
            logging.info("No 'Total Peer(s)' information found in 'display isis peer' output.")
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
        headers = []

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
                    headers = re.split(r'\s{2,}', line)
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

    def parse_display_ospf_interface(self, output: str) -> List[str]:
        """
        Parse the output of 'display ospf interface' to extract OSPF-configured interfaces.

        :param output: Command output.
        :return: List of OSPF-configured interface names in standardized format.
        """
        interfaces = []
        lines = output.splitlines()
        parsing = False  # Flag to indicate if parsing has started

        # Regular expression to match interface lines
        interface_regex = re.compile(r'^\s*(?P<interface>\S+)\s+[\d\.]+')

        for line in lines:
            if "Interfaces" in line:
                parsing = True
                logging.debug("Starting to parse OSPF interface information.")
                continue
            if parsing:
                # Skip empty lines and separator lines
                if not line.strip() or re.match(r'^[-=]+$', line):
                    continue
                # Match interface lines
                match = interface_regex.match(line)
                if match:
                    iface = match.group('interface')
                    logging.debug(f"Found OSPF interface: {iface}")
                    # Format interface name
                    if iface.lower().startswith('eth'):
                        iface_number = iface[3:]  # Remove 'Eth' prefix
                        iface_formatted = f"Ethernet{iface_number}"
                        interfaces.append(iface_formatted)
                        logging.debug(f"Formatted OSPF interface name: {iface_formatted}")
                    elif iface.lower().startswith('loop'):
                        # Handle Loop interfaces like Loop0
                        iface_formatted = iface.capitalize()
                        interfaces.append(iface_formatted)
                        logging.debug(f"Formatted OSPF interface name: {iface_formatted}")
                    else:
                        # Handle other interface types as needed
                        iface_formatted = iface.capitalize()
                        interfaces.append(iface_formatted)
                        logging.debug(f"Formatted OSPF interface name: {iface_formatted}")
                else:
                    logging.debug(f"Unmatched OSPF interface line: {line}")
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
        """
        Collect all results, perform interface matching, and output interface configuration status including OSPF and ISIS statuses.
        """
        # Store interface status information
        interface_status = {}
        ospf_status = {}
        isis_status = {}

        for host_port, sysname in self.telnet_sysnames.items():
            node_interfaces = self.node_interfaces.get(sysname, [])
            telnet_interfaces = self.telnet_configurations.get(host_port, [])
            ospf_interfaces = self.telnet_ospf_interfaces.get(host_port, [])
            isis_interfaces = self.telnet_isis_interfaces.get(host_port, [])
            router_id = self.telnet_router_ids.get(host_port)
            ospf_neighbors = self.telnet_ospf_peers.get(host_port)
            isis_peer_count = self.telnet_isis_peer_counts.get(host_port)

            # Extract interface names from Telnet and convert to lowercase for comparison
            telnet_interface_names = [iface['Interface'].lower() for iface in telnet_interfaces if isinstance(iface, dict)]
            ospf_interfaces_lower = [iface.lower() for iface in ospf_interfaces]
            isis_interfaces_lower = [iface.lower() for iface in isis_interfaces]

            logging.debug(f"[{host_port}] Telnet fetched interfaces: {telnet_interface_names}")
            logging.debug(f"[{host_port}] OSPF-configured interfaces: {ospf_interfaces_lower}")
            logging.debug(f"[{host_port}] ISIS-configured interfaces: {isis_interfaces_lower}")

            interface_status[host_port] = []
            ospf_status[host_port] = ""
            isis_status[host_port] = ""

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
                    config_status = "未配置IP地址"

                # Determine OSPF configuration status
                if iface_formatted_lower in ospf_interfaces_lower:
                    ospf_iface_status = "OSPF已配置"
                else:
                    ospf_iface_status = "OSPF未配置"

                # Determine ISIS configuration status
                if iface_formatted_lower in isis_interfaces_lower:
                    isis_iface_status = "ISIS已配置"
                else:
                    isis_iface_status = "ISIS未配置"

                status = f"{iface_formatted}接口配置状态: {config_status}, {ospf_iface_status}, {isis_iface_status}"
                interface_status[host_port].append(status)
                logging.info(f"[{host_port}] {status}")

            # Determine OSPF status based on 'display ospf peer' and OSPF interfaces
            if host_port in self.telnet_router_ids:
                if self.telnet_ospf_peers.get(host_port):
                    neighbors_str = ', '.join(self.telnet_ospf_peers[host_port])
                    ospf_status[host_port] = f"OSPF 配置正常，邻居 Router IDs: {neighbors_str}"
                else:
                    if ospf_interfaces:
                        ospf_status[host_port] = "OSPF 配置问题：未检测到邻居 Router ID"
                    else:
                        ospf_status[host_port] = "OSPF 未配置"
            else:
                if ospf_interfaces:
                    ospf_status[host_port] = "OSPF 配置问题：未检测到 Router ID"
                else:
                    ospf_status[host_port] = "OSPF 未配置"

            # Determine ISIS status based on 'display isis peer' and ISIS interfaces
            if isis_peer_count is not None:
                if isis_peer_count > 0:
                    isis_status[host_port] = f"ISIS 配置正常，邻居数: {isis_peer_count}"
                else:
                    if isis_interfaces:
                        isis_status[host_port] = "ISIS 配置问题：未检测到邻居"
                    else:
                        isis_status[host_port] = "ISIS 未配置"
            else:
                if isis_interfaces:
                    isis_status[host_port] = "ISIS 配置问题：未检测到邻居"
                else:
                    isis_status[host_port] = "ISIS 未配置"

            logging.info(f"[{host_port}] {ospf_status[host_port]}")
            logging.info(f"[{host_port}] {isis_status[host_port]}")

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
                    "isis_status": isis_status.get(host_port, "未配置 ISIS")
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
        Write interface status, OSPF status, and ISIS status to data.txt in the following format:
        节点: sysname1 (host:port)
            接口: Ethernet1/0/0接口配置状态: 已配置IP地址, OSPF已配置, ISIS未配置
            接口: Ethernet1/0/1接口配置状态: 已配置IP地址, OSPF未配置, ISIS已配置
            接口: Loop0接口配置状态: 已配置IP地址, OSPF未配置, ISIS未配置
            OSPF 状态: OSPF 配置正常，邻居 Router IDs: 2.2.2.2, 1.1.1.1
            ISIS 状态: ISIS 配置正常，邻居数: 1

        节点: sysname2 (host:port)
            接口: Ethernet1/0/2接口配置状态: 已配置IP地址, OSPF未配置, ISIS未配置
            接口: Loop0接口配置状态: 已配置IP地址, OSPF未配置, ISIS未配置
            OSPF 状态: OSPF 未配置
            ISIS 状态: ISIS 未配置
        """
        try:
            with open(data_txt_path, 'w', encoding='utf-8') as f:
                telnet_devices = mapping.get("telnet_devices", {})
                for host_port, device_info in telnet_devices.items():
                    sysname = device_info.get("sysname", "未知节点")
                    interface_status_list = device_info.get("interface_status", [])
                    ospf_status = device_info.get("ospf_status", "未配置 OSPF")
                    isis_status = device_info.get("isis_status", "未配置 ISIS")
                    f.write(f"节点: {sysname} ({host_port})\n")
                    for status in interface_status_list:
                        f.write(f"    接口: {status}\n")
                    f.write(f"    OSPF 状态: {ospf_status}\n")
                    f.write(f"    ISIS 状态: {isis_status}\n\n")  # Add OSPF and ISIS status with a blank line
            logging.info(f"接口状态已写入 {data_txt_path}")
        except IOError as e:
            logging.error(f"写入 {data_txt_path} 时出错: {e}")
            sys.exit(1)

    def main(self, input_path: str, output_path: str):
        base_path = "/uploadPath/reasoning"
        if "{t}" in input_path or "{t}" in output_path:
            latest_folder = self.find_latest_folder(base_path)
            input_path = input_path.replace("{t}", latest_folder)
            output_path = output_path.replace("{t}", latest_folder)
            logging.debug(f"Resolved input_path: {input_path}")
            logging.debug(f"Resolved output_path: {output_path}")

        telnet_info = self.load_telnet_info(input_path)
        router_manager = self  # Since main is within the class

        # Read UNL file based on labId
        lab_id = telnet_info.get("labId")
        if lab_id is not None:
            router_manager.read_unl_file(lab_id)
        else:
            logging.warning("labId not found in telnet_info.")

        router_manager.connect_and_get_sysnames_and_configs()
        mapping = router_manager.collect_results()

        logging.info("Collected router configurations:")
        logging.info(json.dumps(mapping, indent=4, ensure_ascii=False))
        self.write_output(output_path, mapping)

        # Define the path for data.txt, placed in the same directory as output_path
        output_dir = os.path.dirname(output_path)
        data_txt_path = os.path.join(output_dir, "data.txt")
        self.write_interface_status(data_txt_path, mapping)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process router configurations from param.json.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json, use {t} for latest folder number.")
    parser.add_argument("-o", "--output", required=True, help="Output path for process information, use {t} for latest folder number.")
    args = parser.parse_args()

    # Instantiate RouterManager and run main
    manager = RouterManager({})
    manager.main(args.input, args.output)
