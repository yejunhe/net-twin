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
import ipaddress  # Added for subnet calculations

# Configure logging for better traceability and control
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

class RouterManager:
    def __init__(self, telnet_info: Dict[str, Any], max_workers: int = 20):
        self.telnet_info = telnet_info
        self.max_workers = max_workers
        self.telnet_lock = Lock()
        # Define command sequences for different device types
        self.commands_map = {
            "huaweine40": (['scr 0 t', 'display ip routing-table', 'display ip interface brief'], b'q\n')
        }
        # Dictionaries to store sysnames, filtered routing tables, and interfaces
        self.telnet_sysnames: Dict[str, str] = {}
        self.telnet_filtered_routing_tables: Dict[str, List[Dict[str, Any]]] = {}
        self.telnet_interfaces: Dict[str, List[Dict[str, Any]]] = {}

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

    def parse_routing_table(self, routing_table_output: str) -> List[Dict[str, Any]]:
        """
        Parses the routing table output and returns a list of dictionaries
        containing entries where Proto is 'Direct' and NextHop is neither '127.0.0.1' nor '192.168.0.49'.
        """
        filtered_entries = []
        lines = routing_table_output.splitlines()
        parsing = False
        for line in lines:
            if "Destination/Mask" in line and "Proto" in line:
                parsing = True
                continue
            if parsing:
                if not line.strip() or line.startswith('=') or line.startswith('---'):
                    continue
                # Split the line into columns based on whitespace, allowing 'Interface' to contain spaces
                parts = line.split(None, 6)
                if len(parts) < 7:
                    logging.debug(f"Skipping malformed line: {line}")
                    continue
                destination_mask = parts[0]
                proto = parts[1]
                pre = parts[2]
                cost = parts[3]
                flags = parts[4]
                next_hop = parts[5]
                interface = parts[6].strip()
                
                # Apply the filters: Proto is 'Direct' and NextHop is not '127.0.0.1' or '192.168.0.49'
                if proto.lower() == 'direct' and next_hop not in {'127.0.0.1', '192.168.0.49'}:
                    entry = {
                        "Destination/Mask": destination_mask,
                        "Proto": proto,
                        "Pre": pre,
                        "Cost": cost,
                        "Flags": flags,
                        "NextHop": next_hop,
                        "Interface": interface
                    }
                    filtered_entries.append(entry)
        logging.info(f"Parsed {len(filtered_entries)} filtered routing entries.")
        return filtered_entries

    def parse_interface_brief(self, interface_brief_output: str) -> List[Dict[str, Any]]:
        """
        Parses the 'display ip interface brief' output and returns a list of dictionaries
        containing 'Interface' and 'IP Address/Mask', excluding entries where 'IP Address/Mask' is 'unassigned'.
        """
        interfaces = []
        lines = interface_brief_output.splitlines()
        parsing = False
        for line in lines:
            if "Interface" in line and "IP Address/Mask" in line:
                parsing = True
                continue
            if parsing:
                if not line.strip() or line.startswith('=') or line.startswith('---'):
                    continue
                # Split the line into columns based on whitespace, allowing 'Interface' to contain spaces
                parts = line.split(None, 3)
                if len(parts) < 4:
                    logging.debug(f"Skipping malformed line: {line}")
                    continue
                interface = parts[0]
                ip_address_mask = parts[1]
                # Skip entries where IP Address/Mask is 'unassigned'
                if ip_address_mask.lower() == 'unassigned':
                    continue
                # Optionally, you can capture other columns like 'Physical', 'Protocol', 'VPN' if needed
                interface_entry = {
                    "Interface": interface,
                    "IP Address/Mask": ip_address_mask
                }
                interfaces.append(interface_entry)
        logging.info(f"Parsed {len(interfaces)} interface entries.")
        return interfaces

    def build_topology(self) -> List[Dict[str, Any]]:
        """
        Builds the network topology by identifying links between nodes based on shared subnets.
        """
        subnet_map = {}
        for host_port, interfaces in self.telnet_interfaces.items():
            for interface in interfaces:
                ip_mask = interface["IP Address/Mask"]
                try:
                    ip_net = ipaddress.ip_network(ip_mask, strict=False)
                    subnet = str(ip_net.network_address) + '/' + str(ip_net.prefixlen)
                    if subnet not in subnet_map:
                        subnet_map[subnet] = []
                    subnet_map[subnet].append({
                        "host_port": host_port,
                        "interface": interface["Interface"],
                        "sysname": self.telnet_sysnames.get(host_port, "Unknown")
                    })
                except ValueError as ve:
                    logging.error(f"Invalid IP address/mask '{ip_mask}' on {host_port}: {ve}")

        links = []
        for subnet, entries in subnet_map.items():
            # Find links where exactly two interfaces are in the subnet from different nodes
            if len(entries) == 2:
                entry1, entry2 = entries
                if entry1["host_port"] != entry2["host_port"]:
                    link = {
                        "node1": entry1["sysname"],
                        "interface1": entry1["interface"],
                        "node2": entry2["sysname"],
                        "interface2": entry2["interface"],
                        "subnet": subnet
                    }
                    links.append(link)
            else:
                if len(entries) > 2:
                    logging.warning(f"Subnet {subnet} has more than two interfaces: {len(entries)} entries.")
                elif len(entries) ==1:
                    logging.warning(f"Subnet {subnet} has only one interface.")
        logging.info(f"Built topology with {len(links)} links.")
        return links

    def get_configuration_via_telnet(self, tn: telnetlib.Telnet, image_type: str) -> Optional[str]:
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
                    # Parse the routing table and store filtered entries
                    filtered_routing = self.parse_routing_table(output)
                    if filtered_routing:
                        self.telnet_filtered_routing_tables[key] = filtered_routing
                    else:
                        logging.info(f"[{tn.host}:{tn.port}] No routing entries after filtering.")
                    # Parse the interface brief and store interface info
                    interfaces = self.parse_interface_brief(output)
                    if interfaces:
                        self.telnet_interfaces[key] = interfaces
                    else:
                        logging.info(f"[{tn.host}:{tn.port}] No interface entries after filtering.")
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
        Collects the results and builds the network topology.
        """
        links = self.build_topology()
        return {
            "telnet_devices": {
                host_port: {
                    "sysname": self.telnet_sysnames.get(host_port, "Unknown"),
                    "filtered_routing_table": self.telnet_filtered_routing_tables.get(host_port, []),
                    "interfaces": self.telnet_interfaces.get(host_port, [])
                }
                for host_port in self.telnet_sysnames
            },
            "links": links
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

def write_output(output_path: str, data: Dict[str, Any]):
    try:
        with open(output_path, 'w') as f:
            json.dump(data, f, indent=4)
        logging.info(f"Filtered routing results with sysnames and interfaces written to {output_path}")
    except IOError as e:
        logging.error(f"Error writing to output file: {e}")
        sys.exit(1)

def write_lab_output(lab_id: int, data: Dict[str, Any]):
    """
    Writes the data to /opt/unetlab/labs_history/{labId}.json.
    Creates the directory if it does not exist.
    Overwrites the file if it already exists.
    """
    directory = "/opt/unetlab/labs_history"
    os.makedirs(directory, exist_ok=True)
    lab_output_path = os.path.join(directory, f"{lab_id}.json")
    try:
        with open(lab_output_path, 'w') as f:
            json.dump(data, f, indent=4)
        logging.info(f"Lab output written to {lab_output_path}")
    except IOError as e:
        logging.error(f"Error writing to lab output file: {e}")
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

    logging.info("Collected filtered routing configurations with sysnames and interfaces:")
    logging.info(json.dumps(mapping, indent=4))
    write_output(output_path, mapping)

    # Extract labId from telnet_info
    lab_id = telnet_info.get("labId")
    if lab_id is not None:
        write_lab_output(lab_id, mapping)
    else:
        logging.warning("labId not found in param.json. Skipping writing to labs_history.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process and filter router configurations from param.json.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json, use {t} for latest folder number.")
    parser.add_argument("-o", "--output", required=True, help="Output path for filtered routing information with sysnames and interfaces, use {t} for latest folder number.")
    args = parser.parse_args()
    main(args.input, args.output)
