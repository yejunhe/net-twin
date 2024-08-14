import xml.etree.ElementTree as ET
import subprocess

def get_unl_ids(unl_file):
    """
    解析 .unl 文件，提取所有节点的 id。
    """
    tree = ET.parse(unl_file)
    root = tree.getroot()
    
    ids = []
    for node in root.findall(".//nodes/node"):
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
    将 .unl 文件中的 id 与容器 NAMES 进行匹配，输出匹配到的容器 ID。
    """
    for unl_id in unl_ids:
        if unl_id in containers:
            print(f"Match found: {unl_id} -> Container ID: {containers[unl_id]}")
        else:
            print(f"No match found for UNL ID: {unl_id}")

if __name__ == "__main__":
    unl_file_path = "/opt/unetlab/labs/9.unl"  # 将路径替换为实际的 .unl 文件路径
    unl_ids = get_unl_ids(unl_file_path)
    
    if not unl_ids:
        print("No IDs found in the .unl file.")
    else:
        running_containers = get_running_containers()
        match_ids_with_containers(unl_ids, running_containers)
