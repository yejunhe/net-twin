import os
import json
import telnetlib
import xml.etree.ElementTree as ET
import sys
import argparse
from datetime import datetime

class ExperimentProcessor:
    def __init__(self, input_base_dir, output_base_dir):
        self.input_base_dir = input_base_dir
        self.output_base_dir = output_base_dir

    def get_latest_folder_number(self, base_path):
        """获取指定路径下最新的文件夹编号"""
        all_dirs = [d for d in os.listdir(base_path) if os.path.isdir(os.path.join(base_path, d)) and d.isdigit()]
        if not all_dirs:
            return None
        latest_dir = max(all_dirs, key=lambda x: int(x))
        return latest_dir

    def replace_placeholder_with_latest(self, path):
        """将路径中的占位符 [t] 替换为最新的文件夹编号"""
        if '[t]' in path:
            latest_dir = self.get_latest_folder_number(self.input_base_dir)
            if latest_dir:
                path = path.replace('[t]', latest_dir)
        return path


class NetworkTopology:
    def __init__(self, unl_file):
        self.unl_file = unl_file
        self.nodes_info = {}
        self.adjacency_matrix = {}
        self.id_to_name = {}  # 添加一个字典来映射节点ID到名称

    def read_unl_file(self):
        """读取实验文件并构建节点信息和邻接矩阵"""
        tree = ET.parse(self.unl_file)
        root = tree.getroot()

        print(f"Reading UNL file: {self.unl_file}")

        for node in root.findall('.//node'):
            node_id = node.get('id')
            node_name = node.get('name')
            self.nodes_info[node_id] = {'name': node_name, 'interfaces': []}
            self.id_to_name[node_id] = node_name  # 在读取节点时填充映射

            self.adjacency_matrix[node_id] = {}

            print(f"Node added: ID={node_id}, Name={node_name}")

            for interface in node.findall('interface'):
                intf_id = interface.get('id')
                network_id = interface.get('network_id')
                self.nodes_info[node_id]['interfaces'].append({'id': intf_id, 'network_id': network_id})
                print(f"  Interface added: ID={intf_id}, Network ID={network_id}")

        self.build_adjacency_matrix()

    def build_adjacency_matrix(self):
        """构建邻接矩阵"""
        print("Building adjacency matrix...")

        # 初始化邻接矩阵，所有节点之间的连接状态初始为0（无连接）
        for node_id in self.nodes_info:
            for other_node_id in self.nodes_info:
                self.adjacency_matrix[node_id][other_node_id] = 0  # 初始为0表示无连接

        # 构建网络ID到节点的映射
        network_to_nodes = {}
        for node_id, node_data in self.nodes_info.items():
            for interface in node_data['interfaces']:
                network_id = interface['network_id']
                if network_id not in network_to_nodes:
                    network_to_nodes[network_id] = set()
                network_to_nodes[network_id].add(node_id)

        # 填充邻接矩阵
        for network_id, connected_nodes in network_to_nodes.items():
            connected_nodes = list(connected_nodes)
            print(f"Network {network_id} connects nodes: {connected_nodes}")
            for i in range(len(connected_nodes)):
                for j in range(i + 1, len(connected_nodes)):
                    node_id_1 = connected_nodes[i]
                    node_id_2 = connected_nodes[j]
                    self.adjacency_matrix[node_id_1][node_id_2] = 1  # 1表示有连接
                    self.adjacency_matrix[node_id_2][node_id_1] = 1  # 连接是对称的
                    print(
                        f"Link established: {self.id_to_name[node_id_1]} ({node_id_1}) <-> {self.id_to_name[node_id_2]} ({node_id_2})")

        # 打印完整的邻接矩阵
        print("Adjacency Matrix:")
        for node_id, edges in self.adjacency_matrix.items():
            print(f"Node {self.id_to_name[node_id]} ({node_id}): {edges}")


class TelnetConnector:
    def __init__(self, host, port):
        self.host = host
        self.port = port

    def get_sysname(self):
        """通过 Telnet 获取节点的 sysname"""
        try:
            # 增加超时时间，建立 Telnet 连接
            tn = telnetlib.Telnet(self.host, self.port, timeout=10)

            # 发送一个空命令（如回车字符 '\n'），以确保接收到提示符
            tn.write(b'\n')

            # 设置匹配可能的提示符，增加超时时间来等待返回结果
            output = tn.read_until(b'>', timeout=5)  # 等待提示符 '>'，超时时间 5 秒

            # 处理输出，假设 sysname 在提示符之间
            lines = output.decode('ascii').splitlines()
            sysname = None

            # 遍历每一行，寻找可能的 sysname
            for line in lines:
                if line.startswith('<') and line.endswith('>'):
                    sysname = line.strip('<> ')
                    break

            tn.close()
            if sysname:
                return sysname
            else:
                print(f"Could not determine sysname from output: {output}")
                return None

        except Exception as e:
            print(f"Error connecting to {self.host}:{self.port} - {e}")
            return None


