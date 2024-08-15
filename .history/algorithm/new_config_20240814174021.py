import os
import json
import xml.etree.ElementTree as ET
import subprocess


class FrrContainerManager:
    def __init__(self, image_name="25125/frrouting:10-dev-05221913"):
        self.image_name = image_name
        self.frr_conf_data = {}

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

    def get_frr_conf(self, container_id, file_path="/etc/frr/frr.conf"):
        """
        从指定容器中获取 /etc/frr/frr.conf 文件的内容，并返回。
        """
        try:
            result = subprocess.run(['docker', 'exec', container_id, 'cat', file_path], capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout
            else:
                print(f"Error reading file: {result.stderr}")
                return None
        except subprocess.CalledProcessError as e:
            print(f"Error occurred: {e}")
            return None

    def parse_frr_conf(self, frr_conf):
        data = {
            'hostname': None,
            'missing_sections': [],
            'missing_parameters': {}
        }
        lines = frr_conf.splitlines()
        current_section = None

        for line in lines:
            line = line.strip()

            if line.startswith('hostname'):
                data['hostname'] = line.split()[1]

            elif line.startswith('router '):
                current_section = line.split()[1]
                if current_section not in data['missing_parameters']:
                    data['missing_parameters'][current_section] = []

            elif current_section and line and not line.startswith('!'):
                if 'bgp' in current_section and 'bgp router-id' not in line:
                    data['missing_parameters']['bgp'].append('bgp router-id')
                if 'ospf' in current_section and 'ospf router-id' not in line and 'network' not in line:
                    data['missing_parameters']['ospf'] = ['ospf router-id', 'network']

            elif line == '!':
                current_section = None

        # Define required sections
        required_sections = ['bgp', 'ospf']
        for section in required_sections:
            if section not in data['missing_parameters']:
                data['missing_sections'].append(section)

        return data

    def save_missing_info_to_txt(self, data, processor: 'ExperimentProcessor'):
        translations = {
            'bgp': 'BGP配置',
            'ospf': 'OSPF配置',
            'bgp router-id': 'BGP路由器ID',
            'ospf router-id': 'OSPF路由器ID',
            'network': '网络'
        }

        input_file, output_file = processor.process_paths()

        try:
            with open(output_file, 'w', encoding='utf-8') as txt_file:
                for container_id, details in data.items():
                    router_name = details.get('hostname', '未知路由器')

                    if details['missing_sections']:
                        for section in details['missing_sections']:
                            txt_file.write(f"{router_name} 路由器缺少 {translations.get(section, section)} 配置信息\n")

                    if details['missing_parameters']:
                        for section, params in details['missing_parameters'].items():
                            for param in params:
                                txt_file.write(f"{router_name} 路由器缺少 {translations.get(param, param)} 配置信息\n")
                    txt_file.write("\n")

            print(f"缺失信息成功保存到 {output_file}")
        except IOError as e:
            print(f"保存文件时发生错误: {e}")

    def process_containers(self, processor: 'ExperimentProcessor'):
        # Get labId from param.json
        lab_id = processor.get_lab_id_from_param_json()
        if not lab_id:
            print("No labId found in param.json.")
            return

        # Get .unl file path
        unl_file_path = processor.get_unl_file_path(lab_id)
        if not os.path.exists(unl_file_path):
            print(f".unl file {unl_file_path} not found.")
            return

        # Get labId from .unl file
        lab_id_from_unl = processor.get_lab_id_from_unl(unl_file_path)
        if not lab_id_from_unl:
            print("No lab id found in the .unl file.")
            return

        # Get running containers and match them with lab_id
        running_containers = self.get_running_containers()
        matched_container_ids = self.match_ids_with_containers(lab_id_from_unl, running_containers)

        if matched_container_ids:
            for cid in matched_container_ids:
                print(f"Fetching frr.conf from container ID: {cid}")
                frr_conf = self.get_frr_conf(cid)
                if frr_conf:
                    parsed_conf = self.parse_frr_conf(frr_conf)
                    self.frr_conf_data[cid] = {
                        "image_name": self.image_name,
                        "hostname": parsed_conf['hostname'],
                        "missing_sections": parsed_conf['missing_sections'],
                        "missing_parameters": parsed_conf['missing_parameters']
                    }
                else:
                    print(f"Could not retrieve frr.conf from container {cid}.")

            if self.frr_conf_data:
                self.save_missing_info_to_txt(self.frr_conf_data, processor)
            else:
                print("No frr.conf data to save.")
        else:
            print(f"No running containers found for image '{self.image_name}'.")


class ExperimentProcessor:
    def __init__(self, input_base_dir, output_base_dir):
        self.input_base_dir = input_base_dir
        self.output_base_dir = output_base_dir

    def find_latest_folder(self, base_dir):
        folders = [f for f in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, f)) and f.isdigit()]
        if not folders:
            raise Exception(f"No folders found in {base_dir}")
        latest_folder = max(folders, key=int)
        return os.path.join(base_dir, latest_folder), latest_folder

    def process_paths(self):
        input_folder_path, folder_name = self.find_latest_folder(self.input_base_dir)
        output_folder_path = os.path.join(self.output_base_dir, folder_name)
        input_file = os.path.join(input_folder_path, 'params', 'param.json')
        output_folder = os.path.join(output_folder_path, 'res')
        if not os.path.exists(output_folder):
            os.makedirs(output_folder)
        output_file = os.path.join(output_folder, 'data.txt')
        return input_file, output_file

    def get_lab_id_from_param_json(self):
        latest_dir, _ = self.find_latest_folder(self.input_base_dir)
        json_file_path = os.path.join(latest_dir, 'params', 'param.json')
        with open(json_file_path, 'r') as file:
            data = json.load(file)
            lab_id = data.get('labId')
        return lab_id

    def get_unl_file_path(self, lab_id):
        unl_file_name = f"{lab_id}.unl"
        unl_file_path = os.path.join('/opt/unetlab/labs', unl_file_name)
        return unl_file_path

    def get_lab_id_from_unl(self, unl_file):
        tree = ET.parse(unl_file)
        root = tree.getroot()
        return root.get('id')


if __name__ == "__main__":
    processor = ExperimentProcessor('/uploadPath/reasoning', '/uploadPath/reasoning')
    manager = FrrContainerManager()
    manager.process_containers(processor)
