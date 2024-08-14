import xml.etree.ElementTree as ET
import os
import json
import random
import subprocess

class NodeReader:
    def __init__(self, input_file_path, required_version):
        self.input_file_path = input_file_path
        self.required_version = required_version
        self.tree = None
        self.root = None
        self.nodes = None
        self.networks = None
        self.output_data = {
            "nodes": [],
            "links": [],
            "containers": []  # Add a section to hold container information
        }

    def parse_file(self):
        if not os.path.exists(self.input_file_path):
            print(f"File {self.input_file_path} does not exist.")
            return False
        
        self.tree = ET.parse(self.input_file_path)
        self.root = self.tree.getroot()

        # Check if the version matches the required version
        version = self.root.attrib.get("version")
        if version != self.required_version:
            print(f"File {self.input_file_path} version {version} does not match the required version {self.required_version}. Skipping...")
            return False

        self.nodes = self.root.find('topology').find('nodes')
        self.networks = self.root.find('topology').find('networks')
        return True

    def collect_nodes(self):
        for node in self.nodes.findall('node'):
            node_id = node.attrib.get('id')
            node_name = node.attrib.get('name')
            # Generate a random throughput value between 100 and 1000 Mbps
            throughput = random.randint(100, 1000)
            self.output_data["nodes"].append({
                "node_id": node_id,
                "node_name": node_name,
                "throughput_mbps": throughput  # Add throughput to node data
            })

    def collect_links(self):
        for network in self.networks.findall('network'):
            network_id = network.attrib.get('id')
            connected_nodes = self.get_connected_nodes(network_id)
            if len(connected_nodes) > 1:
                self.collect_network_links(connected_nodes)

    def get_connected_nodes(self, network_id):
        connected_nodes = []
        for node in self.nodes.findall('node'):
            for interface in node.findall('interface'):
                if interface.attrib.get('network_id') == network_id:
                    connected_nodes.append({
                        "node_id": node.attrib.get('id'),
                        "node_name": node.attrib.get('name'),
                        "interface_name": interface.attrib.get('name')
                    })
        return connected_nodes

    def collect_network_links(self, connected_nodes):
        for i in range(len(connected_nodes)):
            for j in range(i + 1, len(connected_nodes)):
                node1 = connected_nodes[i]
                node2 = connected_nodes[j]
                self.output_data["links"].append({
                    "source": {
                        "node_name": node1["node_name"],
                        "interface_name": node1["interface_name"]
                    },
                    "target": {
                        "node_name": node2["node_name"],
                        "interface_name": node2["interface_name"]
                    }
                })

    def get_container_ids(self):
        # Command to get the container IDs for the specified image
        command = "docker ps --filter ancestor=25125/frrouting:10-dev-05221913 --format '{{.ID}}'"
        try:
            # Execute the command and get the output
            result = subprocess.run(command, shell=True, capture_output=True, text=True)
            container_ids = result.stdout.strip().split('\n')
            if container_ids and container_ids[0]:  # Check if list is not empty and contains valid IDs
                self.output_data["containers"] = self.get_container_info(container_ids)
            else:
                print(f"No containers found for image 25125/frrouting:10-dev-05221913")
        except subprocess.CalledProcessError as e:
            print(f"Error getting container IDs: {e}")

    def get_container_info(self, container_ids):
        containers_info = []
        for container_id in container_ids:
            try:
                # Get container stats
                command_stats = f"docker stats {container_id} --no-stream --format '{{{{json .}}}}'"
                result_stats = subprocess.run(command_stats, shell=True, capture_output=True, text=True)
                container_stats = json.loads(result_stats.stdout.strip()) if result_stats.returncode == 0 else {}

                # Get container's /etc/frr/frr.conf file content
                command_conf = f"docker exec {container_id} cat /etc/frr/frr.conf"
                result_conf = subprocess.run(command_conf, shell=True, capture_output=True, text=True)
                frr_conf = result_conf.stdout.strip() if result_conf.returncode == 0 else "N/A"

                # Extract router name from frr.conf content
                router_name = self.extract_router_name(frr_conf)

                # Collect container info
                containers_info.append({
                    "container_id": container_id,
                    "cpu_percentage": container_stats.get('CPUPerc', 'N/A'),
                    "memory_usage": container_stats.get('MemUsage', 'N/A'),
                    "network_io": container_stats.get('NetIO', 'N/A'),
                    "block_io": container_stats.get('BlockIO', 'N/A'),
                    "pids": container_stats.get('PIDs', 'N/A'),
                    "router_name": router_name,
                    "frr_conf": frr_conf
                })
            except subprocess.CalledProcessError as e:
                print(f"Error retrieving information for container {container_id}: {e}")
        return containers_info

    def extract_router_name(self, frr_conf_content):
        # Simple extraction logic assuming the router name is set in the conf file
        for line in frr_conf_content.splitlines():
            if line.startswith('hostname'):
                return line.split()[1]  # Assuming the format is "hostname <router_name>"
        return "Unknown"

    def write_output(self, output_file_path):
        self.collect_nodes()
        self.collect_links()
        self.get_container_ids()  # Add container stats and router name to the output data
        # Clean up and organize the output data for better readability
        self.organize_output_data()
        with open(output_file_path, 'w') as file:
            json.dump(self.output_data, file, indent=4)
        print(f"Output written to {output_file_path}")

    def organize_output_data(self):
        # Optional: Organize and format container information for better readability
        for container in self.output_data["containers"]:
            # Simplify frr_conf to first few lines for readability, if needed
            container["frr_conf_preview"] = "\n".join(container["frr_conf"].splitlines()[:5]) + ("..." if len(container["frr_conf"].splitlines()) > 5 else "")

def process_unl_files(directory, output_file_name, required_version):
    # List all .unl files in the specified directory
    for file_name in os.listdir(directory):
        if file_name.endswith('.unl'):
            input_file_path = os.path.join(directory, file_name)
            print(f"Processing file: {file_name}")
            reader = NodeReader(input_file_path, required_version)
            if reader.parse_file():
                reader.write_output(output_file_name)

def main():
    # User can modify these parameters
    directory = '/opt/unetlab/labs'       # Directory containing .unl files
    output_file_name = 'node_link.json'   # Output file name
    required_version = '2'                # Required version of the .unl files

    # Call the function to process the files
    process_unl_files(directory, output_file_name, required_version)

if __name__ == "__main__":
    main()
