import xml.etree.ElementTree as ET
import json
import telnetlib
import os
import argparse
import subprocess
import sys


class UNLParser:
    def __init__(self, unl_file):
        self.unl_file = unl_file
        self.nodes = {}
        self.networks = {}

    def parse(self):
        """Parse the .unl file to extract node and network information."""
        try:
            tree = ET.parse(self.unl_file)
            root = tree.getroot()
        except ET.ParseError as e:
            print(f"Error parsing UNL file: {e}")
            sys.exit(1)
        except FileNotFoundError:
            print(f"UNL file not found at path: {self.unl_file}")
            sys.exit(1)

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
        print("Nodes:", json.dumps(self.nodes, indent=4))
        print("Networks:", json.dumps(self.networks, indent=4))


class RouterManager:
    def __init__(self, telnet_info):
        self.telnet_info = telnet_info
        self.sysnames = {}
        self.configurations = {}

    def get_sysname_via_telnet(self, host, port):
        """通过 Telnet 获取 NE40 节点的 sysname"""
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
        """通过 Telnet 获取 NE40 路由器的配置信息"""
        try:
            tn = telnetlib.Telnet(host, port, timeout=10)
            tn.read_until(b'>', timeout=5)
            tn.write(b'display current-configuration\n')
            config_output = tn.read_until(b'>', timeout=10)
            tn.close()
            return config_output.decode('ascii')
        except Exception as e:
            print(f"Telnet Error: {e}")
            return None

    def connect_via_docker(self, docker_id):
        """通过 Docker 进入 FRR 路由器的容器"""
        try:
            # 获取容器名称对应的 CONTAINER ID
            docker_ps_output = subprocess.check_output(['docker', 'ps'], stderr=subprocess.STDOUT).decode('utf-8')
            container_id = None
            for line in docker_ps_output.splitlines()[1:]:
                if docker_id in line:
                    container_id = line.split()[0]
                    break

            if not container_id:
                print(f"Container not found for Docker ID: {docker_id}")
                return False

            # 使用 subprocess 调用 docker exec -it 进入容器
            print(f"Entering Docker container: {container_id}")
            subprocess.call(['docker', 'exec', '-it', container_id, '/bin/bash'])
            return True
        except subprocess.CalledProcessError as e:
            print(f"Docker Connection Error: {e.output.decode('utf-8')}")
            return False
        except Exception as e:
            print(f"Docker Connection Error: {e}")
            return False

    def connect_and_get_sysnames_and_configs(self):
        """Connect to each router and retrieve sysnames and configurations based on image_type."""
        for node in self.telnet_info.get("node", []):
            image_type = node.get("image_type", "").lower()

            if "frrouting" in image_type:
                # 通过 Docker 登录容器，并进入容器
                docker_id = node.get("dockerid")
                if docker_id:
                    success = self.connect_via_docker(docker_id)
                    if success:
                        self.sysnames[docker_id] = "Connected via Docker"
                else:
                    print(f"No Docker ID provided for node with image_type 'frrouting'.")
            elif "h3c" in image_type or "huaweine40" in image_type:
                # 通过 Telnet 登录并获取 sysname 和配置
                host = node.get("hostip")
                port = node.get("port")

                if not host or not port:
                    print(f"Host IP or port missing for node with image_type '{image_type}'.")
                    continue

                sysname = self.get_sysname_via_telnet(host, port)
                config = self.get_configuration_via_telnet(host, port)

                if sysname:
                    self.sysnames[f"{host}:{port}"] = sysname
                if config:
                    self.configurations[f"{host}:{port}"] = config
            else:
                print(f"Unknown image_type '{image_type}' for node. Skipping.")


class TopologyMapper:
    def __init__(self, unl_parser, router_manager):
        self.unl_parser = unl_parser
        self.router_manager = router_manager

    def map_topology(self):
        """Map the sysnames and configurations retrieved to the UNL topology."""
        mapping = {}
        # Map Telnet-based routers
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

        # Map Docker-based routers
        for docker_id, status in self.router_manager.sysnames.items():
            if status == "Connected via Docker":
                mapping[docker_id] = {
                    'docker_id': docker_id,
                    'status': status
                }

        print("Mapping between UNL topology and router configurations:")
        print(json.dumps(mapping, indent=4))
        return mapping


def find_latest_folder(base_path):
    """Find the latest folder by number under the given base path."""
    try:
        all_folders = [f for f in os.listdir(base_path) if f.isdigit()]
        if not all_folders:
            raise ValueError("No numbered folders found in the base path.")
        latest_folder = max(all_folders, key=int)
        return latest_folder
    except FileNotFoundError:
        print(f"Base path not found: {base_path}")
        sys.exit(1)
    except ValueError as ve:
        print(ve)
        sys.exit(1)


def main(input_path, output_path):
    # Resolve the latest folder number if {t} is used
    base_path = "/uploadPath/reasoning"
    if "{t}" in input_path or "{t}" in output_path:
        latest_folder = find_latest_folder(base_path)
        input_path = input_path.replace("{t}", latest_folder)
        output_path = output_path.replace("{t}", latest_folder)

    # Load param.json
    try:
        with open(input_path, 'r') as f:
            telnet_info = json.load(f)
    except FileNotFoundError:
        print(f"param.json file not found at path: {input_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON from param.json: {e}")
        sys.exit(1)

    # Extract experiment ID and construct UNL file path
    lab_id = telnet_info.get("labId")
    if not lab_id:
        print("labId not found in param.json.")
        sys.exit(1)

    unl_file_path = f"/opt/unetlab/labs/{lab_id}.unl"

    if not os.path.exists(unl_file_path):
        print(f"UNL file does not exist at path: {unl_file_path}")
        sys.exit(1)

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
    try:
        with open(output_path, 'w') as f:
            f.write(json.dumps(mapping, indent=4))
        print(f"Mapping results written to {output_path}")
    except IOError as e:
        print(f"Error writing to output file: {e}")
        sys.exit(1)


if __name__ == "__main__":
    # Set up argument parsing
    parser = argparse.ArgumentParser(description="Process network topology from UNL and param.json.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json, use {t} for latest folder number.")
    parser.add_argument("-o", "--output", required=True, help="Output path for process information, use {t} for latest folder number.")
    args = parser.parse_args()

    main(args.input, args.output)
