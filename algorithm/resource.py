import xml.etree.ElementTree as ET
import os
import json
import random
from datetime import datetime


class ExperimentProcessor:
    def __init__(self, input_base_dir, output_base_dir):
        self.input_base_dir = input_base_dir
        self.output_base_dir = output_base_dir

    def find_latest_folder(self, base_dir):
        # 获取reasoning目录下的所有文件夹
        folders = [f for f in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, f)) and f.isdigit()]

        if not folders:
            raise Exception(f"No folders found in {base_dir}")

        # 找到最新（最大编号）的文件夹
        latest_folder = max(folders, key=int)
        latest_folder_path = os.path.join(base_dir, latest_folder)

        return latest_folder_path, latest_folder

    def process_paths(self):
        # 查找最新的输入和输出文件夹
        input_folder_path, folder_name = self.find_latest_folder(self.input_base_dir)
        output_folder_path = os.path.join(self.output_base_dir, folder_name)  # 输出文件夹与输入文件夹编号相同

        # 自动设置输入和输出路径
        input_file = os.path.join(input_folder_path, 'params', 'param.json')
        output_folder = os.path.join(output_folder_path, 'res')

        # 确保输出文件夹存在
        if not os.path.exists(output_folder):
            os.makedirs(output_folder)

        output_file = os.path.join(output_folder, 'data.txt')

        # 返回输入文件路径和输出文件路径
        return input_file, output_file

# UNLParser类
class UNLParser:
    def __init__(self, param_file, input_folder, output_file_name):
        self.param_file = param_file
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

    def get_lab_id(self):
        """从 param.json 文件中读取 labId"""
        with open(self.param_file, 'r', encoding='utf-8') as f:
            params = json.load(f)
        lab_id = params['labId']
        return lab_id

    def parse_file(self):
        """根据 labId 确定要解析的 .unl 文件并提取节点和网络信息"""
        lab_id = self.get_lab_id()
        file_path = os.path.join(self.input_folder, f"{lab_id}.unl")

        if not os.path.exists(file_path):
            print(f"文件 {file_path} 不存在。")
            return False

        self.tree = ET.parse(file_path)
        self.root = self.tree.getroot()

        self.nodes = self.root.find('topology').find('nodes')
        self.networks = self.root.find('topology').find('networks')
        return True

    def collect_nodes(self):
        """收集并存储拓扑中所有节点的信息，并随机分配CPU和内存利用率。"""
        for node in self.nodes.findall('node'):
            node_id = node.attrib.get('id')
            node_name = node.attrib.get('name')
            cpu_utilization = round(random.uniform(10.0, 90.0), 2)
            memory_utilization = round(random.uniform(20.0, 80.0), 2)
            self.output_data["nodes"].append({
                "node_id": node_id,
                "node_name": node_name,
                "cpu_utilization": f"{cpu_utilization}%",
                "memory_utilization": f"{memory_utilization}%"
            })

    def collect_links(self):
        """收集并存储节点之间所有链路的信息，并随机分配带宽利用率、丢包率和延迟。"""
        for network in self.networks.findall('network'):
            network_id = network.attrib.get('id')
            connected_nodes = self.get_connected_nodes(network_id)
            if len(connected_nodes) > 1:
                self.collect_network_links(connected_nodes)

    def get_connected_nodes(self, network_id):
        """获取连接到特定网络（通过network_id）的节点。"""
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
        """在成对的连接节点之间创建链路条目。"""
        for i in range(len(connected_nodes)):
            for j in range(i + 1, len(connected_nodes)):
                node1 = connected_nodes[i]
                node2 = connected_nodes[j]
                bandwidth_utilization = round(random.uniform(10.0, 90.0), 2)
                packet_loss_rate = round(random.uniform(0.01, 5.0), 2)
                latency = round(random.uniform(1.0, 100.0), 2)
                self.output_data["links"].append({
                    "source": {
                        "node_name": node1["node_name"],
                        "interface_name": node1["interface_name"]
                    },
                    "target": {
                        "node_name": node2["node_name"],
                        "interface_name": node2["interface_name"]
                    },
                    "bandwidth_utilization": f"{bandwidth_utilization}%",
                    "packet_loss_rate": f"{packet_loss_rate}%",
                    "latency": f"{latency} ms"
                })

    def write_output(self):
        """写入收集到的节点和链路信息到JSON文件。"""
        with open(self.output_file_path, 'w', encoding='utf-8') as file:
            json.dump(self.output_data, file, indent=4)
        print(f"输出已写入 {self.output_file_path}")

    def process_unl_files(self):
        """处理指定文件夹中的所有.unl文件，并生成输出。"""
        for root, dirs, files in os.walk(self.input_folder):
            for file in files:
                if file.endswith('.unl'):
                    file_path = os.path.join(root, file)
                    print(f"处理文件: {file_path}")
                    if self.parse_file(file_path):
                        self.collect_nodes()
                        self.collect_links()

        # 写入所有收集的数据到输出文件
        self.write_output()


