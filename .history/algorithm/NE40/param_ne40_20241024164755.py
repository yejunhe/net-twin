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
        self.node_interfaces: Dict[str, List[Dict[str, str]]] = {}  # Store node interface info from UNL
        self.max_workers = max_workers
        self.telnet_lock = Lock()
        # Define command sequences for different device types
        self.commands_map = {
            "huaweine40": (['scr 0 t', 'display ip interface brief'], b'quit\n')  # Updated quit command
        }

    def execute_telnet_commands(self, tn: telnetlib.Telnet, commands: List[str], quit_cmd: bytes) -> str:
        """
        Execute a sequence of Telnet commands and capture the output.
        """
        try:
            tn.write(b'\n')
            time.sleep(1)
            initial_output = tn.read_very_eager().decode('ascii', errors='ignore')
            logging.debug(f"[{tn.host}:{tn.port}] Initial Telnet output:\n{initial_output}")

            full_output = initial_output

            for cmd in commands:
                tn.write(cmd.encode('ascii') + b'\n')
                logging.info(f"[{tn.host}:{tn.port}] Sending command: {cmd}")
                time.sleep(1)  # Wait for command to execute

                # Wait for the prompt after executing the command
                index, match, cmd_output = tn.expect([b'>', b'#', b'\$'], timeout=10)
                if index == -1:
                    logging.warning(f"[{tn.host}:{tn.port}] No prompt detected after command '{cmd}'.")
                else:
                    logging.debug(f"[{tn.host}:{tn.port}] Detected prompt after command '{cmd}': {match.group(0).decode('ascii', errors='ignore')}")
                
                # Read all available data
                cmd_output = tn.read_very_eager().decode('ascii', errors='ignore')
                full_output += cmd_output
                logging.debug(f"[{tn.host}:{tn.port}] Output for '{cmd}':\n{cmd_output}")

            # Send quit command to exit Telnet session if not already in prompt
            prompt = self.get_prompt(tn)
            if prompt and not (prompt.startswith('<') and prompt.endswith('>')):
                tn.write(quit_cmd)
                logging.info(f"[{tn.host}:{tn.port}] Sending quit command.")
                time.sleep(1)
                quit_output = tn.read_very_eager().decode('ascii', errors='ignore')
                full_output += quit_output
                logging.debug(f"[{tn.host}:{tn.port}] Output after quit command:\n{quit_output}")

            return full_output
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] Telnet Error: {e}")
            return ""

    def get_prompt(self, tn: telnetlib.Telnet) -> Optional[str]:
        """
        Retrieve the current prompt from the Telnet session.
        """
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
        """
        Extract the system name from the Telnet session output.
        """
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
        """
        Execute configuration commands via Telnet and capture the output.
        """
        # Find matching device type based on partial image_type
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
                    # Parse the command output
                    parsed_interfaces = self.parse_display_ip_interface_brief(output)
                    self.telnet_configurations[key] = parsed_interfaces
        else:
            logging.warning(f"[{tn.host}:{tn.port}] No output received from Telnet commands.")
        return output

    def connect_and_get_sysnames_and_configs(self):
        """
        Establish Telnet connections to all nodes and retrieve configurations.
        """
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
                        tn.host, tn.port = host, port  # Assign for logging purposes
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
        Collect all results and match interface configurations.
        """
        # Store interface status information
        interface_status = {}

        for host_port, sysname in self.telnet_sysnames.items():
            node_interfaces = self.node_interfaces.get(sysname, [])
            telnet_interfaces = self.telnet_configurations.get(host_port, [])

            # Extract list of interface names from Telnet output
            telnet_interface_names = [iface['Interface'] for iface in telnet_interfaces if isinstance(iface, dict)]

            logging.debug(f"[{host_port}] Telnet 获取的接口列表: {telnet_interface_names}")

            interface_status[host_port] = []

            for iface in node_interfaces:
                # Format interface name, e.g., type="ethernet" name="e1/0/0" => "Ethernet1/0/0"
                iface_name = iface['name']
                if iface_name.lower().startswith('e'):
                    iface_number = iface_name[1:]  # Remove 'e' prefix
                    iface_formatted = f"Ethernet{iface_number}"
                else:
                    # If interface name does not start with 'e', format accordingly
                    iface_formatted = f"{iface['type'].capitalize()}{iface['name']}"

                logging.debug(f"[{host_port}] 格式化后的接口名称: {iface_formatted}")

                if iface_formatted in telnet_interface_names:
                    status = f"{iface_formatted}接口已配置"
                else:
                    status = f"{iface_formatted}接口未配置"
                interface_status[host_port].append(status)
                logging.info(f"[{host_port}] {status}")

        return {
            "telnet_devices": {
                host_port: {
                    "sysname": sysname,
                    "interfaces": self.telnet_configurations.get(host_port, []),
                    "interface_status": interface_status.get(host_port, [])
                }
                for host_port, sysname in self.telnet_sysnames.items()
            },
            "network_connections": self.network_connections
        }

    def read_unl_file(self, lab_id: int):
        """
        Read and parse the UNL file based on lab_id.
        """
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
        Parse the UNL file to extract network connections and node interfaces.
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

    def parse_display_ip_interface_brief(self, output: str) -> List[Dict[str, str]]:
        """
        Parse the output of 'display ip interface brief' command, excluding interfaces with 'unassigned' IP.

        :param output: The output content of the command.
        :return: A list of interface information dictionaries.
        """
        lines = output.splitlines()
        interfaces = []
        header_found = False

        # Regular expression to match interface lines
        interface_regex = re.compile(
            r'^(?P<interface>\S+)\s+'
            r'(?P<ip_address>(?:\d{1,3}\.){3}\d{1,3}/\d{1,2}|unassigned)\s+'
            r'(?P<physical>up|down)\s+'
            r'(?P<protocol>up|down)\s+'
            r'(?P<vpn>\S+)'
        )

        for line in lines:
            # Look for the header
            if not header_found:
                if re.match(r'^Interface\s+IP Address/Mask\s+Physical\s+Protocol\s+VPN', line):
                    header_found = True
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
                else:
                    # Optionally log lines that don't match expected format
                    logging.debug(f"Line does not match interface regex: {line}")
                    continue

        logging.debug(f"Parsed Telnet interfaces: {interfaces}")
        return interfaces

def find_latest_folder(base_path: str) -> str:
    """
    Find the latest numbered folder in the base path.

    :param base_path: The base directory path.
    :return: The name of the latest numbered folder.
    """
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
    """
    Load Telnet connection information from a JSON file.

    :param input_path: Path to the param.json file.
    :return: A dictionary with Telnet connection information.
    """
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

def write_output(data_txt_path: str, data: Dict[str, Any]):
    """
    Write interface status information to data.txt and other data to /tmp/output.json.

    :param data_txt_path: Path to the output data.txt file.
    :param data: The collected data dictionary.
    """
    try:
        # Extract and group interface status information by node
        telnet_devices = data.get("telnet_devices", {})

        with open(data_txt_path, 'w', encoding='utf-8') as f:
            for host_port, device_info in telnet_devices.items():
                sysname = device_info.get("sysname", "Unknown")
                interface_status = device_info.get("interface_status", [])

                f.write(f"节点: {sysname}\n")
                for status in interface_status:
                    f.write(f"  {status}\n")
                f.write("\n")  # Add empty line between nodes

        logging.info(f"Interface status written to {data_txt_path}")

        # Write other data to /tmp/output.json
        tmp_output_path = '/tmp/output.json'
        with open(tmp_output_path, 'w', encoding='utf-8') as f:
            # Remove interface_status field
            telnet_devices_clean = {
                host_port: {
                    "sysname": info["sysname"],
                    "interfaces": info["interfaces"]
                }
                for host_port, info in telnet_devices.items()
            }
            output_data = {
                "telnet_devices": telnet_devices_clean,
                "network_connections": data.get("network_connections", [])
            }
            json.dump(output_data, f, indent=4, ensure_ascii=False)
        logging.info(f"Other data written to {tmp_output_path}")
    except IOError as e:
        logging.error(f"Error writing to output files: {e}")
        sys.exit(1)

def main(input_path: str, data_txt_path: str):
    """
    Main function to execute the script.

    :param input_path: Path to the param.json file.
    :param data_txt_path: Path to the output data.txt file.
    """
    base_path = "/uploadPath/reasoning"
    if "{t}" in input_path:
        latest_folder = find_latest_folder(base_path)
        input_path = input_path.replace("{t}", latest_folder)
        logging.debug(f"Resolved input_path: {input_path}")

    telnet_info = load_telnet_info(input_path)
    router_manager = RouterManager(telnet_info)

    # Read UNL file based on labId
    lab_id = telnet_info.get("labId")
    if lab_id is not None:
        router_manager.read_unl_file(lab_id)
    else:
        logging.warning("labId not found in telnet_info.")

    router_manager.connect_and_get_sysnames_and_configs()
    mapping = router_manager.collect_results()

    logging.info("Collected router configurations:")
    logging.debug(json.dumps(mapping, indent=4, ensure_ascii=False))
    
    # Write outputs
    write_output(data_txt_path, mapping)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process router configurations from param.json.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json, use {t} for latest folder number.")
    parser.add_argument("-o", "--output", required=True, help="Path to output data.txt file.")
    args = parser.parse_args()
    main(args.input, args.output)
