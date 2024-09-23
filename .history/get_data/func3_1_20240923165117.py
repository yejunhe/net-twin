import xml.etree.ElementTree as ET
import json
import telnetlib
import os
import argparse
import time


class UNLParser:
    def __init__(self, unl_file):
        self.unl_file = unl_file
        self.nodes = {}
        self.networks = {}

    def parse(self):
        """Parse the .unl file to extract node and network information."""
        tree = ET.parse(self.unl_file)
        root = tree.getroot()

        # Extract node information
        for node in root.findall(".//node"):
            node_id = node.get('id')
            node_name = node.get('name')
            interfaces = []

            for interface in node.findall("interface"):
                interfaces.append({
                    'id': interface.get('id'),
                    'name': interface.get('name'),
                    'network_id': interface.get('network_id')
                })

            self.nodes[node_id] = {
                'name': node_name,
                'interfaces': interfaces
            }

        # Extract network information
        for network in root.findall(".//network"):
            network_id = network.get('id')
            network_name = network.get('name')
            self.networks[network_id] = network_name

        print("Parsed UNL File:")
        print("Nodes:", self.nodes)
        print("Networks:", self.networks)


class RouterTelnetConnection:
    def __init__(self, host, port, timeout=10):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.tn = None

    def connect(self):
        """Establish a Telnet connection and perform initial setup."""
        try:
            self.tn = telnetlib.Telnet(self.host, self.port, timeout=self.timeout)
            self._read_until_prompt()
            self.tn.write(b'\n')  # Ensure we're at the prompt
            self._read_until_prompt()
            print(f"Connected to {self.host}:{self.port}")
        except Exception as e:
            print(f"Error connecting to {self.host}:{self.port} - {e}")
            self.tn = None

    def send_command(self, command, wait_time=1):
        """Send a command to the Telnet session and return the output."""
        if not self.tn:
            print(f"No active Telnet connection to {self.host}:{self.port}")
            return None
        try:
            self.tn.write(command.encode('ascii') + b'\n')
            time.sleep(wait_time)  # Wait for the command to execute
            output = self._read_until_prompt()
            return output
        except Exception as e:
            print(f"Error sending command to {self.host}:{self.port} - {e}")
            return None

    def _read_until_prompt(self):
        """Read data until the prompt character '>' is found."""
        try:
            output = self.tn.read_until(b'>', timeout=self.timeout)
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
    def __init__(self, telnet_info):
        self.telnet_info = telnet_info
        self.sysnames = {}
        self.neighbor_data = {}
        self.routing_tables = {}

    def connect_router(self, host, port):
        """Establish a Telnet connection to the router."""
        connection = RouterTelnetConnection(host, port)
        connection.connect()
        if connection.tn:
            return connection
        else:
            return None

    def get_sysname(self, connection):
        """Retrieve the sysname from the router."""
        try:
            output = connection.send_command('\n')
            lines = output.splitlines()
            sysname = None
            for line in lines:
                if line.startswith('<') and line.endswith('>'):
                    sysname = line.strip('<> ')
                    break
            return sysname if sysname else None
        except Exception as e:
            print(f"Error retrieving sysname - {e}")
            return None

    def send_scr_command(self, connection):
        """Send the 'scr 0 t' command to the router."""
        try:
            output = connection.send_command('scr 0 t', wait_time=2)
            print(f"Sent 'scr 0 t' to {connection.host}:{connection.port}")
            return output
        except Exception as e:
            print(f"Error sending 'scr 0 t' - {e}")
            return None

    def get_neighbors(self, connection, protocol):
        """Retrieve neighbors for a specific protocol."""
        commands = {
            'isis': 'display isis peer',
            'bgp': 'display bgp peer',
            'mpls_ldp': 'display mpls ldp peer'
        }

        if protocol not in commands:
            raise ValueError(f"Unsupported protocol: {protocol}")

        output = connection.send_command(commands[protocol], wait_time=3)
        return output

    def parse_neighbors(self, output, protocol):
        """Parse the neighbors output based on the protocol."""
        neighbors = []
        if protocol == 'isis':
            in_peer_section = False
            for line in output.splitlines():
                line = line.strip()
                if "System Id" in line:
                    in_peer_section = True
                    continue
                if in_peer_section and len(line) > 0 and not line.startswith("---") and not line.startswith("<"):
                    columns = line.split()
                    if len(columns) > 0:
                        neighbors.append(columns[0])  # Extract System Id
                if "Total" in line:
                    break
        elif protocol == 'bgp':
            in_bgp_section = False
            for line in output.splitlines():
                if "Peer" in line and "AS" in line and "State" in line:
                    in_bgp_section = True
                    continue
                if in_bgp_section and line.strip() and not line.startswith("BGP local"):
                    peer_info = line.split()
                    if len(peer_info) >= 1:
                        neighbors.append(peer_info[0])  # Example: Neighbor IP
        elif protocol == 'mpls_ldp':
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
        """Retrieve the routing table from the router."""
        output = connection.send_command('display ip routing-table', wait_time=5)
        return output

    def parse_routing_table(self, output):
        """Parse the routing table output."""
        routing_entries = []
        lines = output.splitlines()
        parsing = False
        for line in lines:
            if line.startswith("Destination/Mask"):
                parsing = True
                continue
            if parsing:
                if line.strip() == "" or line.startswith("-"):
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

    def collect_neighbors_and_routing(self, protocols=('isis', 'bgp', 'mpls_ldp')):
        """Collect neighbors and routing table information for each router."""
        for node in self.telnet_info["node"]:
            host = node["hostip"]
            port = node["port"]
            connection = self.connect_router(host, port)
            if not connection:
                print(f"Skipping {host}:{port} due to connection issues.")
                continue

            # Retrieve sysname
            sysname = self.get_sysname(connection)
            if sysname:
                self.sysnames[f"{host}:{port}"] = sysname
                print(f"Retrieved sysname for {host}:{port}: {sysname}")
            else:
                print(f"Failed to retrieve sysname for {host}:{port}")

            # Send 'scr 0 t' command
            self.send_scr_command(connection)

            # Collect neighbors
            node_neighbors = {}
            for protocol in protocols:
                output = self.get_neighbors(connection, protocol)
                if output:
                    neighbors = self.parse_neighbors(output, protocol)
                    node_neighbors[protocol] = neighbors
            self.neighbor_data[f"{host}:{port}"] = node_neighbors
            print(f"Collected neighbors for {host}:{port}: {node_neighbors}")

            # Collect routing table
            routing_output = self.get_routing_table(connection)
            if routing_output:
                routing_entries = self.parse_routing_table(routing_output)
                self.routing_tables[f"{host}:{port}"] = routing_entries
                print(f"Collected routing table for {host}:{port}")
            else:
                print(f"Failed to retrieve routing table for {host}:{port}")

            # Close the connection
            connection.close()


