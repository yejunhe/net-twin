import subprocess
import json

def get_frr_container_ids(image_name="25125/frrouting:10-dev-05221913"):
    try:
        result = subprocess.run(['docker', 'ps', '--filter', f'ancestor={image_name}', '-q'], capture_output=True, text=True)
        container_ids = result.stdout.strip().split('\n')
        return container_ids if container_ids[0] else []
    except subprocess.CalledProcessError as e:
        print(f"Error occurred: {e}")
        return []

def get_frr_conf(container_id, file_path="/etc/frr/frr.conf"):
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

def parse_frr_conf(frr_conf):
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

def save_missing_info_to_txt(data, filename="missing_frr_conf.txt"):
    translations = {
        'bgp': 'BGP配置',
        'ospf': 'OSPF配置',
        'bgp router-id': 'BGP路由器ID',
        'ospf router-id': 'OSPF路由器ID',
        'network': '网络'
    }
    
    try:
        with open(filename, 'w', encoding='utf-8') as txt_file:
            for container_id, details in data.items():
                router_name = details.get('hostname', '未知路由器')
                txt_file.write(f"路由器 {router_name} 缺失的配置:\n")
                
                if details['missing_sections']:
                    txt_file.write("  缺失的部分:\n")
                    for section in details['missing_sections']:
                        txt_file.write(f"    {translations.get(section, section)}\n")
                
                if details['missing_parameters']:
                    for section, params in details['missing_parameters'].items():
                        txt_file.write(f"  {translations.get(section, section)} 缺少的参数:\n")
                        for param in params:
                            txt_file.write(f"    {translations.get(param, param)}\n")
                txt_file.write("\n")
                
        print(f"缺失信息成功保存到 {filename}")
    except IOError as e:
        print(f"保存文件时发生错误: {e}")

if __name__ == "__main__":
    image_name = "25125/frrouting:10-dev-05221913"  # 镜像名称和标签
    container_ids = get_frr_container_ids(image_name)
    
    frr_conf_data = {}
    
    if container_ids:
        for cid in container_ids:
            print(f"Fetching frr.conf from container ID: {cid}")
            frr_conf = get_frr_conf(cid)
            if frr_conf:
                parsed_conf = parse_frr_conf(frr_conf)
                frr_conf_data[cid] = {
                    "image_name": image_name,
                    "hostname": parsed_conf['hostname'],
                    "missing_sections": parsed_conf['missing_sections'],
                    "missing_parameters": parsed_conf['missing_parameters']
                }
            else:
                print(f"Could not retrieve frr.conf from container {cid}.")
        
        if frr_conf_data:
            save_missing_info_to_txt(frr_conf_data)
        else:
            print("No frr.conf data to save.")
    else:
        print(f"No running containers found for image '{image_name}'.")
