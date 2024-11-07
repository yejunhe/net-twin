import json
import telnetlib
import os
import argparse
import time
from concurrent.futures import ThreadPoolExecutor, as_completed


class RouterTelnetConnection:
    def __init__(self, host, port, timeout=10):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.tn = None
        self.prompt = b'>'

    def connect(self):
        """Establish a Telnet connection and handle prompt detection."""
        try:
            self.tn = telnetlib.Telnet(self.host, self.port, timeout=self.timeout)
            while True:
                initial_output = self._read_until_prompt()
                if not initial_output:
                    print(f"No output received from {self.host}:{self.port}")
                    break

                # Detect prompt brackets
                if '<' in initial_output and '>' in initial_output:
                    # Extract the prompt between '<' and '>'
                    start = initial_output.find('<') + 1
                    end = initial_output.find('>', start)
                    if start < end:
                        prompt_str = initial_output[start:end].strip()
                        self.prompt = prompt_str.encode('ascii') + b'> '
                        print(f"Detected valid prompt '{self.prompt.decode('ascii')}' on {self.host}:{self.port}")
                        break
                elif '[' in initial_output and ']' in initial_output:
                    # Send 'quit' command and retry
                    print(f"Detected undesired prompt '[' ']' on {self.host}:{self.port}. Sending 'quit' command.")
                    self.send_command('quit', wait_time=1)
                else:
                    # If prompt format is unrecognized, break to avoid infinite loop
                    print(f"Unrecognized prompt format on {self.host}:{self.port}.")
                    break

            if self.tn:
                self.tn.write(b'\n')  # Ensure at prompt
                self._read_until_prompt()
                print(f"Connected to {self.host}:{self.port} with prompt '{self.prompt.decode('ascii')}'")
        except Exception as e:
            print(f"Error connecting to {self.host}:{self.port} - {e}")
            self.tn = None

    def send_command(self, command, wait_time=1):
        """Send a command to the Telnet session and return the output."""
        if not self.tn:
            print(f"No active Telnet connection to {self.host}:{self.port}")
            return None
        try:
            print(f"[{self.host}:{self.port}] Sending command: {command}")
            self.tn.write(command.encode('ascii') + b'\n')
            time.sleep(wait_time)  # Wait for command execution
            output = self._read_until_prompt()
            print(f"[{self.host}:{self.port}] Received output for '{command}':\n{output}")
            return output
        except Exception as e:
            print(f"Error sending command to {self.host}:{self.port} - {e}")
            return None

    def _read_until_prompt(self):
        """Read data until the prompt is found."""
        try:
            output = self.tn.read_until(self.prompt, timeout=self.timeout)
            return output.decode('ascii')
        except Exception as e:
            print(f"Error reading from {self.host}:{self.port} - {e}")
            return ""

    def close(self):
        """Close the Telnet connection."""
        if self.tn:
            self.tn.close()
            print(f"Closed connection to {self.host}:{self.port}")
            self.tn = None


