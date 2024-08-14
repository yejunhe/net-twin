import xml.etree.ElementTree as ET
import os
import json
import numpy as np

class NodeReader:
    def __init__(self, input_directory, output_file_name, required_version):
        self.input_directory = input_directory
        self.output_file_path = os.path.join(os.getcwd(), output_file_name)
        self.required_version = required_version
        self.tree = None
        self.root = None
        self.nodes = None
        self.networks = None
        self.output_data = {
            "nodes": [],
            "links": [],
            "adjacency_matrix": {}
        }

    def parse_file(self, file_path):
        if not os.path.exists(file_path):
            print(f"File {file_path} does not exist.")
            return False
        
        self.tree = ET.parse(file_path)
        self.root = self.tree.getroot()

        version = self.root.attrib.get("version", "")
        if version != self.required_version:
            print(f"File {file_path} version {version} does not match the required version {self.required_version}. Skipping.")
            return False

        self.nodes = self.root.find('topology').find('nodes')
        self.networks = self.root.find('topology').find('networks')
        return True

    def collect_nodes(self):
        for node in self.nodes.findall('node'):
            node_id = node.attrib.get('id')
            node_name = node.attrib.get('name')
            self.output_data["nodes"].append({
                "node_id": node_id,
                "node_name": node_name
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

    def generate_adjacency_matrix(self):
        node_names = [node["node_name"] for node in self.output_data["nodes"]]
        size = len(node_names)
        adjacency_matrix = np.zeros((size, size), dtype=int)

        name_to_index = {name: index for index, name in enumerate(node_names)}

        for link in self.output_data["links"]:
            source_index = name_to_index[link["source"]["node_name"]]
            target_index = name_to_index[link["target"]["node_name"]]
            adjacency_matrix[source_index][target_index] = 1
            adjacency_matrix[target_index][source_index] = 1  # Undirected graph

        # Convert adjacency matrix to a list of lists for JSON serialization
        adjacency_matrix_list = adjacency_matrix.tolist()
        
        self.output_data["adjacency_matrix"]["node_names"] = node_names
        self.output_data["adjacency_matrix"]["matrix"] = adjacency_matrix_list

    def write_output(self):
        with open(self.output_file_path, 'w') as file:
            json.dump(self.output_data, file, indent=4)
        print(f"Output written to {self.output_file_path}")

    def process_directory(self):
        for file_name in os.listdir(self.input_directory):
            if file_name.endswith('.unl'):
                file_path = os.path.join(self.input_directory, file_name)
                if self.parse_file(file_path):
                    self.collect_nodes()
                    self.collect_links()
                    self.generate_adjacency_matrix()

        self.write_output()

def parse_unl_files():
    reader = NodeReader('/opt/unetlab/labs', 'node_link.json', required_version="2")
    reader.process_directory()

if __name__ == "__main__":
    parse_unl_files()
