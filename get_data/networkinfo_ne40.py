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

    def get_sysname(self, host, port):
        """通过 Telnet 获取节点的 sysname"""
        try:
            # 增加超时时间，建立 Telnet 连接
            tn = telnetlib.Telnet(host, port, timeout=10)

            # 发送一个空命令（如回车字符 '\n'），以确保接收到提示符
            tn.write(b'\n')

            # 设置匹配可能的提示符，增加超时时间来等待返回结果
            output = tn.read_until(b'>', timeout=5)  # 等待提示符 '>'，超时时间 5 秒

            # 处理输出，假设 sysname 在提示符之间
            lines = output.decode('ascii').splitlines()
            sysname = None

            # 遍历每一行，寻找可能的 sysname
            for line in lines:
                if line.startswith('<') and line.endswith('>'):
                    sysname = line.strip('<> ')
                    break

            tn.close()
            if sysname:
                return sysname
            else:
                print(f"Could not determine sysname from output: {output}")
                return None

        except Exception as e:
            print(f"Error connecting to {host}:{port} - {e}")
            return None

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


class TopologyMapper:
    def __init__(self, unl_parser, telnet_manager):
        self.unl_parser = unl_parser
        self.telnet_manager = telnet_manager

    def map_topology(self):
        """Map the sysnames retrieved from Telnet to the nodes in the UNL topology."""
        mapping = {}
        for node_id, node_info in self.unl_parser.nodes.items():
            node_name = node_info['name']
            for host_port, sysname in self.telnet_manager.sysnames.items():
                if node_name == sysname:
                    mapping[node_id] = {
                        'node_name': node_name,
                        'host_port': host_port,
                        'sysname': sysname
                    }

        print("Mapping between UNL topology and Telnet sysnames:")
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
    # Resolve the latest folder number if {t} is used
    base_path = "/uploadPath/reasoning"
    if "{t}" in input_path or "{t}" in output_path:
        latest_folder = find_latest_folder(base_path)
        input_path = input_path.replace("{t}", latest_folder)
        output_path = output_path.replace("{t}", latest_folder)

    # Load param.json
    with open(input_path, 'r') as f:
        telnet_info = json.load(f)

    # Extract experiment ID and construct UNL file path
    lab_id = telnet_info["labId"]
    unl_file_path = f"/opt/unetlab/labs/{lab_id}.unl"

    # Parse the UNL file
    unl_parser = UNLParser(unl_file_path)
    unl_parser.parse()

    # Manage Telnet connections
    telnet_manager = RouterTelnetManager(telnet_info)
    telnet_manager.connect_and_get_sysnames()

    # Map topology
    mapper = TopologyMapper(unl_parser, telnet_manager)
    mapping = mapper.map_topology()

    # Output results to file
    with open(output_path, 'w') as f:
        f.write(json.dumps(mapping, indent=4))
    print(f"Mapping results written to {output_path}")


if __name__ == "__main__":
    # Set up argument parsing
    parser = argparse.ArgumentParser(description="Process network topology from UNL and param.json.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json, use {t} for latest folder number.")
    parser.add_argument("-o", "--output", required=True, help="Output path for process information, use {t} for latest folder number.")
    args = parser.parse_args()

    main(args.input, args.output)