class RouterTelnetManager:
    def __init__(self, telnet_info, max_workers=20):
        """
        Initialize the Telnet Manager.

        :param telnet_info: Dictionary containing Telnet connection info.
        :param max_workers: Maximum number of concurrent threads.
        """
        self.telnet_info = telnet_info
        self.sysnames = {}
        self.neighbor_data = {}
        self.routing_tables = {}
        self.bgp_summaries = {}  # For storing BGP Summary information
        self.max_workers = max_workers

    def connect_and_collect(self, node, protocols=('isis', 'bgp', 'mpls_ldp')):
        """
        Connect to a single router and collect sysname, neighbors, routing table, and BGP Summary information.

        :param node: Dictionary containing 'hostip' and 'port'.
        :param protocols: Tuple of protocols to collect neighbor information for.
        """
        host = node.get("hostip")
        port = node.get("port")
        if not host or not port:
            print(f"Invalid node configuration: {node}")
            return

        connection = RouterTelnetConnection(host, port)
        connection.connect()
        if not connection.tn:
            print(f"Skipping {host}:{port} due to connection issues.")
            return

        try:
            # Get sysname with enhanced prompt handling
            sysname = self.get_sysname(connection)
            if sysname:
                self.sysnames[f"{host}:{port}"] = sysname
                print(f"[{host}:{port}] Retrieved sysname: {sysname}")
            else:
                print(f"[{host}:{port}] Failed to retrieve sysname.")

            # Send 'scr 0 t' command
            self.send_scr_command(connection)

            # Collect neighbor information
            node_neighbors = {}
            for protocol in protocols:
                output = self.get_neighbors(connection, protocol)
                if output:
                    neighbors = self.parse_neighbors(output, protocol)
                    node_neighbors[protocol] = neighbors
                    print(f"[{host}:{port}] Parsed neighbors for '{protocol}': {neighbors}")
                else:
                    node_neighbors[protocol] = []
            self.neighbor_data[f"{host}:{port}"] = node_neighbors
            print(f"[{host}:{port}] Collected neighbors: {node_neighbors}")

            # Collect routing table
            routing_output = self.get_routing_table(connection)
            if routing_output:
                routing_entries = self.parse_routing_table(routing_output)
                self.routing_tables[f"{host}:{port}"] = routing_entries
                print(f"[{host}:{port}] Collected routing table with {len(routing_entries)} entries.")
            else:
                print(f"[{host}:{port}] Failed to retrieve routing table.")

            # Collect BGP Summary information
            bgp_summary_output = self.get_bgp_summary(connection)
            if bgp_summary_output:
                bgp_summary = self.parse_bgp_summary(bgp_summary_output)
                self.bgp_summaries[f"{host}:{port}"] = bgp_summary
                print(f"[{host}:{port}] Collected BGP Summary.")
            else:
                print(f"[{host}:{port}] Failed to retrieve BGP Summary.")

        except Exception as e:
            print(f"Error processing {host}:{port} - {e}")
        finally:
            # Ensure the connection is closed
            connection.close()

    def collect_neighbors_and_routing(self, protocols=('isis', 'bgp', 'mpls_ldp')):
        """
        Collect neighbors, routing tables, and BGP Summary information in parallel for each router.

        :param protocols: Tuple of protocols to collect neighbor information for.
        """
        nodes = self.telnet_info.get("node", [])
        if not nodes:
            print("No nodes found in the Telnet information.")
            return

        print(f"Starting parallel collection for {len(nodes)} nodes with up to {self.max_workers} workers.")
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit all tasks to the executor
            future_to_node = {executor.submit(self.connect_and_collect, node, protocols): node for node in nodes}

            # Handle completed tasks
            for future in as_completed(future_to_node):
                node = future_to_node[future]
                host = node.get("hostip")
                port = node.get("port")
                try:
                    future.result()
                except Exception as e:
                    print(f"Exception occurred while processing {host}:{port} - {e}")

    def get_sysname(self, connection):
        """Retrieve sysname from the router."""
        try:
            while True:
                output = connection.send_command('', wait_time=1)
                lines = output.splitlines()
                prompt = lines[-1].strip()  # 假设最后一行是提示符
                if prompt.startswith('<') and prompt.endswith('>'):
                    sysname = prompt[1:-1]  # 去掉<和>
                    return sysname
                elif prompt.startswith('[') and prompt.endswith(']'):
                    sysname = re.sub('~', '', prompt[1:-1])  # 去掉~并提取内容
                    return sysname
                else:
                    # 输入'q'并继续检测
                    connection.send_command('q', wait_time=1)
        except Exception as e:
            print(f"Error retrieving sysname from {connection.host}:{connection.port} - {e}")
            return None


    def send_scr_command(self, connection):
        """Send 'scr 0 t' command to the router."""
        try:
            connection.send_command('scr 0 t', wait_time=2)
            print(f"[{connection.host}:{connection.port}] Sent 'scr 0 t' command.")
        except Exception as e:
            print(f"Error sending 'scr 0 t' to {connection.host}:{connection.port} - {e}")

    def get_neighbors(self, connection, protocol):
        """Get neighbors for a specified protocol."""
        commands = {
            'isis': 'display isis peer',
            'bgp': 'display bgp peer',
            'mpls_ldp': 'display mpls ldp peer'
        }

        command = commands.get(protocol)
        if not command:
            print(f"Unsupported protocol '{protocol}' for {connection.host}:{connection.port}.")
            return None

        output = connection.send_command(command, wait_time=3)
        return output

    def parse_neighbors(self, output, protocol):
        """Parse neighbor information based on protocol."""
        neighbors = []
        if protocol == 'isis':
            in_peer_section = False
            for line in output.splitlines():
                line = line.strip()
                if "System Id" in line:
                    in_peer_section = True
                    continue
                if in_peer_section and line and not line.startswith("---") and not line.startswith("<"):
                    columns = line.split()
                    if columns:
                        neighbors.append(columns[0])  # Only extract System Id
                if "Total" in line:
                    break
        elif protocol == 'bgp':
            # Parse BGP neighbor information
            in_bgp_section = False
            for line in output.splitlines():
                if "Peer" in line and "AS" in line and "State" in line:
                    in_bgp_section = True
                    continue
                if in_bgp_section and line.strip() and not line.startswith("BGP local"):
                    peer_info = line.split()
                    if peer_info:
                        neighbors.append(peer_info[0])  # Example: Neighbor IP
        elif protocol == 'mpls_ldp':
            # Parse MPLS LDP neighbor information
            in_ldp_section = False
            for line in output.splitlines():
                if "PeerID" in line and "TransportAddress" in line:
                    in_ldp_section = True
                    continue
                if in_ldp_section and line.strip() and ':' in line:
                    ldp_peer = line.split()[0]
                    neighbors.append(ldp_peer.split(':')[0])  # Example: '192.168.2.2:0'
        return neighbors

    def get_routing_table(self, connection):
        """Get the routing table."""
        output = connection.send_command('display ip routing-table', wait_time=5)
        return output

    def parse_routing_table(self, output):
        """Parse the routing table output."""
        routing_entries = []
        lines = output.splitlines()
        parsing = False
        for line in lines:
            line = line.strip()
            if line.startswith("Destination/Mask"):
                parsing = True
                continue
            if parsing:
                if not line or line.startswith("-"):
                    continue
                # Split by multiple spaces
                fields = line.split()
                if len(fields) < 7:
                    continue
                destination_mask = fields[0]
                proto = fields[1]
                pre = fields[2]
                cost = fields[3]
                flags = fields[4]
                next_hop = fields[5]
                interface = fields[6]
                routing_entries.append({
                    'Destination/Mask': destination_mask,
                    'Proto': proto,
                    'Pre': pre,
                    'Cost': cost,
                    'Flags': flags,
                    'NextHop': next_hop,
                    'Interface': interface
                })
        return routing_entries

    def get_bgp_summary(self, connection):
        """Get BGP Summary information."""
        output = connection.send_command('display bgp all summary', wait_time=3)
        return output

    def parse_bgp_summary(self, output):
        """Parse the BGP Summary output."""
        bgp_summary = {
            'router_id': None,
            'local_as_number': None,
            'address_family': None,
            'total_peers': 0,
            'peers_established': 0,
            'peers': []
        }
        lines = output.splitlines()
        parsing_peers = False

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Parse Router ID
            if line.startswith("BGP local router ID"):
                parts = line.split(":")
                if len(parts) == 2:
                    bgp_summary['router_id'] = parts[1].strip()
                continue

            # Parse Local AS number
            if line.startswith("Local AS number"):
                parts = line.split(":")
                if len(parts) == 2:
                    bgp_summary['local_as_number'] = parts[1].strip()
                continue

            # Parse Address Family
            if line.startswith("Address Family"):
                parts = line.split(":")
                if len(parts) == 2:
                    bgp_summary['address_family'] = parts[1].strip()
                continue

            # Parse total peers and established peers
            if line.startswith("Total number of peers"):
                # Example line:
                # Total number of peers : 1                 Peers in established state : 0
                parts = line.split()
                try:
                    # Extract total_peers
                    total_peers_index = parts.index("peers") + 2  # 'peers' is part of 'peers : <number>'
                    total_peers = int(parts[total_peers_index])
                    bgp_summary['total_peers'] = total_peers
                except (ValueError, IndexError):
                    pass

                try:
                    # Extract peers_established
                    established_index = parts.index("established") + 4  # 'established' is part of 'established state : <number>'
                    peers_established = int(parts[established_index])
                    bgp_summary['peers_established'] = peers_established
                except (ValueError, IndexError):
                    pass
                continue

            # Parse Peer table header to start parsing peer information
            if line.startswith("Peer") and "AS" in line and "State" in line:
                parsing_peers = True
                continue

            if parsing_peers:
                # Assume peer information is after the header until an empty line or non-data line
                if line.startswith("-") or line.startswith("Total"):
                    parsing_peers = False
                    continue
                fields = line.split()
                if len(fields) >= 7:
                    peer_ip = fields[0]
                    remote_as = fields[1]
                    msg_rcvd = fields[2]
                    msg_sent = fields[3]
                    out_q = fields[4]
                    up_down = fields[5]
                    state = fields[6]
                    # RtRcv and RtAdv may exist or be missing
                    rt_rcv = fields[7] if len(fields) > 7 else "0"
                    rt_adv = fields[8] if len(fields) > 8 else "0"

                    bgp_summary['peers'].append({
                        'Peer': peer_ip,
                        'RemoteAS': remote_as,
                        'MsgRcvd': msg_rcvd,
                        'MsgSent': msg_sent,
                        'OutQ': out_q,
                        'Up/Down': up_down,
                        'State': state,
                        'RtRcv': rt_rcv,
                        'RtAdv': rt_adv
                    })

        return bgp_summary


