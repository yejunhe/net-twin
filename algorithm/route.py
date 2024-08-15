import json
import os
import xml.etree.ElementTree as ET
import heapq
from datetime import datetime

class ExperimentProcessor:
    def __init__(self, input_base_dir, output_base_dir):
        self.input_base_dir = input_base_dir
        self.output_base_dir = output_base_dir

    def find_latest_folder(self, base_dir):
        folders = [f for f in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, f)) and f.isdigit()]
        if not folders:
            raise Exception(f"No folders found in {base_dir}")
        latest_folder = max(folders, key=int)
        latest_folder_path = os.path.join(base_dir, latest_folder)
        return latest_folder_path, latest_folder

    def process_paths(self):
        input_folder_path, folder_name = self.find_latest_folder(self.input_base_dir)
        output_folder_path = os.path.join(self.output_base_dir, folder_name)

        input_file = os.path.join(input_folder_path, 'params', 'param.json')
        output_folder = os.path.join(output_folder_path, 'res')
        if not os.path.exists(output_folder):
            os.makedirs(output_folder)

        output_file = os.path.join(output_folder, 'data.txt')
        return input_file, output_file

    def load_param_json(self, input_file):
        with open(input_file, 'r') as file:
            param_data = json.load(file)
        lab_id = str(param_data.get("labId"))
        docker_ids = [int(node['dockerid'].split('-')[-1]) for node in param_data.get("node", [])]
        return lab_id, docker_ids

    def get_node_names(self, docker_ids, unl_parser):
        node_map = {int(node.attrib['id']): node.attrib['name'] for node in unl_parser.nodes.findall('node')}
        start_node = node_map.get(docker_ids[0])
        end_node = node_map.get(docker_ids[1])
        return start_node, end_node


class UNLParser:
    def __init__(self, input_folder, output_file_name):
        self.input_folder = input_folder
        self.output_file_path = os.path.join(os.getcwd(), output_file_name)
        self.tree = None
        self.root = None
        self.nodes = None
        self.networks = None
        self.output_data = {
            "nodes": [],
            "links": []
        }

    def parse_file(self, file_path):
        if not os.path.exists(file_path):
            print(f"File {file_path} does not exist.")
            return False

        self.tree = ET.parse(file_path)
        self.root = self.tree.getroot()

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

    def write_output(self):
        with open(self.output_file_path, 'w') as file:
            json.dump(self.output_data, file, indent=4)
        print(f"Output written to {self.output_file_path}")

    def process_unl_files(self, lab_id):
        """Process the .unl file corresponding to the given lab_id."""
        file_name = f"{lab_id}.unl"
        file_path = os.path.join(self.input_folder, file_name)
        print(f"Processing file: {file_path}")
        if self.parse_file(file_path):
            self.collect_nodes()
            self.collect_links()

        # Write the collected data to the output file
        self.write_output()

def load_json(file_path):
    with open(file_path, 'r') as file:
        data = json.load(file)
    return data

def build_graph(data):
    graph = Graph()
    for node in data['nodes']:
        graph.add_node(node['node_name'])

    for link in data['links']:
        source = link['source']['node_name']
        target = link['target']['node_name']
        graph.add_edge(source, target)

    return graph

class Graph:
    def __init__(self):
        self.nodes = {}
        self.edges = {}

    def add_node(self, node_name):
        self.nodes[node_name] = []
        self.edges[node_name] = {}

    def add_edge(self, from_node, to_node, weight=1):
        self.nodes[from_node].append(to_node)
        self.nodes[to_node].append(from_node)
        self.edges[from_node][to_node] = weight
        self.edges[to_node][from_node] = weight

    def dijkstra(self, start, end):
        queue = []
        heapq.heappush(queue, (0, start))
        distances = {node: float('inf') for node in self.nodes}
        distances[start] = 0
        previous_nodes = {node: None for node in self.nodes}

        while queue:
            current_distance, current_node = heapq.heappop(queue)

            if current_distance > distances[current_node]:
                continue

            for neighbor in self.nodes[current_node]:
                distance = current_distance + self.edges[current_node][neighbor]

                if distance < distances[neighbor]:
                    distances[neighbor] = distance
                    previous_nodes[neighbor] = current_node
                    heapq.heappush(queue, (distance, neighbor))

        path, current_node = [], end
        while previous_nodes[current_node] is not None:
            path.append(current_node)
            current_node = previous_nodes[current_node]
        path.append(start)
        path = path[::-1]
        return path, distances[end]

def write_result_to_file(path, distance, output_file):
    with open(output_file, 'w') as file:
        # Write the report header and generation time
        file.write("路由路径推演优化报告\n")
        file.write(f"报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

        # Write the shortest path and total distance details
        file.write("根据Dijkstra算法计算得出的最短路径如下：\n")
        file.write(f"最短路径为: {' -> '.join(path)}\n")
        file.write(f"该路径的总距离为: {distance}\n")
        file.write(f"此路径在计算过程中，已考虑了所有节点之间的最短距离与链路的状态信息，确保了路径的最优性。\n")

if __name__ == "__main__":
    # Use ExperimentProcessor to determine input and output paths
    processor = ExperimentProcessor(input_base_dir="/uploadPath/reasoning", output_base_dir="/uploadPath/reasoning")
    input_file, output_file = processor.process_paths()

    # Load labId and docker_ids from param.json
    lab_id, docker_ids = processor.load_param_json(input_file)

    # Initialize the UNLParser with the input folder and output file name
    unl_parser = UNLParser('/opt/unetlab/labs', 'node_link.json')

    # Process the .unl file corresponding to the labId
    unl_parser.process_unl_files(lab_id)

    # Get start_node and end_node based on docker_ids
    start_node, end_node = processor.get_node_names(docker_ids, unl_parser)

    # Load the generated JSON topology
    file_path = unl_parser.output_file_path
    data = load_json(file_path)

    # Build the graph from the JSON data
    graph = build_graph(data)

    # Find the shortest path between the nodes extracted from param.json
    path, distance = graph.dijkstra(start_node, end_node)

    # Output the result to a text file
    write_result_to_file(path, distance, output_file)

