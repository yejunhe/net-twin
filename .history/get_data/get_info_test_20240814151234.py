import os
import json
import xml.etree.ElementTree as ET
import subprocess

def get_latest_directory(directory):
    dirs = [os.path.join(directory, d) for d in os.listdir(directory) if os.path.isdir(os.path.join(directory, d))]
    latest_dir = max(dirs, key=os.path.getmtime)
    return latest_dir

def get_lab_id_from_json(json_file):
    with open(json_file, 'r') as file:
        data = json.load(file)
        lab_id = data.get('labId')
    return lab_id

def get_unl_file_path(directory, lab_id):
    unl_file_name = f"{lab_id}.unl"
    unl_file_path = os.path.join(directory, unl_file_name)
    return unl_file_path

def get_lab_id_from_unl(unl_file):
    tree = ET.parse(unl_file)
    root = tree.getroot()
    lab_id = root.get('id')
    return lab_id

def get_running_containers():
    result = subprocess.run(['docker', 'ps', '--format', '{{.ID}} {{.Names}}'], capture_output=True, text=True)
    containers = {}
    
    for line in result.stdout.strip().splitlines():
        container_id, container_name = line.split(maxsplit=1)
        containers[container_name] = container_id
    
    return containers

def match_ids_with_containers(lab_id, containers):
    matched_containers = []
    for container_name, container_id in containers.items():
        if lab_id in container_name:
            matched_containers.append(container_id)
    
    return matched_containers

def get_frr_config_from_container(container_id):
    try:
        result = subprocess.run(['docker', 'exec', container_id, 'cat', '/etc/frr/frr.conf'],
                                capture_output=True, text=True, check=True)
        return result.stdout
    except subprocess.CalledProcessError as e:
        return f"Failed to get configuration from container {container_id}. Error: {e}"

def parse_frr_config(config_content):
    """
    解析 FRR 配置文件，提取 hostname、接口配置和路由配置信息。
    """
    lines = config_content.strip().splitlines()
    hostname = None
    interfaces = []
    router_configs = []
    
    current_section = None
    
    for line in lines:
        line = line.strip()
        if line.startswith("hostname"):
            hostname = line.split(maxsplit=1)[1]
        elif line.startswith("interface"):
            current_section = "interface"
            interfaces.append(line)
        elif line.startswith("router"):
            current_section = "router"
            router_configs.append(line)
        elif current_section == "interface":
            if line == "!":
                current_section = None
            else:
                interfaces.append(line)
        elif current_section == "router":
            if line == "!":
                current_section = None
            else:
                router_configs.append(line)
    
    return hostname, "\n".join(interfaces), "\n".join(router_configs)

def format_output(container_id, hostname, interfaces, router_configs):
    """
    格式化输出，包含容器 ID、hostname、接口配置和路由配置信息。
    """
    formatted_output = []
    formatted_output.append("="*60)
    formatted_output.append(f"Container ID: {container_id}")
    formatted_output.append(f"Hostname: {hostname}")
    formatted_output.append("="*60)
    formatted_output.append("Interface Configuration:")
    formatted_output.append(interfaces)
    formatted_output.append("\nRouter Configuration:")
    formatted_output.append(router_configs)
    formatted_output.append("\n")  # 添加空行分隔不同容器的配置信息
    return "\n".join(formatted_output)

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
                            config_content = get_frr_config_from_container(container_id)
                            hostname, interfaces, router_configs = parse_frr_config(config_content)
                            formatted_output = format_output(container_id, hostname, interfaces, router_configs)
                            output_file.write(formatted_output)
                            print(formatted_output)  # 也可以同时输出到控制台
                    print(f"Configuration information written to {output_file_path}")
                else:
                    print("No matching containers found.")
