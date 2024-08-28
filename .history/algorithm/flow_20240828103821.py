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


    def format_container_stats(self, container_id, stats):
        formatted_stats = []
        formatted_stats.append("="*60)
        formatted_stats.append(f"Resource Usage for Container ID: {container_id}")
        formatted_stats.append(f"Name: {stats['Name']}")
        formatted_stats.append(f"CPU Usage: {stats['CPU']}")
        formatted_stats.append(f"Memory Usage: {stats['Memory']}")
        formatted_stats.append(f"Network I/O: {stats['NetIO']}")
        formatted_stats.append("\n")
        return "\n".join(formatted_stats)

    def schedule_traffic(self, nodes, container_stats):
        weights = {}
        for node in nodes:
            container_id = self.get_container_id_by_node(node['id'], container_stats)
            if container_id and container_id in container_stats:
                cpu_usage = float(container_stats[container_id]['CPU'].strip('%'))
                mem_usage = float(container_stats[container_id]['Memory'].split('/')[0].strip('MiB'))
                
                # Calculate weights based on resource usage (example: inverse of usage)
                weights[node['id']] = 1 / (cpu_usage + mem_usage + 1e-5)

        total_weight = sum(weights.values())
        normalized_weights = {node_id: weight / total_weight for node_id, weight in weights.items()}

        # Choose node based on weighted probability
        chosen_node_id = random.choices(list(normalized_weights.keys()), weights=list(normalized_weights.values()))[0]
        return chosen_node_id

    def get_frr_config_from_container(self, container_id):
        try:
            result = subprocess.run(['docker', 'exec', container_id, 'cat', '/etc/frr/frr.conf'],
                                    capture_output=True, text=True, check=True)
            return result.stdout
        except subprocess.CalledProcessError as e:
            return f"Failed to get configuration from container {container_id}. Error: {e}"

    def get_container_stats(self):
        result = subprocess.run(['docker', 'stats', '--no-stream', '--format', 
                                '{{.ID}} {{.Name}} {{.CPUPerc}} {{.MemUsage}} {{.NetIO}}'],
                                capture_output=True, text=True)
        stats = {}
        for line in result.stdout.strip().splitlines():
            container_id, container_name, cpu, mem_usage, net_io = line.split(maxsplit=4)
            stats[container_id] = {
                'Name': container_name,
                'CPU': cpu,
                'Memory': mem_usage,
                'NetIO': net_io
            }
        return stats

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

    def get_container_id_by_node(self, node_id, container_stats):
        for container_id, stats in container_stats.items():
            if node_id in stats['Name']:
                return container_id
        return None

    def extract_and_save_configurations(self):
        # Existing code for extracting and saving configurations...

        container_stats = self.get_container_stats()

        with open(self.output_file_path, 'w') as output_file:
            output_file.write(topology_info)
            print(topology_info)  # 输出拓扑信息到控制台

            for container_id in matched_container_ids:
                config_content = self.get_frr_config_from_container(container_id)
                hostname, interfaces, router_configs = self.parse_frr_config(config_content)
                formatted_output = self.format_output(container_id, hostname, interfaces, router_configs)

                # Add container stats to the output
                formatted_stats = self.format_container_stats(container_id, container_stats[container_id])
                output_file.write(formatted_output)
                output_file.write(formatted_stats)
                print(formatted_output)  # 也可以同时输出到控制台
                print(formatted_stats)

            # Example of applying the traffic scheduling
            optimal_node_id = self.schedule_traffic(nodes, container_stats)
            output_file.write(f"\nOptimal node for traffic scheduling: {optimal_node_id}\n")
            print(f"Optimal node for traffic scheduling: {optimal_node_id}")

        print(f"Configuration information written to {self.output_file_path}")
# 使用示例
if __name__ == "__main__":
    reasoning_directory = "/uploadPath/reasoning"
    labs_directory = "/opt/unetlab/labs"
    output_file_path = "frr_configs.txt"
    
    extractor = FRRConfigExtractor(reasoning_directory, labs_directory, output_file_path)
    extractor.extract_and_save_configurations()
