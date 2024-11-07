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
        self.network_connections: Optional[List[Dict[str, Any]]] = None
        self.node_interfaces: Dict[str, List[Dict[str, str]]] = {}  # Node interface info from UNL
        self.max_workers = max_workers
        self.telnet_lock = Lock()
        # Define command sequences for different device types
        self.commands_map = {
            "h3c": (
                [
                    'screen-length disable',
                    'display ip interface brief'
                ],
                b'quit\n'
            )
        }

        # Interface type mapping dictionary
        self.interface_type_map = {
            'gigabit': 'GigabitEthernet',
            'gi': 'GigabitEthernet',
            'ge': 'GigabitEthernet',
            'fast': 'FastEthernet',
            'loopback': 'Loopback',
            'ethernet': 'GigabitEthernet', 
            # Add more mappings as needed
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
            if prompt and not (prompt.startswith('<') and prompt.endswith('>') or prompt.startswith('[') and prompt.endswith(']')):
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
                # Handle <R5> format
                if line.startswith('<') and line.endswith('>'):
                    sysname = line.strip('<> ').strip()
                    logging.info(f"[{tn.host}:{tn.port}] Detected sysname: {sysname}")
                    return sysname
                # Handle [~R1] format
                elif line.startswith('[') and line.endswith(']'):
                    content = line.strip('[]').strip()
                    # Remove ~ if present
                    if content.startswith('~'):
                        sysname = content[1:]
                    else:
                        sysname = content
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
        Parse the output of 'display ip interface brief', exclude interfaces with IP Address/Mask as '--',
        and return structured data.

        :param output: Command output.
        :return: List of interface information dictionaries with IP configured.
        """
        lines = output.splitlines()
        interfaces = []
        header_found = False

        # Regular expression to match interface lines
        interface_regex = re.compile(
            r'^\s*(?P<interface>\S+)\s+'                        # Interface
            r'(?P<physical>up|down)\s+'                         # Physical
            r'(?P<protocol>up(?:\([sl]+\))?|down(?:\([sl]+\))?)\s+'  # Protocol with optional flags (s, l)
            r'(?P<ip_address>(?:\d{1,3}\.){3}\d{1,3}/\d{1,2}|--)\s+'   # IP address/Mask or --
            r'(?P<vpn>\S+)\s+'                                   # VPN instance
            r'(?P<description>.*)$'                              # Description
        )

        for line in lines:
            # 查找表头
            if not header_found:
                if re.match(r'^Interface\s+Physical\s+Protocol\s+IP address/Mask\s+VPN instance\s+Description', line):
                    header_found = True
                    logging.debug("找到 'display ip interface brief' 表头。")
                continue
            else:
                # 跳过空行或分隔线
                if not line.strip() or re.match(r'^[-=]+$', line):
                    continue

                match = interface_regex.match(line)
                if match:
                    ip_address = match.group('ip_address').replace('unassigned', '--')  # Replace 'unassigned' with '--'
                    vpn = match.group('vpn').replace('unassigned', '--')  # Replace 'unassigned' with '--'
                    description = match.group('description').strip().replace('unassigned', '--')  # Replace 'unassigned' with '--'

                    # Only add interfaces where IP Address/Mask is not '--'
                    if ip_address != '--':
                        interface_info = {
                            'Interface': match.group('interface'),
                            'Physical': match.group('physical'),
                            'Protocol': match.group('protocol'),
                            'IP Address/Mask': ip_address,
                            'VPN': vpn,
                            'Description': description
                        }
                        interfaces.append(interface_info)
                        logging.debug(f"Parsed interface info: {interface_info}")
                    else:
                        logging.debug(f"Interface {match.group('interface')} has IP Address/Mask '--', skipping.")
                else:
                    logging.debug(f"No match in 'display ip interface brief' for line: {line}")
                    continue

        logging.debug(f"Parsed Telnet interfaces (only those with IP configured): {interfaces}")
        return interfaces

    def standardize_interface_name(self, interface: str) -> str:
        """
        Standardize the interface name format based on known prefixes.

        :param interface: Original interface name.
        :return: Standardized interface name.
        """
        # Example standardization based on common interface prefixes
        prefix_map = {
            'GE': 'GigabitEthernet',
            'Gi': 'GigabitEthernet',
            'Fa': 'FastEthernet',
            'Ethernet': 'Ethernet',
            'GigabitEthernet': 'GigabitEthernet',
            'FastEthernet': 'FastEthernet',
            'Loop': 'Loopback',
            # Add more mappings as needed
        }

        for short, full in prefix_map.items():
            if interface.startswith(short):
                return interface.replace(short, full, 1)
        return interface  # Return as-is if no mapping found

    def connect_and_get_sysnames_and_configs(self):
        nodes = self.telnet_info.get("node", [])
        if not nodes:
            logging.warning("No nodes found in telnet_info.")
            return

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_node = {}
            for node in nodes:
                image_type = node.get("image_type", "").lower()
                if "h3c" in image_type:
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
            telnet_interface_names = [
                self.standardize_interface_name(iface['Interface']).lower() for iface in telnet_interfaces
                if isinstance(iface, dict) and self.standardize_interface_name(iface['Interface'])
            ]

            logging.debug(f"[{host_port}] Telnet fetched interfaces: {telnet_interface_names}")

            interface_status[host_port] = ""

            for iface in node_interfaces:
                iface_type = iface['type'].lower()
                iface_name = iface['name']

                # Get the standardized interface prefix from the mapping
                iface_prefix = self.interface_type_map.get(iface_type, iface_type.capitalize())

                # Remove any prefixes like 'Gi', 'Fa' from the interface name if present
                iface_number = re.sub(r'^(Gi|Fa|Ethernet|GigabitEthernet|FastEthernet)', '', iface_name, flags=re.IGNORECASE)

                # Format the interface name
                iface_formatted = f"{iface_prefix}{iface_number}"

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

class HistoryManager:
    """Manage history records, including loading and saving history JSON files"""

    HISTORY_DIR = '/opt/unetlab/labs_history'

    def __init__(self, lab_id):
        self.lab_id = lab_id
        self.history_path = self.get_history_path()

    def get_history_path(self):
        try:
            if not os.path.exists(self.HISTORY_DIR):
                logging.info(f"History directory does not exist. Creating: {self.HISTORY_DIR}")
                os.makedirs(self.HISTORY_DIR, exist_ok=True)
                logging.info(f"History directory created: {self.HISTORY_DIR}")
            else:
                logging.info(f"History directory exists: {self.HISTORY_DIR}")
        except Exception as e:
            logging.error(f"Cannot create history directory {self.HISTORY_DIR}. Error: {e}")
            raise IOError(f"Cannot create history directory {self.HISTORY_DIR}. Error: {e}")
        return os.path.join(self.HISTORY_DIR, f"{self.lab_id}.json")

    def load_history(self):
        if not os.path.exists(self.history_path):
            logging.info(f"History file does not exist: {self.history_path}")
            return None
        logging.info(f"Loading history file: {self.history_path}")
        with open(self.history_path, 'r', encoding='utf-8') as f:
            try:
                history = json.load(f)
                logging.info("History file loaded successfully.")
                return history
            except json.JSONDecodeError as e:
                logging.warning(f"Cannot parse history JSON file {self.history_path}. Error: {e}")
                return None

    def save_history(self, data):
        try:
            with open(self.history_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            logging.info(f"History saved to {self.history_path}")
        except Exception as e:
            logging.error(f"Cannot save history to {self.history_path}. Error: {e}")
            raise IOError(f"Cannot save history to {self.history_path}. Error: {e}")

class TopologyComparator:
    """Compare current JSON with history JSON to detect topology changes"""

    IGNORED_FIELDS = {'left', 'top', 'uuid'}

    def __init__(self, current, history):
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

    def compare(self):
        if self.history is None:
            logging.info("No history to compare with.")
            return None  # No history

        # Compare nodes
        current_nodes = {node['id']: node for node in self.current['topology'].get('nodes', [])}
        history_nodes = {node['id']: node for node in self.history['topology'].get('nodes', [])}

        # Detect added nodes
        for node_id in current_nodes:
            if node_id not in history_nodes:
                self.differences['added_nodes'].append(current_nodes[node_id])

        # Detect removed nodes
        for node_id in history_nodes:
            if node_id not in current_nodes:
                self.differences['removed_nodes'].append(history_nodes[node_id])

        # Detect modified nodes
        for node_id in current_nodes:
            if node_id in history_nodes:
                filtered_current = self.filter_ignored_fields(current_nodes[node_id])
                filtered_history = self.filter_ignored_fields(history_nodes[node_id])
                if filtered_current != filtered_history:
                    changes = self.find_changes(filtered_history, filtered_current)
                    self.differences['modified_nodes'].append({
                        'id': node_id,
                        'changes': changes
                    })

        # Compare networks
        current_networks = {network['id']: network for network in self.current['topology'].get('networks', [])}
        history_networks = {network['id']: network for network in self.history['topology'].get('networks', [])}

        # Detect added networks
        for network_id in current_networks:
            if network_id not in history_networks:
                self.differences['added_networks'].append(current_networks[network_id])

        # Detect removed networks
        for network_id in history_networks:
            if network_id not in current_networks:
                self.differences['removed_networks'].append(history_networks[network_id])

        # Detect modified networks
        for network_id in current_networks:
            if network_id in history_networks:
                filtered_current = self.filter_ignored_fields(current_networks[network_id])
                filtered_history = self.filter_ignored_fields(history_networks[network_id])
                if filtered_current != filtered_history:
                    changes = self.find_changes(filtered_history, filtered_current)
                    self.differences['modified_networks'].append({
                        'id': network_id,
                        'changes': changes
                    })

        # Remove empty categories
        self.differences = {k: v for k, v in self.differences.items() if v}
        return self.differences

    def filter_ignored_fields(self, item):
        """Return a new dictionary excluding ignored fields"""
        return {k: v for k, v in item.items() if k not in self.IGNORED_FIELDS}

    def find_changes(self, old, new):
        """Find differences between two dictionaries"""
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

class TopologyChangeDetector:
    """Main controller class to coordinate components for topology change detection"""

    def __init__(self, input_path, output_path):
        self.input_path = input_path
        self.output_path = output_path

    def run(self):
        start_time = time.time()
        execution_result = "成功"

        try:
            # Read param.json
            telnet_info = load_telnet_info(self.input_path)
            lab_id = telnet_info.get('labId')
            if lab_id is None:
                raise ValueError("param.json 中缺少 'labId' 字段。")

            # Parse UNL file
            unl_parser = UnlParser(lab_id)
            current_json = unl_parser.parse_unl_to_json()

            # Load history
            history_manager = HistoryManager(lab_id)
            history_json = history_manager.load_history()

            # Compare current JSON with history JSON
            comparator = TopologyComparator(current_json, history_json)
            differences = comparator.compare()

            return differences, start_time, execution_result

        except Exception as e:
            execution_result = "失败"
            end_time = time.time()
            formatted_diff = self.format_error_report(start_time, end_time, str(e))
            logging.error(f"拓扑变更检测失败。错误信息: {e}")
            return {"error": str(e)}, start_time, "失败"

    def format_error_report(self, start_time, end_time, error_message):
        report_lines = []
        report_lines.append("=" * 50)
        report_lines.append("拓扑变更检测报告")
        report_lines.append(f"执行时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start_time))} 至 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(end_time))}")
        report_lines.append("执行结果：失败")
        report_lines.append("变更内容：")
        report_lines.append("-" * 50)
        report_lines.append(f"错误信息: {error_message}")
        report_lines.append("=" * 50)
        return "\n".join(report_lines)

class UnlParser:
    """Find and parse .unl files, converting them to JSON format"""

    LABS_DIR = '/opt/unetlab/labs'

    def __init__(self, lab_id):
        self.lab_id = lab_id
        self.unl_path = self.find_unl_file()
        self.lab_json = None

    def find_unl_file(self):
        unl_filename = f"{self.lab_id}.unl"
        unl_path = os.path.join(self.LABS_DIR, unl_filename)
        logging.info(f"Looking for .unl file: {unl_path}")
        if not os.path.exists(unl_path):
            logging.error(f"Corresponding .unl file not found: {unl_path}")
            raise FileNotFoundError(f"Corresponding .unl file not found: {unl_path}")
        logging.info(f"Found .unl file: {unl_path}")
        return unl_path

    def parse_unl_to_json(self):
        try:
            logging.info(f"Parsing .unl file: {self.unl_path}")
            tree = ET.parse(self.unl_path)
            root = tree.getroot()
            lab_json = {}
            lab_json['lab'] = {
                'name': root.attrib.get('name'),
                'id': root.attrib.get('id'),
                'version': root.attrib.get('version'),
                'scripttimeout': root.attrib.get('scripttimeout'),
                'lock': root.attrib.get('lock')
            }

            # Parse topology
            topology = root.find('topology')
            if topology is not None:
                # Parse nodes
                nodes = []
                nodes_elem = topology.find('nodes')
                if nodes_elem is not None:
                    for node in nodes_elem.findall('node'):
                        node_dict = node.attrib.copy()
                        interfaces = []
                        for interface in node.findall('interface'):
                            interfaces.append(interface.attrib.copy())
                        node_dict['interfaces'] = interfaces
                        nodes.append(node_dict)
                lab_json['topology'] = {'nodes': nodes}

                # Parse networks
                networks = []
                networks_elem = topology.find('networks')
                if networks_elem is not None:
                    for network in networks_elem.findall('network'):
                        networks.append(network.attrib.copy())
                lab_json['topology']['networks'] = networks

            self.lab_json = lab_json
            logging.info("Successfully parsed .unl file.")
            return lab_json
        except ET.ParseError as e:
            logging.error(f"Cannot parse XML file {self.unl_path}. Error: {e}")
            raise ValueError(f"Cannot parse XML file {self.unl_path}. Error: {e}")

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

def write_output(output_path: str, data: Dict[str, Any]):
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)  # Use ensure_ascii=False to support Chinese
        logging.info(f"Mapping results written to {output_path}")
    except IOError as e:
        logging.error(f"Error writing to output file: {e}")
        sys.exit(1)

def write_interface_status(data_txt_path: str, mapping: Dict[str, Any]):
    """
    将接口状态写入 data.txt，格式如下：
    节点: sysname1 (host:port)
        接口: GigabitEthernet1/0接口配置状态: 已配置IP地址
    节点: sysname2 (host:port)
        接口: GigabitEthernet1/2接口配置状态: 未配置IP地址
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
        sys.exit(1)

def format_diff(differences, start_time, execution_result) -> str:
    """
    Format the topology differences into a readable report.
    """
    report_lines = []
    report_lines.append("=" * 50)
    report_lines.append("拓扑变更检测报告")
    report_lines.append(f"执行时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start_time))}")
    report_lines.append(f"执行结果：{execution_result}")
    report_lines.append("变更内容：")
    report_lines.append("-" * 50)

    if differences is None:
        report_lines.append("首次运行，无历史记录进行比较。")
    elif 'error' in differences:
        report_lines.append(f"拓扑变更检测失败。错误信息: {differences['error']}")
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
                        added_interfaces = get_added_interfaces(change.get('old', []), change.get('new', []))
                        removed_interfaces = get_removed_interfaces(change.get('old', []), change.get('new', []))
                        modified_interfaces = get_modified_interfaces(change.get('old', []), change.get('new', []))
                        if added_interfaces:
                            report_lines.append(f"   * 接口新增:")
                            for iface in added_interfaces:
                                report_lines.append(f"     - ID: {iface.get('id', 'N/A')}, Name: {iface.get('name', 'N/A')}, Type: {iface.get('type', 'N/A')}, Network ID: {iface.get('network_id', 'N/A')}")
                        if removed_interfaces:
                            report_lines.append(f"   * 接口删除:")
                            for iface in removed_interfaces:
                                report_lines.append(f"     - ID: {iface.get('id', 'N/A')}, Name: {iface.get('name', 'N/A')}, Type: {iface.get('type', 'N/A')}, Network ID: {iface.get('network_id', 'N/A')}")
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

def get_added_interfaces(old_interfaces, new_interfaces):
    """Identify added interfaces"""
    old_ids = {iface['id'] for iface in old_interfaces}
    added = [iface for iface in new_interfaces if iface['id'] not in old_ids]
    return added

def get_removed_interfaces(old_interfaces, new_interfaces):
    """Identify removed interfaces"""
    new_ids = {iface['id'] for iface in new_interfaces}
    removed = [iface for iface in old_interfaces if iface['id'] not in new_ids]
    return removed

def get_modified_interfaces(old_interfaces, new_interfaces):
    """Identify modified interfaces"""
    old_dict = {iface['id']: iface for iface in old_interfaces}
    new_dict = {iface['id']: iface for iface in new_interfaces}
    modified = []
    for iface_id in new_dict:
        if iface_id in old_dict:
            filtered_old = filter_ignored_fields(iface_id, old_dict[iface_id])
            filtered_new = filter_ignored_fields(iface_id, new_dict[iface_id])
            if filtered_old != filtered_new:
                changes = find_changes(filtered_old, filtered_new)
                modified.append({
                    'id': iface_id,
                    'changes': changes
                })
    return modified

def filter_ignored_fields(iface_id, item):
    """Return a new dictionary excluding ignored fields"""
    IGNORED_FIELDS = {'left', 'top', 'uuid'}
    return {k: v for k, v in item.items() if k not in IGNORED_FIELDS}

def find_changes(old, new):
    """Find differences between two dictionaries"""
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

class HistoryManager:
    """Manage history records, including loading and saving history JSON files"""

    HISTORY_DIR = '/opt/unetlab/labs_history'

    def __init__(self, lab_id):
        self.lab_id = lab_id
        self.history_path = self.get_history_path()

    def get_history_path(self):
        try:
            if not os.path.exists(self.HISTORY_DIR):
                logging.info(f"History directory does not exist. Creating: {self.HISTORY_DIR}")
                os.makedirs(self.HISTORY_DIR, exist_ok=True)
                logging.info(f"History directory created: {self.HISTORY_DIR}")
            else:
                logging.info(f"History directory exists: {self.HISTORY_DIR}")
        except Exception as e:
            logging.error(f"Cannot create history directory {self.HISTORY_DIR}. Error: {e}")
            raise IOError(f"Cannot create history directory {self.HISTORY_DIR}. Error: {e}")
        return os.path.join(self.HISTORY_DIR, f"拓扑{self.lab_id}.json")

    def load_history(self):
        if not os.path.exists(self.history_path):
            logging.info(f"History file does not exist: {self.history_path}")
            return None
        logging.info(f"Loading history file: {self.history_path}")
        with open(self.history_path, 'r', encoding='utf-8') as f:
            try:
                history = json.load(f)
                logging.info("History file loaded successfully.")
                return history
            except json.JSONDecodeError as e:
                logging.warning(f"Cannot parse history JSON file {self.history_path}. Error: {e}")
                return None

    def save_history(self, data):
        try:
            with open(self.history_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            logging.info(f"History saved to {self.history_path}")
        except Exception as e:
            logging.error(f"Cannot save history to {self.history_path}. Error: {e}")
            raise IOError(f"Cannot save history to {self.history_path}. Error: {e}")

class CombinedScriptManager:
    """Main controller class to coordinate router management and topology change detection"""

    def __init__(self, input_path: str, output_path: str):
        self.input_path = input_path
        self.output_path = output_path
        self.mapping = {}

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

    def run(self):
        # Replace {t} with the latest folder number if present
        base_path = "/uploadPath/reasoning"
        if "{t}" in self.input_path or "{t}" in self.output_path:
            latest_folder = self.find_latest_folder(base_path)
            self.input_path = self.input_path.replace("{t}", latest_folder)
            self.output_path = self.output_path.replace("{t}", latest_folder)
            logging.debug(f"Resolved input_path: {self.input_path}")
            logging.debug(f"Resolved output_path: {self.output_path}")

        # Load telnet_info
        telnet_info = self.load_telnet_info(self.input_path)
        router_manager = RouterManager(telnet_info)

        # Read and parse UNL file
        lab_id = telnet_info.get("labId")
        if lab_id is not None:
            router_manager.read_unl_file(lab_id)
        else:
            logging.warning("labId not found in telnet_info.")

        # Connect and get sysnames and configurations
        router_manager.connect_and_get_sysnames_and_configs()
        mapping = router_manager.collect_results()

        logging.info("Collected router configurations:")
        logging.info(json.dumps(mapping, indent=4, ensure_ascii=False))
        write_output(self.output_path, mapping)

        # Define the path for data.txt, placed in the same directory as output_path
        output_dir = os.path.dirname(self.output_path)
        data_txt_path = os.path.join(output_dir, "data.txt")
        write_interface_status(data_txt_path, mapping)

        # Run topology change detection
        topology_detector = TopologyChangeDetector(self.input_path, self.output_path)
        topology_differences, start_time, execution_result = topology_detector.run()

        # Get topology change report
        if topology_differences is None:
            topology_report = "首次运行，无历史记录进行比较。"
        elif 'error' in topology_differences:
            topology_report = f"拓扑变更检测失败。错误信息: {topology_differences['error']}"
        elif not topology_differences:
            topology_report = "无拓扑变化。"
        else:
            topology_report = format_diff(topology_differences, start_time, execution_result)

        # Append the topology change report to data.txt
        try:
            with open(data_txt_path, 'a', encoding='utf-8') as f:
                f.write("\n拓扑变更检测报告:\n")
                f.write(topology_report)
            logging.info(f"拓扑变更检测报告已追加到 {data_txt_path}")
        except IOError as e:
            logging.error(f"无法将拓扑变更检测报告追加到 {data_txt_path}。错误信息: {e}")
            sys.exit(1)

        # Save history (only if topology detection is successful and no errors)
        if topology_differences and 'error' not in topology_differences:
            try:
                unl_parser = UnlParser(lab_id)
                current_json = unl_parser.parse_unl_to_json()
                history_manager = HistoryManager(lab_id)
                history_manager.save_history(current_json)
                logging.info("History has been updated.")
            except Exception as e:
                logging.error(f"Cannot save history. Error: {e}")

        logging.info("All tasks completed.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process router configurations from param.json.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json, use {t} for latest folder number.")
    parser.add_argument("-o", "--output", required=True, help="Output path for process information, use {t} for latest folder number.")
    args = parser.parse_args()
    CombinedScriptManager(args.input, args.output).run()
