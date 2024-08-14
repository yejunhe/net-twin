import os
import json
import xml.etree.ElementTree as ET
import subprocess

def get_latest_directory(directory):
    """
    获取指定目录下最新生成的文件夹。
    """
    dirs = [os.path.join(directory, d) for d in os.listdir(directory) if os.path.isdir(os.path.join(directory, d))]
    latest_dir = max(dirs, key=os.path.getmtime)
    return latest_dir

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

def get_lab_id_from_unl(unl_file):
    """
    解析 .unl 文件，提取 <lab> 元素的 id。
    """
    tree = ET.parse(unl_file)
    root = tree.getroot()
    lab_id = root.get('id')
    return lab_id

def get_running_containers():
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

def match_ids_with_containers(lab_id, containers):
    """
    将 .unl 文件中的 lab_id 与容器 NAMES 进行部分匹配，返回所有匹配到的容器 ID。
    """
    matched_containers = []
    for container_name, container_id in containers.items():
        if lab_id in container_name:
            matched_containers.append(container_id)
    
    return matched_containers

def get_frr_config_from_container(container_id):
    """
    从指定容器中获取 /etc/frr/frr.conf 文件的内容，并返回。
    """
    try:
        result = subprocess.run(['docker', 'exec', container_id, 'cat', '/etc/frr/frr.conf'],
                                capture_output=True, text=True, check=True)
        return f"Configuration from container {container_id}:\n{result.stdout}\n"
    except subprocess.CalledProcessError as e:
        return f"Failed to get configuration from container {container_id}. Error: {e}\n"

if __name__ == "__main__":
    reasoning_directory = "/uploadPath/reasoning"  # 最新文件夹所在的路径
    labs_directory = "/opt/unetlab/labs"  # .unl 文件所在的根目录
    output_file_path = "frr_configs.txt"  # 输出文件路径
    
    # 获取最新文件夹并解析出其中的 param.json 文件路径
    latest_dir = get_latest_directory(reasoning_directory)
    json_file_path = os.path.join(latest_dir, 'params', 'param.json')
    
    # 从 JSON 文件中提取 labId
    lab_id = get_lab_id_from_json(json_file_path)
    
    if not lab_id:
        print("No labId found in the param.json file.")
    else:
        # 根据 labId 构建 .unl 文件的路径
        unl_file_path = get_unl_file_path(labs_directory, lab_id)
        
        if not os.path.exists(unl_file_path):
            print(f".unl file {unl_file_path} not found.")
        else:
            # 获取 unl 文件中的 lab id
            lab_id_from_unl = get_lab_id_from_unl(unl_file_path)
            
            if not lab_id_from_unl:
                print("No lab id found in the .unl file.")
            else:
                running_containers = get_running_containers()
                matched_container_ids = match_ids_with_containers(lab_id_from_unl, running_containers)
                
                if matched_container_ids:
                    with open(output_file_path, 'w') as output_file:
                        for container_id in matched_container_ids:
                            config = get_frr_config_from_container(container_id)
                            output_file.write(config)
                            print(config)  # 也可以同时输出到控制台
                    print(f"Configuration information written to {output_file_path}")
                else:
                    print("No matching containers found.")
