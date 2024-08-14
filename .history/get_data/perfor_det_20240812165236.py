import xml.etree.ElementTree as ET
import os
import json
import random
import subprocess

class NodeReader:
    def __init__(self, input_file_path, required_version):
        self.input_file_path = input_file_path
        self.required_version = required_version
        self.tree = None
        self.root = None
        self.nodes = None
        self.networks = None
        self.output_data = {
            "nodes": [],
            "links": [],
            "containers": [],
            "performance_evaluation": "",
            "evaluation_criteria": ""  # 添加性能评估标准字段
        }

    def parse_file(self):
        if not os.path.exists(self.input_file_path):
            print(f"File {self.input_file_path} does not exist.")
            return False
        
        self.tree = ET.parse(self.input_file_path)
        self.root = self.tree.getroot()

        version = self.root.attrib.get("version")
        if version != self.required_version:
            print(f"File {self.input_file_path} version {version} does not match the required version {self.required_version}. Skipping...")
            return False

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

    def get_container_ids(self):
        command = "docker ps --filter ancestor=25125/frrouting:10-dev-05221913 --format '{{.ID}}'"
        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True)
            container_ids = result.stdout.strip().split('\n')
            if container_ids and container_ids[0]:
                self.output_data["containers"] = self.get_container_info(container_ids)
            else:
                print(f"No containers found for image 25125/frrouting:10-dev-05221913")
        except subprocess.CalledProcessError as e:
            print(f"Error getting container IDs: {e}")

    def get_container_info(self, container_ids):
        containers_info = []
        for container_id in container_ids:
            try:
                command_stats = f"docker stats {container_id} --no-stream --format '{{{{json .}}}}'"
                result_stats = subprocess.run(command_stats, shell=True, capture_output=True, text=True)
                container_stats = json.loads(result_stats.stdout.strip()) if result_stats.returncode == 0 else {}

                command_conf = f"docker exec {container_id} cat /etc/frr/frr.conf"
                result_conf = subprocess.run(command_conf, shell=True, capture_output=True, text=True)
                frr_conf = result_conf.stdout.strip() if result_conf.returncode == 0 else "N/A"

                router_name = self.extract_router_name(frr_conf)

                containers_info.append({
                    "container_id": container_id,
                    "cpu_percentage": container_stats.get('CPUPerc', 'N/A'),
                    "memory_usage": container_stats.get('MemUsage', 'N/A'),
                    "network_io": container_stats.get('NetIO', 'N/A'),
                    "block_io": container_stats.get('BlockIO', 'N/A'),
                    "pids": container_stats.get('PIDs', 'N/A'),
                    "router_name": router_name,
                    "frr_conf": frr_conf
                })
            except subprocess.CalledProcessError as e:
                print(f"Error retrieving information for container {container_id}: {e}")
        return containers_info

    def extract_router_name(self, frr_conf_content):
        for line in frr_conf_content.splitlines():
            if line.startswith('hostname'):
                return line.split()[1]
        return "Unknown"

    def evaluate_performance(self):
        average_throughput = sum(node["throughput_mbps"] for node in self.output_data["nodes"]) / len(self.output_data["nodes"])
        cpu_overload = any(float(container["cpu_percentage"].rstrip('%')) > 80 for container in self.output_data["containers"] if container["cpu_percentage"] != 'N/A')

        def parse_memory_usage(memory_str):
            try:
                memory_value = float(memory_str.replace('MiB', '').strip())
                return memory_value
            except ValueError:
                return 0
        
        memory_overload = any(parse_memory_usage(container["memory_usage"].split('/')[0]) > 80 for container in self.output_data["containers"] if container["memory_usage"] != 'N/A')

        evaluation_criteria = (
            f"平均吞吐量: {average_throughput} Mbps\n"
            f"CPU 过载: {'是' if cpu_overload else '否'}\n"
            f"内存过载: {'是' if memory_overload else '否'}"
        )
        self.output_data["evaluation_criteria"] = evaluation_criteria

        if average_throughput < 300 or cpu_overload or memory_overload:
            self.output_data["performance_evaluation"] = "较差"
        elif 300 <= average_throughput < 700 and not cpu_overload and not memory_overload:
            self.output_data["performance_evaluation"] = "中等"
        else:
            self.output_data["performance_evaluation"] = "良好"

    def write_output(self, json_output_file, txt_output_file):
        self.collect_nodes()
        self.collect_links()
        self.get_container_ids()
        self.evaluate_performance()  # 调用性能评估方法
        self.organize_output_data()

        # Write configuration data to JSON file
        with open(json_output_file, 'w') as json_file:
            json.dump(self.output_data, json_file, indent=4)
        print(f"Configuration output written to {json_output_file}")

        # Write performance evaluation to TXT file in Chinese
        with open(txt_output_file, 'w') as txt_file:
            txt_file.write(f"性能评估结果: {self.output_data['performance_evaluation']}\n")
            txt_file.write(f"评估标准:\n{self.output_data['evaluation_criteria']}\n")
        print(f"Performance evaluation written to {txt_output_file}")

    def organize_output_data(self):
        for container in self.output_data["containers"]:
            container["frr_conf_preview"] = "\n".join(container["frr_conf"].splitlines()[:5]) + ("..." if len(container["frr_conf"].splitlines()) > 5 else "")

def process_unl_files(directory, json_output_file, txt_output_file, required_version):
    for file_name in os.listdir(directory):
        if file_name.endswith('.unl'):
            input_file_path = os.path.join(directory, file_name)
            print(f"Processing file: {file_name}")
            reader = NodeReader(input_file_path, required_version)
            if reader.parse_file():
                reader.write_output(json_output_file, txt_output_file)

def main():
    directory = '/opt/unetlab/labs'
    json_output_file = 'config_data.json'
    txt_output_file = 'performance_evaluation.txt'
    required_version = '2'

    process_unl_files(directory, json_output_file, txt_output_file, required_version)

if __name__ == "__main__":
    main()
