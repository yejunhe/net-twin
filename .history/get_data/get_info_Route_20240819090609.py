import os
import json
import xml.etree.ElementTree as ET
import subprocess

class FRRConfigExtractor:
    def __init__(self, reasoning_directory, labs_directory, output_file_path):
        self.reasoning_directory = reasoning_directory
        self.labs_directory = labs_directory
        self.output_file_path = output_file_path

    def get_latest_directory(self, directory):
        dirs = [os.path.join(directory, d) for d in os.listdir(directory) if os.path.isdir(os.path.join(directory, d))]
        latest_dir = max(dirs, key=os.path.getmtime)
        return latest_dir

    def get_lab_id_from_json(self, json_file):
        with open(json_file, 'r') as file:
            data = json.load(file)
            lab_id = data.get('labId')
        return lab_id

    def get_unl_file_path(self, lab_id):
        unl_file_name = f"{lab_id}.unl"
        unl_file_path = os.path.join(self.labs_directory, unl_file_name)
        return unl_file_path

    def get_lab_id_from_unl(self, unl_file):
        tree = ET.parse(unl_file)
        root = tree.getroot()
        lab_id = root.get('id')
        return lab_id

    def get_nodes_and_links_from_unl(self, unl_file):
        tree = ET.parse(unl_file)
        root = tree.getroot()

        nodes = []
        for node in root.findall(".//node"):
            node_info = {
                'id': node.get('id'),
                'name': node.get('name'),
                'type': node.get('type'),
                'image': node.get('image'),
                'ram': node.get('ram'),
                'console': node.get('console'),
                'interfaces': []
            }
            for interface in node.findall(".//interface"):
                interface_info = {
                    'id': interface.get('id'),
                    'name': interface.get('name'),
                    'type': interface.get('type'),
                    'network_id': interface.get('network_id')
                }
                node_info['interfaces'].append(interface_info)
            nodes.append(node_info)

        links = []
        for network in root.findall(".//network"):
            link_info = {
                'id': network.get('id'),
                'type': network.get('type'),
                'name': network.get('name')
            }
            links.append(link_info)

        return nodes, links

    def get_running_containers(self):
        result = subprocess.run(['docker', 'ps', '--format', '{{.ID}} {{.Names}}'], capture_output=True, text=True)
        containers = {}
        for line in result.stdout.strip().splitlines():
            container_id, container_name = line.split(maxsplit=1)
            containers[container_name] = container_id
        return containers

    def match_ids_with_containers(self, lab_id, containers):
        matched_containers = []
        for container_name, container_id in containers.items():
            if lab_id in container_name:
                matched_containers.append(container_id)
        return matched_containers

    def get_frr_config_from_container(self, container_id):
        try:
            result = subprocess.run(['docker', 'exec', container_id, 'cat', '/etc/frr/frr.conf'],
                                    capture_output=True, text=True, check=True)
            return result.stdout
        except subprocess.CalledProcessError as e:
            return f"Failed to get configuration from container {container_id}. Error: {e}"

    def get_container_stats(self):
        try:
            result = subprocess.run(['docker', 'stats', '--no-stream', '--format',
                                     '"{{.ID}} {{.Name}} {{.CPUPerc}} {{.MemUsage}} {{.NetIO}} {{.BlockIO}} {{.PIDs}}"'],
                                    capture_output=True, text=True, check=True)
            stats = {}
            for line in result.stdout.strip().splitlines():
                line = line.strip().strip('"')
                parts = line.split(maxsplit=6)
                container_id = parts[0]
                stats[container_id] = {
                    'Name': parts[1],
                    'CPU': parts[2],
                    'Memory': parts[3],
                    'NetIO': parts[4],
                    'BlockIO': parts[5],
                    'PIDs': parts[6],
                }
            return stats
        except subprocess.CalledProcessError as e:
            return {}

    def parse_frr_config(self, config_content):
        lines = config_content.strip().splitlines()
        hostname = None
        interfaces = []
        router_configs = []
        current_section = None
        
        for line in lines:
            line = line.strip()
            if line.startswith("hostname"):
                hostname = line.split(maxsplit=1)[1]
            elif line.startswith("interface"):
                current_section = "interface"
                interfaces.append(line)
            elif line.startswith("router"):
                current_section = "router"
                router_configs.append(line)
            elif current_section == "interface":
                if line == "!":
                    current_section = None
                else:
                    interfaces.append(line)
            elif current_section == "router":
                if line == "!":
                    current_section = None
                else:
                    router_configs.append(line)
        return hostname, "\n".join(interfaces), "\n".join(router_configs)

    def format_output(self, container_id, hostname, interfaces, router_configs, stats):
        formatted_output = []
        formatted_output.append("="*60)
        formatted_output.append(f"Container ID: {container_id}")
        formatted_output.append(f"Hostname: {hostname}")
        if container_id in stats:
            formatted_output.append(f"Stats: CPU: {stats[container_id]['CPU']}, Memory: {stats[container_id]['Memory']}, "
                                    f"Net I/O: {stats[container_id]['NetIO']}, Block I/O: {stats[container_id]['BlockIO']}, "
                                    f"PIDs: {stats[container_id]['PIDs']}")
        else:
            formatted_output.append("Stats: Not available")
        formatted_output.append("="*60)
        formatted_output.append("Interface Configuration:")
        formatted_output.append(interfaces)
        formatted_output.append("\nRouter Configuration:")
        formatted_output.append(router_configs)
        formatted_output.append("\n")
        return "\n".join(formatted_output)

    def format_topology_info(self, nodes, links):
        formatted_output = []
        formatted_output.append("="*60)
        formatted_output.append("Topology Information:")
        formatted_output.append("="*60)
        
        formatted_output.append("Nodes:")
        for node in nodes:
            formatted_output.append(f"Node ID: {node['id']}, Name: {node['name']}, Type: {node['type']}, Image: {node['image']}")
            for interface in node['interfaces']:
                formatted_output.append(f"  Interface ID: {interface['id']}, Name: {interface['name']}, Type: {interface['type']}, Network ID: {interface['network_id']}")
        
        formatted_output.append("\nLinks:")
        for link in links:
            formatted_output.append(f"Link ID: {link['id']}, Type: {link['type']}, Name: {link['name']}")
        
        formatted_output.append("\n")
        return "\n".join(formatted_output)

    def extract_and_save_configurations(self):
        latest_dir = self.get_latest_directory(self.reasoning_directory)
        json_file_path = os.path.join(latest_dir, 'params', 'param.json')
        
        lab_id = self.get_lab_id_from_json(json_file_path)
        
        if not lab_id:
            print("No labId found in the param.json file.")
            return
        
        unl_file_path = self.get_unl_file_path(lab_id)
        
        if not os.path.exists(unl_file_path):
            print(f".unl file {unl_file_path} not found.")
            return
        
        lab_id_from_unl = self.get_lab_id_from_unl(unl_file_path)
        
        if not lab_id_from_unl:
            print("No lab id found in the .unl file.")
            return
        
        nodes, links = self.get_nodes_and_links_from_unl(unl_file_path)
        topology_info = self.format_topology_info(nodes, links)
        
        running_containers = self.get_running_containers()
        matched_container_ids = self.match_ids_with_containers(lab_id_from_unl, running_containers)
        
        container_stats = self.get_container_stats()
        
        if matched_container_ids:
            with open(self.output_file_path, 'w') as output_file:
                output_file.write(topology_info)
                print(topology_info)
                
                for container_id in matched_container_ids:
                    config_content = self.get_frr_config_from_container(container_id)
                    hostname, interfaces, router_configs = self.parse_frr_config(config_content)
                    formatted_output = self.format_output(container_id, hostname, interfaces, router_configs, container_stats)
                    output_file.write(formatted_output)
                    print(formatted_output)
            print(f"Configuration information written to {self.output_file_path}")
        else:
            print("No matching containers found.")

# 使用示例
if __name__ == "__main__":
    reasoning_directory = "/uploadPath/reasoning"
    labs_directory = "/opt/unetlab/labs"
    output_file_path = "frr_configs.txt"
    
    extractor = FRRConfigExtractor(reasoning_directory, labs_directory, output_file_path)
    extractor.extract_and_save_configurations()
