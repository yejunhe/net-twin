import xml.etree.ElementTree as ET
import subprocess

def get_unl_ids(unl_file):
    """
    解析 .unl 文件，提取所有节点的 id。
    """
    tree = ET.parse(unl_file)
    root = tree.getroot()
    
    ids = []
    for node in root.findall(".//node"):
        node_id = node.get('id')
        if node_id:
            ids.append(node_id)
    
    return ids

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

def match_ids_with_containers(unl_ids, containers):
    """
    将 .unl 文件中的 id 与容器 NAMES 进行部分匹配，返回所有匹配到的容器 ID。
    """
    matched_containers = []
    for unl_id in unl_ids:
        for container_name, container_id in containers.items():
            if container_name.startswith(unl_id):
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
    unl_file_path = "/opt/unetlab/labs/9.unl"  # 将路径替换为实际的 .unl 文件路径
    output_file_path = "frr_configs.txt"  # 输出文件路径
    
    unl_ids = get_unl_ids(unl_file_path)
    
    if not unl_ids:
        print("No IDs found in the .unl file.")
    else:
        running_containers = get_running_containers()
        matched_container_ids = match_ids_with_containers(unl_ids, running_containers)
        
        if matched_container_ids:
            with open(output_file_path, 'w') as output_file:
                for container_id in matched_container_ids:
                    config = get_frr_config_from_container(container_id)
                    output_file.write(config)
                    print(config)  # 也可以同时输出到控制台
            print(f"Configuration information written to {output_file_path}")
        else:
            print("No matching containers found.")
