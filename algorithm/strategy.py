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
    def __init__(self, input_file_name=None, output_file_name=None):
        self.input_file_path = os.path.join('/opt/unetlab/labs', input_file_name) if input_file_name else None
        self.output_file_path = os.path.join(os.getcwd(), output_file_name) if output_file_name else None
        self.tree = None
        self.root = None
        self.nodes = None
        self.networks = None
        self.output_data = {
            "nodes": [],
            "links": []
        }

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
        with open(self.output_file_path, 'w') as file:
            json.dump(self.output_data, file, indent=4)
        print(f"Output written to {self.output_file_path}")

    def process_unl_file(self):
        if self.parse_file():
            self.collect_nodes()
            self.collect_links()
            self.write_output()

    def find_unl_with_version(self, folder_path, target_version):
        target_files = []
        for root, _, files in os.walk(folder_path):
            for file in files:
                if file.endswith(".unl"):
                    file_path = os.path.join(root, file)
                    try:
                        tree = ET.parse(file_path)
                        lab = tree.getroot()
                        version = lab.attrib.get('version')
                        if version == target_version:
                            print(f"Found target file: {file_path}")
                            target_files.append(file_path)
                    except ET.ParseError as e:
                        print(f"Error parsing {file_path}: {e}")
        return target_files

    def process_folder_for_version(self, folder_path, param_json_path, output_file_name):
        # 读取 param.json 文件获取 labId
        with open(param_json_path, 'r') as file:
            param_data = json.load(file)
            lab_id = param_data['labId']

        # 根据 labId 构建目标 .unl 文件名
        target_file_name = f"{lab_id}.unl"
        target_file_path = os.path.join(folder_path, target_file_name)

        # 检查目标文件是否存在并处理它
        if os.path.exists(target_file_path):
            self.input_file_path = target_file_path
            self.output_file_path = os.path.join(os.getcwd(), output_file_name)
            self.process_unl_file()
        else:
            print(f"File {target_file_path} does not exist.")

class StrategyAdvisor:
    def __init__(self, data):
        self.data = data

    def analyze_nodes(self):
        high_resource_usage = []
        for node in self.data["nodes"]:
            if node["resource_usage"]["cpu_usage"] > 80 or node["resource_usage"]["memory_usage"] > 80:
                reason = "CPU" if node["resource_usage"]["cpu_usage"] > 80 else "memory"
                usage = node["resource_usage"]["cpu_usage"] if reason == "CPU" else node["resource_usage"]["memory_usage"]
                high_resource_usage.append((node["node_name"], reason, usage))
        return high_resource_usage

    def analyze_links(self):
        problematic_links = []
        for link in self.data["links"]:
            metrics = link["metrics"]
            if metrics["bandwidth_usage"] > 80 or metrics["latency"] > 50 or metrics["packet_loss"] > 2:
                issue = []
                if metrics["bandwidth_usage"] > 80:
                    issue.append(f"带宽使用率 {metrics['bandwidth_usage']}%")
                if metrics["latency"] > 50:
                    issue.append(f"延迟 {metrics['latency']} ms")
                if metrics["packet_loss"] > 2:
                    issue.append(f"丢包率 {metrics['packet_loss']}%")
                problematic_links.append((link["source"]["node_name"], link["target"]["node_name"], ', '.join(issue)))
        return problematic_links

    def suggest_optimizations(self):
        node_issues = self.analyze_nodes()
        link_issues = self.analyze_links()

        suggestions = [f"策略配置推演优化报告\n日期: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"]

        if node_issues:
            suggestions.append("\n以下节点资源使用率过高：")
            for node_name, reason, usage in node_issues:
                suggestions.append(f"节点 {node_name} 的 {reason} 使用率为 {usage}%。建议:")
                suggestions.append(f"#运行资源分配推演优化算法以更合理地分配网络资源。")
                suggestions.append(f"#运行路由路径推演优化算法以减轻该节点的负载。")

        if link_issues:
            suggestions.append("\n针对链路问题进行以下优化建议：")
            for src, tgt, issues in link_issues:
                suggestions.append(f"链路 {src} 到 {tgt} 存在以下问题：{issues}。建议:")
                if "带宽使用率" in issues:
                    suggestions.append("#运行流量调度推演优化算法以优化带宽使用。")
                if "延迟" in issues:
                    suggestions.append("#运行路由路径推演优化算法以降低延迟。")
                if "丢包率" in issues:
                    suggestions.append("#检查物理连接和运行任务调度推演优化算法以减少丢包。")

        return suggestions

    def run(self):
        optimizations = self.suggest_optimizations()
        self.write_recommendations_to_file(optimizations)

    def write_recommendations_to_file(self, recommendations):
        with open(output_file, 'w', encoding='utf-8') as file:
            for recommendation in recommendations:
                file.write(recommendation + '\n')

# Example usage:
if __name__ == "__main__":
    # 使用 ExperimentProcessor 类来处理输入和输出路径
    processor = ExperimentProcessor(input_base_dir="/uploadPath/reasoning", output_base_dir="/uploadPath/reasoning")
    input_file, output_file = processor.process_paths()

    # 传递 param.json 的路径，而不是版本号
    parser = UNLParser()
    parser.process_folder_for_version('/opt/unetlab/labs', input_file, 'node_link.json')

    # 从 JSON 输出文件中加载数据
    with open('node_link.json', 'r') as file:
        parsed_data = json.load(file)

    # 创建 StrategyAdvisor 实例并运行优化建议生成
    advisor = StrategyAdvisor(parsed_data)
    advisor.run()
