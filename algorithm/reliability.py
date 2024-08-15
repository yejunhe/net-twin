import xml.etree.ElementTree as ET
import os
import json
import random
import datetime

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
        """Collect and store information about all nodes in the topology."""
        for node in self.nodes.findall('node'):
            node_id = node.attrib.get('id')
            node_name = node.attrib.get('name')
            self.output_data["nodes"].append({
                "node_id": node_id,
                "node_name": node_name
            })

    def collect_links(self):
        """Collect and store information about all links between nodes."""
        for network in self.networks.findall('network'):
            network_id = network.attrib.get('id')
            connected_nodes = self.get_connected_nodes(network_id)
            if len(connected_nodes) > 1:
                self.collect_network_links(connected_nodes)

    def get_connected_nodes(self, network_id):
        """Get nodes connected to a specific network by network_id."""
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
        """Create link entries between pairs of connected nodes."""
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
        """Write the collected node and link information to a JSON file."""
        with open(self.output_file_path, 'w') as file:
            json.dump(self.output_data, file, indent=4)
        print(f"Output written to {self.output_file_path}")

class UNLNetworkAnalyzer(UNLParser):
    def __init__(self, param_file, input_folder, output_file_name, reliability_output):
        super().__init__(param_file=param_file, input_folder=input_folder, output_file_name=output_file_name)
        self.reliability_output_path = os.path.join(os.getcwd(), reliability_output)

    def assign_random_parameters(self):
        """Assign random network parameters to each node and link."""
        for node in self.output_data["nodes"]:
            node["bandwidth"] = random.randint(100, 1000)  # in Mbps
            node["latency"] = round(random.uniform(1, 50), 3)       # in ms
            node["packet_loss"] = random.uniform(0, 0.05) # packet loss rate

        for link in self.output_data["links"]:
            link["bandwidth"] = random.randint(100, 1000)  # in Mbps
            link["latency"] = round(random.uniform(1, 50), 3)        # in ms
            link["packet_loss"] = random.uniform(0, 0.05)  # packet loss rate

    def calculate_reliability(self):
        """Calculate and evaluate the network reliability based on parameters."""
        total_latency = round(sum(link["latency"] for link in self.output_data["links"]),3)
        average_packet_loss = sum(link["packet_loss"] for link in self.output_data["links"]) / len(self.output_data["links"])
        reliability_score = (1 - average_packet_loss) * 100  # Simple reliability score based on packet loss
        return {
            "total_latency": total_latency,
            "average_packet_loss": average_packet_loss,
            "reliability_score": reliability_score
        }

    def write_reliability_output(self, reliability_data):
        """Write the network reliability data to a file."""
        with open(self.reliability_output_path, 'w', encoding='utf-8') as file:
            # 写入报告标题和时间戳
            file.write("网络可靠性评估报告\n")
            file.write(f"生成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

            # 写入链路和节点的资源信息
            file.write("节点资源信息:\n")
            for node in self.output_data["nodes"]:
                file.write(f"节点ID: {node['node_id']}, 节点名称: {node['node_name']}, "
                           f"带宽: {node['bandwidth']} Mbps, 延迟: {node['latency']} ms, "
                           f"丢包率: {node['packet_loss']:.2%}\n")

            file.write("\n链路资源信息:\n")
            for link in self.output_data["links"]:
                file.write(f"源节点: {link['source']['node_name']} (接口: {link['source']['interface_name']}), "
                           f"目标节点: {link['target']['node_name']} (接口: {link['target']['interface_name']}), "
                           f"带宽: {link['bandwidth']} Mbps, 延迟: {link['latency']} ms, "
                           f"丢包率: {link['packet_loss']:.2%}\n")

            # 分割线与关键指标
            file.write("\n" + "-" * 40 + "\n")
            file.write(f"总网络延迟: {reliability_data['total_latency']} ms\n")
            file.write("-" * 40 + "\n")
            file.write(f"平均丢包率: {reliability_data['average_packet_loss']:.2%}\n")
            file.write("-" * 40 + "\n")
            file.write(f"网络可靠性评估: {reliability_data['reliability_score']:.2f}%\n")
            file.write("-" * 40 + "\n")

    def process_unl_file(self):
        """Extend the method to include reliability calculations and output."""
        if self.parse_file():  # 不再需要传递 file_path
            self.collect_nodes()
            self.collect_links()
            self.assign_random_parameters()
            reliability_data = self.calculate_reliability()
            self.write_output()
            self.write_reliability_output(reliability_data)

    def process_unl_files(self):
        """Process all .unl files in the specified folder that match the target version."""
        for root, dirs, files in os.walk(self.input_folder):
            for file in files:
                if file.endswith('.unl'):
                    file_path = os.path.join(root, file)
                    print(f"Processing file: {file_path}")
                    self.process_unl_file(file_path)  # Call the updated method with the correct file path

# Example usage:
if __name__ == "__main__":
    # 使用 ExperimentProcessor 类来处理输入和输出路径
    processor = ExperimentProcessor(input_base_dir="/uploadPath/reasoning", output_base_dir="/uploadPath/reasoning")
    param_file, output_file = processor.process_paths()

    # 创建 UNLNetworkAnalyzer 对象，并传递 param_file, input_folder, output_file 和 reliability_output
    unl_analyzer = UNLNetworkAnalyzer(param_file=param_file, input_folder='/opt/unetlab/labs',
                                      output_file_name='node_link.json', reliability_output=output_file)

    # 处理 .unl 文件并生成输出
    unl_analyzer.process_unl_file()  # 直接调用 process_unl_file 而不传递参数