class NetworkOptimizer:
    def __init__(self, input_file):
        with open(input_file, 'r', encoding='utf-8') as file:
            self.data = json.load(file)
        self.nodes = self.data['nodes']
        self.links = self.data['links']

    def analyze_network(self):
        """Analyze the network and identify links with high utilization."""
        self.high_utilization_links = [
            link for link in self.links if float(link['bandwidth_utilization'].rstrip('%')) > 70
        ]

    def optimize_network(self):
        """Generate optimization strategies based on analysis."""
        self.optimization_strategies = []
        for link in self.high_utilization_links:
            bandwidth_utilization = float(link['bandwidth_utilization'].rstrip('%'))
            source_node = link['source']['node_name']
            target_node = link['target']['node_name']
            strategy = ""

            if bandwidth_utilization > 85:
                strategy = (f"增加 {source_node} 与 {target_node} 之间的带宽。"
                            f" (当前带宽利用率: {bandwidth_utilization}%)")
            else:
                strategy = (f"考虑为 {source_node} 到 {target_node} 的链路重新路由。"
                            f" (当前带宽利用率: {bandwidth_utilization}%)")

            # Adding resource information for the source and target nodes
            source_node_resources = next(node for node in self.nodes if node['node_name'] == source_node)
            target_node_resources = next(node for node in self.nodes if node['node_name'] == target_node)

            resource_info = (f"资源信息: "
                             f"{source_node} (CPU: {source_node_resources['cpu_utilization']}, "
                             f"内存: {source_node_resources['memory_utilization']}), "
                             f"{target_node} (CPU: {target_node_resources['cpu_utilization']}, "
                             f"内存: {target_node_resources['memory_utilization']})")

            full_strategy = f"{strategy}\n{resource_info}"
            self.optimization_strategies.append(full_strategy)

    def output_optimization_plan(self, output_file):
        """Output the generated optimization plan to a file."""
        with open(output_file, 'w', encoding='utf-8') as file:
            # Write report title and timestamp
            file.write("网络资源分配推演优化报告\n")
            file.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            file.write("=" * 50 + "\n\n")

            # Write each strategy
            for strategy in self.optimization_strategies:
                file.write(strategy + '\n\n')

        print(f"优化计划已写入 {output_file}")

    def process_optimization(self):
        """Perform the entire optimization process."""
        self.analyze_network()
        self.optimize_network()
        self.output_optimization_plan(output_file)

# 示例使用
if __name__ == "__main__":
    # 使用 ExperimentProcessor 类来处理输入和输出路径
    processor = ExperimentProcessor(input_base_dir="/uploadPath/reasoning", output_base_dir="/uploadPath/reasoning")
    input_file, output_file = processor.process_paths()

    # 解析param.json并生成UNL文件路径
    param_file = input_file  # param.json 的路径由 processor.process_paths() 返回
    unl_parser = UNLParser(param_file=param_file, input_folder='/opt/unetlab/labs', output_file_name='node_link.json')

    # 根据 param.json 中的 labId 确定并解析对应的 UNL 文件
    if unl_parser.parse_file():
        unl_parser.collect_nodes()
        unl_parser.collect_links()

    # 写入收集的数据到JSON文件
    unl_parser.write_output()

    # 根据解析数据优化网络
    optimizer = NetworkOptimizer('node_link.json')
    optimizer.process_optimization()