class ShortestPathCalculator:
    @staticmethod
    def dijkstra(adjacency_matrix, start_node, end_node):
        """使用 Dijkstra 算法计算最短路径"""
        unvisited_nodes = list(adjacency_matrix.keys())
        shortest_path = {}
        previous_nodes = {}
        max_value = sys.maxsize

        # 初始化最短路径表
        for node in unvisited_nodes:
            shortest_path[node] = max_value
        shortest_path[start_node] = 0

        while unvisited_nodes:
            # 选择距离起始节点最近的未访问节点
            min_node = None
            for node in unvisited_nodes:
                if min_node is None:
                    min_node = node
                elif shortest_path[node] < shortest_path[min_node]:
                    min_node = node

            # 更新当前节点的邻居节点的距离
            for neighbor, cost in adjacency_matrix[min_node].items():
                if cost > 0:  # 确保只有有连接的情况下才更新（1表示有连接）
                    temp_value = shortest_path[min_node] + cost
                    if temp_value < shortest_path[neighbor]:
                        shortest_path[neighbor] = temp_value
                        previous_nodes[neighbor] = min_node

            unvisited_nodes.remove(min_node)

        # 构建最短路径
        path = []
        current_node = end_node
        while current_node != start_node:
            if current_node in previous_nodes:
                path.insert(0, current_node)
                current_node = previous_nodes[current_node]
            else:
                print("No path found")
                return None
        path.insert(0, start_node)

        # 检查是否找到了有效路径
        if shortest_path[end_node] == max_value:
            print("No path found")
            return None

        return path

class ReportGenerator:
    @staticmethod
    def write_result_to_file(path, distance, output_file, id_to_name):
        """将最短路径结果写入输出文件"""
        # 将路径中的节点ID转换为名称
        named_path = [id_to_name[node_id] for node_id in path]

        with open(output_file, 'w') as file:
            file.write("路由路径推演优化报告\n")
            file.write(f"报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            file.write("根据Dijkstra算法计算得出的最短路径如下：\n")
            file.write(f"最短路径为: {' -> '.join(named_path)}\n")
            file.write(f"该路径的总距离为: {distance}\n")
            file.write("此路径在计算过程中，已考虑了所有节点之间的最短距离与链路的状态信息，确保了路径的最优性。\n\n")


def main():
    # 使用 argparse 处理命令行参数
    parser = argparse.ArgumentParser(description="Process input and output paths.")
    parser.add_argument('-i', '--input', type=str, required=True, help='Input file path')
    parser.add_argument('-o', '--output', type=str, required=True, help='Output file path')

    args = parser.parse_args()

    # 初始化处理器
    processor = ExperimentProcessor(input_base_dir="/uploadPath/reasoning/", output_base_dir="/uploadPath/reasoning/")

    # 替换路径中的占位符 [t] 为最新的文件夹编号
    input_file = processor.replace_placeholder_with_latest(args.input)
    output_file = processor.replace_placeholder_with_latest(args.output)

    print(f"Resolved input file path: {input_file}")
    print(f"Resolved output file path: {output_file}")

    # 读取参数文件
    lab_id, nodes = read_param_file(input_file)

    # 读取实验文件
    unl_file = f'/opt/unetlab/labs/{lab_id}.unl'
    topology = NetworkTopology(unl_file)
    topology.read_unl_file()

    # Telnet连接到两个节点并获取sysname
    node1_info = nodes[0]
    node2_info = nodes[1]
    telnet1 = TelnetConnector(node1_info['hostip'], node1_info['port'])
    telnet2 = TelnetConnector(node2_info['hostip'], node2_info['port'])
    sysname1 = telnet1.get_sysname()
    sysname2 = telnet2.get_sysname()

    print(f"Sysname for Node 1: {sysname1}, Node 2: {sysname2}")

    # 建立 sysname 与 node_id 的映射
    sysname_to_node_id = {v['name']: k for k, v in topology.nodes_info.items()}

    # 确保 sysname 匹配到 node_id
    if sysname1 not in sysname_to_node_id or sysname2 not in sysname_to_node_id:
        print("Sysname does not match any node in the topology.")
        return

    start_node = sysname_to_node_id[sysname1]
    end_node = sysname_to_node_id[sysname2]

    # 查找最短路径
    shortest_path = ShortestPathCalculator.dijkstra(topology.adjacency_matrix, start_node, end_node)

    if shortest_path:
        distance = len(shortest_path) - 1  # 假设每条链路的权重为1
        ReportGenerator.write_result_to_file(shortest_path, distance, output_file, topology.id_to_name)
        print(f"Shortest path: {shortest_path} with distance: {distance}")
    else:
        print("No valid path found.")

def read_param_file(param_file):
    with open(param_file, 'r') as file:
        params = json.load(file)
    lab_id = params['labId']
    nodes = params['node']
    return lab_id, nodes


if __name__ == "__main__":
    main()
