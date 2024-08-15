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
    def __init__(self, input_folder, param_file_path, output_file_name):
        self.input_folder = input_folder
        self.param_file_path = param_file_path
        self.output_file_path = os.path.join(os.getcwd(), output_file_name)
        self.tree = None
        self.root = None
        self.nodes = None
        self.networks = None
        self.output_data = {
            "nodes": [],
            "links": []
        }
        self.target_file_name = self.get_target_unl_filename()

    def get_target_unl_filename(self):
        with open(self.param_file_path, 'r', encoding='utf-8') as file:
            params = json.load(file)
            lab_id = params.get("labId")
            if lab_id is None:
                raise Exception("labId not found in param.json")
            return f"{lab_id}.unl"

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
                    "source": node1,
                    "target": node2,
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
        with open(self.output_file_path, 'w', encoding='utf-8') as file:
            json.dump(self.output_data, file, indent=4)
        print(f"Output written to {self.output_file_path}")

    def process_unl_files(self):
        target_file_path = os.path.join(self.input_folder, self.target_file_name)
        print(f"Processing file: {target_file_path}")
        if self.parse_file(target_file_path):
            self.collect_nodes()
            self.collect_links()

        self.write_output()


class ParameterOptimizer:
    def __init__(self, data_file, output_file):
        self.data_file = data_file
        self.output_file = output_file
        self.data = None

    def load_data(self):
        with open(self.data_file, 'r', encoding='utf-8') as file:
            self.data = json.load(file)

    def analyze_and_optimize(self):
        recommendations = []
        for node in self.data['nodes']:
            cpu_usage = node['resource_usage']['cpu_usage']
            memory_usage = node['resource_usage']['memory_usage']
            if cpu_usage > 80:
                recommendations.append(
                    f"节点 {node['node_name']} 的CPU使用率为 {cpu_usage}%，建议增加CPU资源或优化应用性能。")
            if memory_usage > 80:
                recommendations.append(f"节点 {node['node_name']} 的内存使用率为 {memory_usage}%，建议增加内存容量。")
        for link in self.data['links']:
            latency = link['metrics']['latency']
            packet_loss = link['metrics']['packet_loss']
            bandwidth_usage = link['metrics']['bandwidth_usage']
            if latency > 50:
                recommendations.append(
                    f"链路从 {link['source']['node_name']} 到 {link['target']['node_name']} 的延迟为 {latency}ms，建议检查网络配置或升级硬件。")
            if packet_loss > 1:
                recommendations.append(
                    f"链路从 {link['source']['node_name']} 到 {link['target']['node_name']} 的丢包率为 {packet_loss}%，建议优化网络链路。")
            if bandwidth_usage > 80:
                recommendations.append(
                    f"链路 {link['source']['node_name']} 到 {link['target']['node_name']} 的带宽使用率为 {bandwidth_usage}%，建议升级带宽。")
        return recommendations

    import datetime

    def write_recommendations(self, recommendations):
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header = "参数配置优化建议报告"
        timestamp = f"生成时间: {current_time}\n"

        with open(self.output_file, 'w', encoding='utf-8') as file:
            file.write(header + '\n')
            file.write('=' * len(header) + '\n')  # 添加标题分隔符
            file.write(timestamp + '\n')

            last_item = None
            for recommendation in recommendations:
                current_item = recommendation.split()[1]  # 假设建议格式始终以节点名开头，如“节点 A 的CPU...”
                if last_item and current_item != last_item:
                    file.write('-' * 40 + '\n')  # 在不同节点或链路间添加分割线
                file.write(recommendation + "\n\n")  # 添加建议并在每条建议后增加空行
                last_item = current_item

        print(f"Optimization recommendations written to {self.output_file}")

    def process_optimization(self):
        self.load_data()
        recommendations = self.analyze_and_optimize()
        self.write_recommendations(recommendations)


if __name__ == "__main__":
    # 使用 ExperimentProcessor 类来处理输入和输出路径
    processor = ExperimentProcessor(input_base_dir="/uploadPath/reasoning", output_base_dir="/uploadPath/reasoning")
    input_file, output_file = processor.process_paths()

    # 创建 UNLParser 实例时传递 input_folder 和 param_file 路径
    unl_parser = UNLParser('/opt/unetlab/labs', input_file, 'node_link.json')

    # 处理指定的 .unl 文件
    unl_parser.process_unl_files()

    # 创建 ParameterOptimizer 实例并进行优化处理
    optimizer = ParameterOptimizer('node_link.json', output_file)
    optimizer.process_optimization()

