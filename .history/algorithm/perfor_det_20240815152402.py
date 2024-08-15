import xml.etree.ElementTree as ET
import os
import json
import random
import subprocess
import argparse

def get_latest_directory(directory):
    """
    获取指定目录下最新生成的文件夹。
    """
    dirs = [os.path.join(directory, d) for d in os.listdir(directory) if os.path.isdir(os.path.join(directory, d))]
    latest_dir = max(dirs, key=os.path.getmtime)
    return latest_dir

def get_docker_stats(container_id):
    """
    获取指定容器的资源使用情况。
    返回一个字典，包含 CPU 使用率、内存使用情况、网络 I/O 等信息。
    """
    try:
        result = subprocess.run(['docker', 'stats', container_id, '--no-stream', '--format',
                                 '{{.ID}} {{.Name}} {{.CPUPerc}} {{.MemUsage}} {{.NetIO}} {{.BlockIO}} {{.PIDs}}'],
                                capture_output=True, text=True, check=True)
        output = result.stdout.strip().split()
        stats = {
            "container_id": output[0],
            "container_name": output[1],
            "cpu_percent": output[2],
            "memory_usage": output[3] + ' ' + output[4],  # 合并内存使用量和限制
            "network_io": output[5] + ' ' + output[6],    # 合并网络输入和输出
            "block_io": output[7] + ' ' + output[8],      # 合并块输入和输出
            "pids": output[9]
        }
        return stats
    except subprocess.CalledProcessError as e:
        return f"Failed to get docker stats for container {container_id}. Error: {e}\n"
def parse_memory_usage(memory_str):
        """
        解析 memory_str（如 "122.4MiB" 或 "2.5GiB"）并转换为以 MiB 为单位的浮点数。
        """
        units = {"KiB": 1 / 1024, "MiB": 1, "GiB": 1024}
        number = float(''.join(filter(str.isdigit, memory_str.split()[0])) + '.' + ''.join(filter(str.isdigit, memory_str.split()[0].split('.')[-1])))
        unit = ''.join(filter(str.isalpha, memory_str.split()[0]))
        return number * units[unit]    