def find_latest_folder(base_path):
    """Find the folder with the highest numerical name in the given base path."""
    try:
        all_folders = [f for f in os.listdir(base_path) if f.isdigit()]
        if not all_folders:
            raise ValueError("No numbered folders found in the base path.")
        latest_folder = max(all_folders, key=int)
        print(f"Latest folder found: {latest_folder}")
        return latest_folder
    except Exception as e:
        print(f"Error finding latest folder in {base_path}: {e}")
        raise


def main(input_path, output_path):
    """
    Main function to execute the Telnet data collection.

    :param input_path: Path to the input JSON file containing Telnet info.
    :param output_path: Path to save the output JSON file with collected data.
    """
    base_path = "/uploadPath/reasoning"
    if "{t}" in input_path or "{t}" in output_path:
        try:
            latest_folder = find_latest_folder(base_path)
            input_path = input_path.replace("{t}", latest_folder)
            output_path = output_path.replace("{t}", latest_folder)
            print(f"Replaced '{{t}}' with '{latest_folder}' in paths.")
        except Exception as e:
            print(f"Failed to replace '{{t}}' in paths: {e}")
            return

    # Load Telnet information from input JSON
    try:
        with open(input_path, 'r') as f:
            telnet_info = json.load(f)
        print(f"Loaded Telnet information from {input_path}.")
    except Exception as e:
        print(f"Error loading input file {input_path}: {e}")
        return

    # Initialize Telnet Manager and collect data in parallel
    telnet_manager = RouterTelnetManager(telnet_info, max_workers=20)
    telnet_manager.collect_neighbors_and_routing(protocols=('isis', 'bgp', 'mpls_ldp'))

    # Prepare the mapping result without UNL file
    mapping = {
        "sysnames": telnet_manager.sysnames,
        "neighbors": telnet_manager.neighbor_data,
        "routing_tables": telnet_manager.routing_tables,
        "bgp_summaries": telnet_manager.bgp_summaries
    }

    # Save the collected data to the output JSON file
    try:
        with open(output_path, 'w') as f:
            json.dump(mapping, f, indent=4)
        print(f"Mapping results written to {output_path}")
    except Exception as e:
        print(f"Error writing output file {output_path}: {e}")


if __name__ == "__main__":
    # Setup command-line argument parsing
    parser = argparse.ArgumentParser(description="Process network data via Telnet connections.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json, use {t} for latest folder number.")
    parser.add_argument("-o", "--output", required=True, help="Output path for process information, use {t} for latest folder number.")
    args = parser.parse_args()

    main(args.input, args.output)