class TopologyMapper:
    def __init__(self, unl_parser, telnet_manager):
        self.unl_parser = unl_parser
        self.telnet_manager = telnet_manager

    def map_topology(self):
        """Map the sysnames and neighbors retrieved from Telnet to the nodes in the UNL topology."""
        mapping = {}
        for node_id, node_info in self.unl_parser.nodes.items():
            node_name = node_info['name']
            for host_port, sysname in self.telnet_manager.sysnames.items():
                if node_name == sysname:
                    # Get routing table data
                    routing_entries = self.telnet_manager.routing_tables.get(host_port, [])
                    # Organize routing table by protocol
                    routing_by_proto = {}
                    for entry in routing_entries:
                        proto = entry['Proto']
                        if proto not in routing_by_proto:
                            routing_by_proto[proto] = []
                        routing_by_proto[proto].append({
                            'Destination/Mask': entry['Destination/Mask'],
                            'Cost': entry['Cost'],
                            'NextHop': entry['NextHop'],
                            'Interface': entry['Interface']
                        })

                    mapping[node_id] = {
                        'node_name': node_name,
                        'host_port': host_port,
                        'sysname': sysname,
                        'neighbors': self.telnet_manager.neighbor_data.get(host_port, {}),
                        'routing_table': routing_by_proto  # Routing table data
                    }

        print("Mapping between UNL topology, Telnet sysnames, neighbors, and routing tables:")
        print(json.dumps(mapping, indent=4))
        return mapping


def find_latest_folder(base_path):
    """Find the latest folder by number under the given base path."""
    all_folders = [f for f in os.listdir(base_path) if f.isdigit()]
    if not all_folders:
        raise ValueError("No numbered folders found in the base path.")
    latest_folder = max(all_folders, key=int)
    return latest_folder


def main(input_path, output_path):
    base_path = "/uploadPath/reasoning"
    if "{t}" in input_path or "{t}" in output_path:
        latest_folder = find_latest_folder(base_path)
        input_path = input_path.replace("{t}", latest_folder)
        output_path = output_path.replace("{t}", latest_folder)

    with open(input_path, 'r') as f:
        telnet_info = json.load(f)

    lab_id = telnet_info["labId"]
    unl_file_path = f"/opt/unetlab/labs/{lab_id}.unl"

    unl_parser = UNLParser(unl_file_path)
    unl_parser.parse()

    telnet_manager = RouterTelnetManager(telnet_info)
    telnet_manager.collect_neighbors_and_routing(protocols=('isis', 'bgp', 'mpls_ldp'))

    mapper = TopologyMapper(unl_parser, telnet_manager)
    mapping = mapper.map_topology()

    with open(output_path, 'w') as f:
        f.write(json.dumps(mapping, indent=4))
    print(f"Mapping results with neighbors and routing tables written to {output_path}")


if __name__ == "__main__":
    # Set up argument parsing
    parser = argparse.ArgumentParser(description="Process network topology from UNL and param.json.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json, use {t} for latest folder number.")
    parser.add_argument("-o", "--output", required=True, help="Output path for process information, use {t} for latest folder number.")
    args = parser.parse_args()

    main(args.input, args.output)
