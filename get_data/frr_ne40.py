import xml.etree.ElementTree as ET
import json
import telnetlib
import os
import argparse
import subprocess
import re

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

class RouterManager:
    def __init__(self, telnet_info):
        self.telnet_info = telnet_info
        self.sysnames = {}
        self.configurations = {}

    def get_sysname_via_telnet(self, host, port):
        """Retrieve sysname via Telnet."""
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
            print(f"Telnet Error: {e}")
            return None

    def get_configuration_via_telnet(self, host, port):
        """Retrieve configuration via Telnet."""
        try:
            tn = telnetlib.Telnet(host, port, timeout=10)
            tn.read_until(b'>', timeout=5)
            tn.write(b'display current-configuration\n')
            config_output = tn.read_until(b'>', timeout=10)
            tn.close()
            return self.clean_configuration(config_output.decode('ascii'))
        except Exception as e:
            print(f"Telnet Error: {e}")
            return None

    def get_configuration_via_ssh(self, docker_id):
        """Retrieve configuration via SSH/Docker."""
        try:
            docker_ps_output = subprocess.check_output(['docker', 'ps']).decode('utf-8')
            container_id = None
            for line in docker_ps_output.splitlines()[1:]:
                if docker_id in line:
                    container_id = line.split()[0]
                    break

            if not container_id:
                print(f"Container not found for Docker ID: {docker_id}")
                return None

            cmd = f"docker exec {container_id} cat /etc/frr/frr.conf"
            config_output = subprocess.check_output(cmd, shell=True).decode('utf-8')
            return self.clean_configuration(config_output)
        except Exception as e:
            print(f"SSH/Docker Error: {e}")
            return None

    def clean_configuration(self, config):
        """Clean and format the configuration."""
        # Remove any unneeded sections and clean the output
        cleaned_config = []
        in_interface_section = False
        
        for line in config.splitlines():
            line = line.strip()
            if line.startswith("interface"):
                in_interface_section = True
            elif in_interface_section and line == "#":
                in_interface_section = False
            if in_interface_section or line.startswith(("sysname", "ip address", "mpls", "aaa", "isis")):
                cleaned_config.append(line)
        
        return "\n".join(cleaned_config)

    def connect_and_get_sysnames_and_configs(self):
        """Connect to each router and retrieve sysnames and configurations."""
        for node in self.telnet_info["node"]:
            host = node.get("hostip")
            port = node.get("port")
            docker_id = node.get("dockerid")
            console_type = node.get("console_type")

            if console_type == "telnet":
                # Retrieve NE40 sysname and configuration
                sysname = self.get_sysname_via_telnet(host, port)
                config = self.get_configuration_via_telnet(host, port)
                if sysname:
                    self.sysnames[f"{host}:{port}"] = sysname
                if config:
                    self.configurations[f"{host}:{port}"] = config
            elif console_type == "ssh" and docker_id:
                # Retrieve FRR configuration
                config = self.get_configuration_via_ssh(docker_id)
                if config:
                    self.configurations[docker_id] = config

class TopologyMapper:
    def __init__(self, unl_parser, router_manager):
        self.unl_parser = unl_parser
        self.router_manager = router_manager

    def map_topology(self):
        """Map the sysnames and configurations retrieved to the UNL topology."""
        mapping = {}
        for node_id, node_info in self.unl_parser.nodes.items():
            node_name = node_info['name']
            for host_port, sysname in self.router_manager.sysnames.items():
                if node_name == sysname:
                    mapping[node_id] = {
                        'node_name': node_name,
                        'host_port': host_port,
                        'sysname': sysname,
                        'configuration': self.router_manager.configurations.get(host_port, 'No config')
                    }

        for docker_id, config in self.router_manager.configurations.items():
            if docker_id not in mapping.values():
                mapping[docker_id] = {
                    'docker_id': docker_id,
                    'configuration': config
                }

        print("Mapping between UNL topology and router configurations:")
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

    # Manage Telnet/SSH connections and retrieve sysnames and configurations
    router_manager = RouterManager(telnet_info)
    router_manager.connect_and_get_sysnames_and_configs()

    # Map topology
    mapper = TopologyMapper(unl_parser, router_manager)
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
