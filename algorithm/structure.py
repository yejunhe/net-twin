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
    
# UNLParser Class
class UNLParser:
    def __init__(self, param_json_path=None, output_file_name=None):
        self.lab_id = None
        self.input_file_path = None
        self.output_file_path = os.path.join(os.getcwd(), output_file_name) if output_file_name else None
        self.tree = None
        self.root = None
        self.nodes = None
        self.networks = None
        self.output_data = {
            "nodes": [],
            "links": []
        }

        if param_json_path:
            self.load_lab_id(param_json_path)
            self.set_input_file_path()

    def load_lab_id(self, param_json_path):
        with open(param_json_path, 'r') as file:
            params = json.load(file)
        self.lab_id = params['labId']

    def set_input_file_path(self):
        self.input_file_path = os.path.join('/opt/unetlab/labs', f'{self.lab_id}.unl')

    def parse_file(self):
        if not os.path.exists(self.input_file_path):
            print(f"File {self.input_file_path} does not exist.")
            return False

        self.tree = ET.parse(self.input_file_path)
        self.root = self.tree.getroot()
        self.nodes = self.root.find('topology').find('nodes')
        self.networks = self.root.find('topology').find('networks')
        return True

    def collect_nodes(self):
        for node in self.nodes.findall('node'):
            node_id = node.attrib.get('id')
            node_name = node.attrib.get('name')
            resource_usage = self.generate_resource_usage()
            self.output_data["nodes"].append({
                "node_id": node_id,
                "node_name": node_name,
                "resource_usage": resource_usage
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
                link_metrics = self.generate_link_metrics()
                self.output_data["links"].append({
                    "source": {
                        "node_name": node1["node_name"],
                        "interface_name": node1["interface_name"]
                    },
                    "target": {
                        "node_name": node2["node_name"],
                        "interface_name": node2["interface_name"]
                    },
                    "metrics": link_metrics
                })

    def generate_resource_usage(self):
        return {
            "cpu_usage": random.randint(0, 100),
            "memory_usage": random.randint(0, 100)
        }

    def generate_link_metrics(self):
        return {
            "bandwidth_usage": random.randint(0, 100),
            "latency": round(random.uniform(1.0, 100.0),3),
            "packet_loss": round(random.uniform(0, 5),3)
        }

    def write_output(self):
        with open(self.output_file_path, 'w') as file:
            json.dump(self.output_data, file, indent=4)
        print(f"Output written to {self.output_file_path}")

    def process_unl_file(self):
        if self.parse_file():
            self.collect_nodes()
            self.collect_links()
            self.write_output()

# NetworkOptimizer Class
class NetworkOptimizer:
    def __init__(self, data_file):
        self.data_file = data_file
        self.nodes = []
        self.links = []
        self.report = []

    def load_data(self):
        with open(self.data_file, 'r') as file:
            data = json.load(file)
        self.nodes = data['nodes']
        self.links = data['links']

    def evaluate_network(self):
        for link in self.links:
            bandwidth_usage = link['metrics']['bandwidth_usage']
            latency = link['metrics']['latency']
            packet_loss = link['metrics']['packet_loss']
            link_report = f"链路从 {link['source']['node_name']} 到 {link['target']['node_name']}："
            issues = []
            suggestions = []

            if bandwidth_usage > 80:
                issues.append(f"带宽使用率过高，当前使用率：{bandwidth_usage}%")
                suggestions.append("考虑使用负载均衡技术分散流量，或者添加冗余链路来分摊高流量负荷。")
            if latency > 50:
                issues.append(f"延迟过高，当前延迟：{latency} ms")
                suggestions.append("重新评估和优化网络路由，可能需要重新规划网络拓扑以减少跳数。")
            if packet_loss > 1:
                issues.append(f"丢包率过高，当前丢包率：{packet_loss}%")
                suggestions.append("增强链路冗余或引入更高效的错误恢复协议，如使用MPLS进行链路恢复。")

            if issues:
                link_report += '；'.join(issues) + "。"
                link_report += " 建议：" + ' '.join(suggestions)
            else:
                link_report += "该链路性能良好。"
            self.report.append(link_report)

    def write_report_to_txt(self):
        file_name = output_file
        with open(file_name, 'w', encoding='utf-8') as file:
            file.write("网络结构推演优化报告\n")
            file.write("=============\n")
            file.write(f"报告生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            file.write("=============\n\n")
            for item in self.report:
                file.write(item + "\n")
                file.write("-" * 40 + "\n")
        print(f"报告已写入到文件：{file_name}")

    def optimize_network(self):
        self.load_data()
        self.evaluate_network()
        self.write_report_to_txt()

# Main usage example:
if __name__ == "__main__":
    # 使用 ExperimentProcessor 类来处理输入和输出路径
    processor = ExperimentProcessor(input_base_dir="/uploadPath/reasoning", output_base_dir="/uploadPath/reasoning")
    input_file, output_file = processor.process_paths()

    # 使用新的 UNLParser 类逻辑
    parser = UNLParser(param_json_path=input_file, output_file_name=output_file)
    parser.process_unl_file()

    # 使用 NetworkOptimizer 进行网络优化
    optimizer = NetworkOptimizer(output_file)
    optimizer.optimize_network()  # This reads the JSON file, evaluates the network, and writes the report



