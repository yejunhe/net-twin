import xml.etree.ElementTree as ET
import os
import json
import random
import math
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

# UNL文件解析类
class UNLParser:
    def __init__(self, input_folder, param_file, output_file_name):
        self.input_folder = input_folder
        self.param_file = param_file  # 添加 param_file 参数
        self.output_file_path = os.path.join(os.getcwd(), output_file_name)
        self.tree = None
        self.root = None
        self.nodes = None
        self.networks = None
        self.output_data = {
            "nodes": [],
            "links": []
        }
        self.lab_id = self.get_lab_id_from_param()  # 读取 labId

    def get_lab_id_from_param(self):
        """从param.json文件中获取labId"""
        with open(self.param_file, 'r', encoding='utf-8') as file:
            data = json.load(file)
            return str(data["labId"])

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
        """ 收集节点信息并计算基于任务数量的资源利用率。 """
        for node in self.nodes.findall('node'):
            node_id = node.attrib.get('id')
            node_name = node.attrib.get('name')
            tasks_count = random.randint(1, 10)  # 随机生成任务数量

            # 使用指数函数计算资源利用率
            cpu_usage = round(min(100, 100 * (math.exp(0.05 * tasks_count) - 1)), 3)
            memory_usage = round(min(100, 50 * (math.exp(0.03 * tasks_count) - 1)), 3)

            self.output_data["nodes"].append({
                "node_id": node_id,
                "node_name": node_name,
                "tasks_count": tasks_count,
                "cpu_usage": cpu_usage,
                "memory_usage": memory_usage
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

    def process_unl_files(self):
        """直接使用labId作为文件名处理指定的UNL文件"""
        target_unl_file = os.path.join(self.input_folder, f"{self.lab_id}.unl")
        print(f"Processing file: {target_unl_file}")
        if self.parse_file(target_unl_file):
            self.collect_nodes()
            self.collect_links()

        # 写入收集的数据到输出文件
        self.write_output()

# 任务调度类
class Scheduler:
    def __init__(self, input_file_name, output_file_name):
        self.input_file_path = input_file_name
        self.output_file_path = output_file_name
        self.data = None
        self.load_data()

    def load_data(self):
        """从JSON文件加载节点和链接数据。"""
        with open(self.input_file_path, 'r', encoding='utf-8') as file:
            self.data = json.load(file)

    def balance_load(self):
        """根据节点的当前任务数平衡网络负载。"""
        if not self.data:
            print("未加载数据。")
            return

        # 获得所有节点按任务数排序
        nodes_by_tasks = sorted(self.data['nodes'], key=lambda x: x['tasks_count'])

        # 循环直到高负载和低负载节点的任务数相差不大于1
        transfers = []
        high = len(nodes_by_tasks) - 1
        low = 0

        while nodes_by_tasks[high]['tasks_count'] - nodes_by_tasks[low]['tasks_count'] > 1:
            # 记录调度信息
            transfer_info = {
                "from": nodes_by_tasks[high]['node_name'],
                "to": nodes_by_tasks[low]['node_name'],
                "before_transfer": {
                    "from_cpu": nodes_by_tasks[high]['cpu_usage'],
                    "from_memory": nodes_by_tasks[high]['memory_usage'],
                    "to_cpu": nodes_by_tasks[low]['cpu_usage'],
                    "to_memory": nodes_by_tasks[low]['memory_usage']
                }
            }

            # 从高负载节点移除一个任务到低负载节点
            nodes_by_tasks[high]['tasks_count'] -= 1
            nodes_by_tasks[low]['tasks_count'] += 1

            # 重新计算CPU和内存使用率
            nodes_by_tasks[high]['cpu_usage'] = nodes_by_tasks[high]['tasks_count'] * 10
            nodes_by_tasks[high]['memory_usage'] = nodes_by_tasks[high]['tasks_count'] * 5
            nodes_by_tasks[low]['cpu_usage'] = nodes_by_tasks[low]['tasks_count'] * 10
            nodes_by_tasks[low]['memory_usage'] = nodes_by_tasks[low]['tasks_count'] * 5

            # 更新调度后的资源情况
            transfer_info["after_transfer"] = {
                "from_cpu": nodes_by_tasks[high]['cpu_usage'],
                "from_memory": nodes_by_tasks[high]['memory_usage'],
                "to_cpu": nodes_by_tasks[low]['cpu_usage'],
                "to_memory": nodes_by_tasks[low]['memory_usage']
            }
            transfers.append(transfer_info)

            # 重新排序节点列表
            nodes_by_tasks = sorted(nodes_by_tasks, key=lambda x: x['tasks_count'])
            high = len(nodes_by_tasks) - 1
            low = 0

        return transfers

    def write_comparison(self, transfers):
        """将调度前后的节点指标写入文本文件进行比较。"""
        with open(self.output_file_path, 'w', encoding='utf-8') as file:
            # 写入报告标题和生成时间
            file.write("任务调度推演优化报告\n")
            file.write(f"报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            file.write("=============\n")

            # 写入调度详细信息
            for transfer in transfers:
                file.write(f"任务从节点 '{transfer['from']}' 调度到节点 '{transfer['to']}'\n")
                file.write(f"调度前:\n")
                file.write(f"    节点 '{transfer['from']}' CPU使用率: {transfer['before_transfer']['from_cpu']}%, 内存使用率: {transfer['before_transfer']['from_memory']}%\n")
                file.write(f"    节点 '{transfer['to']}' CPU使用率: {transfer['before_transfer']['to_cpu']}%, 内存使用率: {transfer['before_transfer']['to_memory']}%\n")
                file.write(f"调度后:\n")
                file.write(f"    节点 '{transfer['from']}' CPU使用率: {transfer['after_transfer']['from_cpu']}%, 内存使用率: {transfer['after_transfer']['from_memory']}%\n")
                file.write(f"    节点 '{transfer['to']}' CPU使用率: {transfer['after_transfer']['to_cpu']}%, 内存使用率: {transfer['after_transfer']['to_memory']}%\n\n")

    def process_scheduling(self):
        """处理整个调度过程，从加载、重新分配到输出结果。"""
        # 执行负载平衡
        transfers = self.balance_load()

        # 输出比较结果
        self.write_comparison(transfers)

# 主流程
if __name__ == "__main__":
    # 使用 ExperimentProcessor 类来处理输入和输出路径
    processor = ExperimentProcessor(input_base_dir="/uploadPath/reasoning", output_base_dir="/uploadPath/reasoning")
    input_file, output_file = processor.process_paths()

    # 实例化 UNLParser
    parser = UNLParser(
        input_folder='/opt/unetlab/labs',
        param_file=input_file,  # 传入 param.json 的路径
        output_file_name='node_link.json'
    )
    parser.process_unl_files()

    scheduler = Scheduler('node_link.json', output_file)
    scheduler.process_scheduling()

