import xml.etree.ElementTree as ET
import json
import telnetlib
import os
import argparse


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


class RouterTelnetManager:
    def __init__(self, telnet_info):
        self.telnet_info = telnet_info
        self.sysnames = {}
        self.neighbor_data = {}

    def get_sysname(self, host, port):
        """通过 Telnet 获取节点的 sysname"""
        try:
            tn = telnetlib.Telnet(host, port, timeout=10)
            tn.write(b'\n')
            output = tn.read_until(b'>', timeout=5)
            lines = output.decode('ascii').splitlines()
            sysname = None
            for line in lines:
                if line.startswith('<') and line.endswith('>'):
                    sysname = line.strip('<> ')
                    break
            tn.close()
            return sysname if sysname else None
        except Exception as e:
            print(f"Error connecting to {host}:{port} - {e}")
            return None

    def get_neighbors(self, host, port, protocol):
        """通过 Telnet 获取指定协议的邻居列表"""
        commands = {
            'isis': b'display isis peer\n',
            'bgp': b'display bgp peer\n',
            'mpls_ldp': b'display mpls ldp peer\n'
        }

        if protocol not in commands:
            raise ValueError(f"Unsupported protocol: {protocol}")

        try:
            tn = telnetlib.Telnet(host, port, timeout=10)
            tn.write(b'\n')
            tn.read_until(b'>', timeout=5)

            tn.write(commands[protocol])
            output = tn.read_until(b'>', timeout=10)

            tn.close()
            return output.decode('ascii')
        except Exception as e:
            print(f"Error retrieving {protocol} neighbors from {host}:{port} - {e}")
            return None

    def parse_neighbors(self, output, protocol):
        """解析不同协议的邻居列表"""
        neighbors = []
        if protocol == 'isis':
            for line in output.splitlines():
                if 'adjacency' in line:  # Example: '192.168.1.2 adjacency'
                    neighbors.append(line.split()[0])
        elif protocol == 'bgp':
            for line in output.splitlines():
                if line.strip().startswith('Neighbor'):
                    continue
                elif line.strip():
                    neighbor_info = line.split()
                    if len(neighbor_info) >= 1:
                        neighbors.append(neighbor_info[0])  # Example: Neighbor IP
        elif protocol == 'mpls_ldp':
            for line in output.splitlines():
                if 'Peer LDP Identifier' in line:
                    neighbors.append(line.split()[-1])  # Example: 'Peer LDP Identifier: 10.10.10.10:0'
        return neighbors

    def connect_and_get_sysnames(self):
        """Connect to each router via Telnet and retrieve its sysname."""
        for node in self.telnet_info["node"]:
            host = node["hostip"]
            port = node["port"]
            sysname = self.get_sysname(host, port)
            if sysname:
                self.sysnames[host + ':' + str(port)] = sysname
                print(f"Connected to {host}:{port} - Sysname: {sysname}")
            else:
                print(f"Failed to retrieve sysname for {host}:{port}")

    def collect_neighbors(self, protocols=('isis', 'bgp', 'mpls_ldp')):
        """为每个 Telnet 连接收集指定协议的邻居信息"""
        for node in self.telnet_info["node"]:
            host = node["hostip"]
            port = node["port"]
            node_neighbors = {}

            for protocol in protocols:
                output = self.get_neighbors(host, port, protocol)
                if output:
                    neighbors = self.parse_neighbors(output, protocol)
                    node_neighbors[protocol] = neighbors

            self.neighbor_data[host + ':' + str(port)] = node_neighbors

            print(f"Collected neighbors for {host}:{port}: {node_neighbors}")


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
                    mapping[node_id] = {
                        'node_name': node_name,
                        'host_port': host_port,
                        'sysname': sysname,
                        'neighbors': self.telnet_manager.neighbor_data.get(host_port, {})
                    }

        print("Mapping between UNL topology, Telnet sysnames, and neighbors:")
        print(mapping)
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
    telnet_manager.connect_and_get_sysnames()
    telnet_manager.collect_neighbors(protocols=('isis', 'bgp', 'mpls_ldp'))

    mapper = TopologyMapper(unl_parser, telnet_manager)
    mapping = mapper.map_topology()

    with open(output_path, 'w') as f:
        f.write(json.dumps(mapping, indent=4))
    print(f"Mapping results with neighbors written to {output_path}")


if __name__ == "__main__":
    # Set up argument parsing
    parser = argparse.ArgumentParser(description="Process network topology from UNL and param.json.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json, use {t} for latest folder number.")
    parser.add_argument("-o", "--output", required=True, help="Output path for process information, use {t} for latest folder number.")
    args = parser.parse_args()

    main(args.input, args.output)