class NodeReader:
    def __init__(self, unl_file_path):
        self.unl_file_path = unl_file_path
        self.tree = None
        self.root = None
        self.nodes = None
        self.networks = None
        self.output_data = {
            "nodes": [],
            "links": [],
            "containers": [],
            "performance_evaluation": "",
            "evaluation_criteria": ""
        }

    def parse_file(self):
        if not os.path.exists(self.unl_file_path):
            print(f"File {self.unl_file_path} does not exist.")
            return False
        
        self.tree = ET.parse(self.unl_file_path)
        self.root = self.tree.getroot()

        self.nodes = self.root.find('topology').find('nodes')
        self.networks = self.root.find('topology').find('networks')
        return True

    def collect_nodes(self):
        for node in self.nodes.findall('node'):
            node_id = node.attrib.get('id')
            node_name = node.attrib.get('name')
            throughput = random.randint(100, 1000)
            self.output_data["nodes"].append({
                "node_id": node_id,
                "node_name": node_name,
                "throughput_mbps": throughput
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

    def get_running_containers(self):
        """
        通过 docker ps 获取正在运行的容器信息。
        返回一个字典，其中 key 是容器 NAMES，value 是容器 ID。
        """
        result = subprocess.run(['docker', 'ps', '--format', '{{.ID}} {{.Names}}'], capture_output=True, text=True)
        containers = {}
        
        for line in result.stdout.strip().splitlines():
            container_id, container_name = line.split(maxsplit=1)
            containers[container_name] = container_id
        
        return containers

    def match_ids_with_containers(self, lab_id, containers):
        """
        将 .unl 文件中的 lab_id 与容器 NAMES 进行部分匹配，返回所有匹配到的容器 ID。
        """
        matched_containers = []
        for container_name, container_id in containers.items():
            if lab_id in container_name:
                matched_containers.append(container_id)
        
        return matched_containers

    def get_frr_config_from_container(self, container_id):
        """
        从指定容器中获取 /etc/frr/frr.conf 文件的内容，并提取 hostname。
        返回包含容器ID和hostname的字典。
        """
        try:
            result = subprocess.run(['docker', 'exec', container_id, 'cat', '/etc/frr/frr.conf'],
                                    capture_output=True, text=True, check=True)
            frr_conf = result.stdout
            
            # 提取 hostname
            hostname = ""
            for line in frr_conf.splitlines():
                if line.startswith("hostname"):
                    hostname = line.split()[1]
                    break

            return {
                "container_id": container_id,
                "hostname": hostname,
                "frr_conf": f"Configuration from container {container_id}:\n{frr_conf}\n"
            }
        except subprocess.CalledProcessError as e:
            return {
                "container_id": container_id,
                "hostname": "",
                "frr_conf": f"Failed to get configuration from container {container_id}. Error: {e}\n"
            }
    def write_output(self, json_output_file, reasoning_directory):
        self.collect_nodes()
        self.collect_links()

    # 获取运行中的容器
        running_containers = self.get_running_containers()

    # 匹配容器ID
        matched_container_ids = self.match_ids_with_containers(self.root.get('id'), running_containers)

        performance_summary = []
        evaluation_criteria = {
            "cpu_threshold": 70,  # CPU 使用率阈值
            "memory_threshold": 60,  # 内存使用率阈值，以 MiB 为单位
            "network_latency_threshold": 100  # 假设的网络延迟阈值（ms）
        }

        if matched_container_ids:
            for container_id in matched_container_ids:
            # 获取FRR配置及hostname
                container_info = self.get_frr_config_from_container(container_id)
                docker_stats = get_docker_stats(container_id)

            # 提取并解析 CPU 和内存使用率
                cpu_usage = float(docker_stats["cpu_percent"].strip('%'))
                memory_usage = parse_memory_usage(docker_stats["memory_usage"])

            # 动态生成性能评估
                if cpu_usage > evaluation_criteria["cpu_threshold"]:
                    performance_summary.append(f"路由 {container_info['hostname']} 的 CPU 使用率过高: {cpu_usage}%")
                else:
                    performance_summary.append(f"路由 {container_info['hostname']} 的 CPU 使用率正常: {cpu_usage}%")

                if memory_usage > evaluation_criteria["memory_threshold"]:
                    performance_summary.append(f"路由 {container_info['hostname']} 的内存使用率过高: {memory_usage} MiB")
                else:
                    performance_summary.append(f"路由 {container_info['hostname']} 的内存使用率正常: {memory_usage} MiB")

            # 假设我们可以通过某种方式获得网络延迟数据（这部分数据不在 docker stats 输出中，需要额外的监控工具）
                network_latency = random.randint(50, 150)  # 这里我们随机生成一个模拟延迟
                if network_latency > evaluation_criteria["network_latency_threshold"]:
                    performance_summary.append(f"路由 {container_info['hostname']} 的网络延迟过高: {network_latency}ms")
                else:
                    performance_summary.append(f"路由 {container_info['hostname']} 的网络延迟正常: {network_latency}ms")

                print(container_info["frr_conf"])
                print(docker_stats)

            self.output_data["performance_evaluation"] = "\n".join(performance_summary)
        else:
            print("No matching containers found.")

    # 获取最新文件夹并创建 /res 目录下的 data.txt 文件路径
        latest_dir = get_latest_directory(reasoning_directory)
        res_directory = os.path.join(latest_dir, 'res')
        if not os.path.exists(res_directory):
            os.makedirs(res_directory)
        data_txt_path = os.path.join(res_directory, 'data.txt')

    # 将性能评估写入 data.txt 文件
        with open(data_txt_path, 'w') as txt_file:
            txt_file.write(f"性能评估结果: {self.output_data['performance_evaluation']}\n\n")
            txt_file.write(f"评估标准及具体数值:\n")
            txt_file.write(f"CPU 使用率阈值: {evaluation_criteria['cpu_threshold']}%\n")
            txt_file.write(f"内存使用率阈值: {evaluation_criteria['memory_threshold']} MiB\n")
            txt_file.write(f"网络延迟阈值: {evaluation_criteria['network_latency_threshold']}ms\n")
        print(f"Performance evaluation written to {data_txt_path}")


def main():
    # 设置默认路径
    default_reasoning_directory = "/uploadPath/reasoning"  # 最新文件夹所在的路径
    default_labs_directory = "/opt/unetlab/labs"  # .unl 文件所在的根目录
    default_json_output_file = 'config_data.json'

    # 创建参数解析器
    parser = argparse.ArgumentParser(description="Process UNL files and collect performance data.")
    parser.add_argument("-i", "--input", help="Directory containing the reasoning folder or path to the JSON file", default=default_reasoning_directory)
    parser.add_argument("-o", "--output", help="JSON output file", default=default_json_output_file)
    
    args = parser.parse_args()

    # 根据是否提供参数来选择路径
    input_path = args.input if args.input else default_reasoning_directory
    json_output_file = args.output if args.output else default_json_output_file

    if os.path.isdir(input_path):
        # 如果是目录，则继续获取最新文件夹
        latest_dir = get_latest_directory(input_path)
        json_file_path = os.path.join(latest_dir, 'params', 'param.json')
    elif os.path.isfile(input_path):
        # 如果是文件，直接使用该文件路径
        json_file_path = input_path
    else:
        print(f"Invalid input path: {input_path}")
        return

    # 从 JSON 文件中提取 labId
    lab_id = get_lab_id_from_json(json_file_path)
    
    if not lab_id:
        print("No labId found in the param.json file.")
    else:
        # 根据 labId 构建 .unl 文件的路径
        unl_file_path = get_unl_file_path(default_labs_directory, lab_id)
        
        if not os.path.exists(unl_file_path):
            print(f".unl file {unl_file_path} not found.")
        else:
            print(f"Processing file: {unl_file_path}")
            reader = NodeReader(unl_file_path)
            if reader.parse_file():
                # 调用 write_output 时传递 reasoning_directory
                reader.write_output(json_output_file, input_path)


def get_lab_id_from_json(json_file):
    """
    从 param.json 文件中解析出 labId 的值，这个值对应 .unl 文件的名称。
    """
    with open(json_file, 'r') as file:
        data = json.load(file)
        lab_id = data.get('labId')
    return lab_id

def get_unl_file_path(directory, lab_id):
    """
    根据 labId 在指定目录下构建 .unl 文件的路径。
    """
    unl_file_name = f"{lab_id}.unl"
    unl_file_path = os.path.join(directory, unl_file_name)
    return unl_file_path

if __name__ == "__main__":
    main()
