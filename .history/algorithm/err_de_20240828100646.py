import os
import json
import xml.etree.ElementTree as ET
import subprocess
import argparse

class FrrContainerManager:
    def __init__(self):
        self.frr_conf_data = {}


    def get_running_containers(self):
        result = subprocess.run(['docker', 'ps', '--format', '{{.ID}} {{.Names}}'], capture_output=True, text=True)
        containers = {}
        for line in result.stdout.strip().splitlines():
            container_id, container_name = line.split(maxsplit=1)
            containers[container_name] = container_id
        return containers
  
    def match_ids_with_containers(self, lab_id, containers):
        matched_containers = []
        for container_name, container_id in containers.items():
            if lab_id in container_name:
                matched_containers.append(container_id)
        return matched_containers

    def get_frr_conf(self, container_id, file_path="/etc/frr/frr.conf"):
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
            'missing_sections': []
        }
    
        has_bgp = False
        has_ospf = False
        has_static_route = False

        lines = frr_conf.splitlines()

        for line in lines:
            line = line.strip()

            if line.startswith('hostname'):
                data['hostname'] = line.split()[1]

            elif line.startswith('router bgp'):
                has_bgp = True

            elif line.startswith('router ospf'):
                has_ospf = True

            elif line.startswith('ip route'):
                has_static_route = True

    # 检查是否缺少BGP、OSPF或静态路由配置
        if not has_bgp:
            data['missing_sections'].append('bgp')
        if not has_ospf:
            data['missing_sections'].append('ospf')
        if not has_static_route:
            data['missing_sections'].append('static route')

        return data

    def save_missing_info_to_txt(self, data, output_file):
        translations = {
            'bgp': 'BGP配置',
            'ospf': 'OSPF配置',
            'static route': '静态路由配置'
        }

        try:
            with open(output_file, 'w', encoding='utf-8') as txt_file:
                for container_id, details in data.items():
                    router_name = details.get('hostname', '未知路由器')

                    if details['missing_sections']:
                        for section in details['missing_sections']:
                            txt_file.write(f"{router_name} 路由器缺少 {translations.get(section, section)}\n")

                print(f"缺失信息成功保存到 {output_file}")
            return True
        except IOError as e:
            print(f"保存文件时发生错误: {e}")
            return False

    def process_containers(self, processor: 'ExperimentProcessor'):
        lab_id = processor.get_lab_id_from_param_json()
        if not lab_id:
            print("No labId found in param.json.")
            return

        unl_file_path = processor.get_unl_file_path(lab_id)
        if not os.path.exists(unl_file_path):
            print(f".unl file {unl_file_path} not found.")
            return

        lab_id_from_unl = processor.get_lab_id_from_unl(unl_file_path)
        if not lab_id_from_unl:
            print("No lab id found in the .unl file.")
            return

        running_containers = self.get_running_containers()
        matched_container_ids = self.match_ids_with_containers(lab_id_from_unl, running_containers)

        if matched_container_ids:
            for cid in matched_container_ids:
                print(f"Fetching frr.conf from container ID: {cid}")
                frr_conf = self.get_frr_conf(cid)
                if frr_conf:
                    parsed_conf = self.parse_frr_conf(frr_conf)
                    self.frr_conf_data[cid] = {
            
                        "hostname": parsed_conf['hostname'],
                        "missing_sections": parsed_conf['missing_sections'],
                        "missing_parameters": parsed_conf['missing_parameters']
                    }
                else:
                    print(f"Could not retrieve frr.conf from container {cid}.")

            if self.frr_conf_data:
                self.save_missing_info_to_txt(self.frr_conf_data, processor.output_file)
            else:
                print("No frr.conf data to save.")
        else:
            print(f"No running containers found for image ")


class ExperimentProcessor:
    def __init__(self, input_base_dir=None, output_base_dir=None):
        self.input_base_dir = input_base_dir or '/uploadPath/reasoning'
        self.output_base_dir = output_base_dir or '/uploadPath/reasoning'
        self.input_path = None
        self.output_file = None

    def set_paths(self, input_path=None, output_path=None):
        if input_path:
            self.input_path = input_path
        else:
            self.input_path, _ = self.process_paths()

        if output_path:
            self.output_file = output_path
        else:
            _, self.output_file = self.process_paths()

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
        with open(self.input_path, 'r') as file:
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
    parser = argparse.ArgumentParser(description="Process FRR containers and save missing configuration info.")
    parser.add_argument('-i', '--input', type=str, help='Input file path (param.json)')
    parser.add_argument('-o', '--output', type=str, help='Output file path (data.txt)')
    
    args = parser.parse_args()
    
    processor = ExperimentProcessor()

    # Set paths based on provided arguments or defaults
    processor.set_paths(args.input, args.output)
    
    manager = FrrContainerManager()
    manager.process_containers(processor)
