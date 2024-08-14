import subprocess
import json
import re

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
        'interfaces': {},
        'router_config': {},
        'missing_sections': [],
        'missing_parameters': {}
    }
    lines = frr_conf.splitlines()
    current_section = None

    for line in lines:
        line = line.strip()
        
        if line.startswith('hostname'):
            data['hostname'] = line.split()[1]
        
        elif line.startswith('interface'):
            current_interface = line.split()[1]
            data['interfaces'][current_interface] = {}
        
        elif line.startswith('ip address') and 'current_interface' in locals():
            ip_address = line.split()[2]
            data['interfaces'][current_interface]['ip_address'] = ip_address
        
        elif line.startswith('router '):
            current_section = line.split()[1]
            data['router_config'][current_section] = []
        
        elif current_section and line and not line.startswith('!'):
            data['router_config'][current_section].append(line)
        
        elif line == '!':
            current_section = None
    
    # Define required sections and parameters
    required_sections = ['bgp', 'ospf']
    for section in required_sections:
        if section not in data['router_config']:
            data['missing_sections'].append(section)
        else:
            # Check for required parameters within each section
            required_parameters = {
                'bgp': ['bgp router-id'],
                'ospf': ['ospf router-id', 'network']
            }
            for param in required_parameters.get(section, []):
                if not any(param in line for line in data['router_config'][section]):
                    data['missing_parameters'].setdefault(section, []).append(param)

    return data

def save_summary_to_txt(data, filename="frr_conf_summary.txt"):
    try:
        with open(filename, 'w') as txt_file:
            for container_id, details in data.items():
                txt_file.write(f"Container ID: {container_id}\n")
                txt_file.write(f"Image Name: {details['image_name']}\n")
                txt_file.write(f"Hostname: {details['frr_conf'].get('hostname', 'N/A')}\n")
                
                if details['frr_conf']['interfaces']:
                    txt_file.write("Interfaces:\n")
                    for iface, info in details['frr_conf']['interfaces'].items():
                        txt_file.write(f"  Interface {iface}: {info}\n")
                else:
                    txt_file.write("No interface configurations found.\n")
                
                if details['frr_conf']['router_config']:
                    txt_file.write("Router Configurations:\n")
                    for section, params in details['frr_conf']['router_config'].items():
                        txt_file.write(f"  {section}:\n")
                        for param in params:
                            txt_file.write(f"    {param}\n")
                else:
                    txt_file.write("No router configurations found.\n")

                if details['frr_conf']['missing_sections']:
                    txt_file.write("Missing Sections:\n")
                    for section in details['frr_conf']['missing_sections']:
                        txt_file.write(f"  {section}\n")
                else:
                    txt_file.write("No missing sections.\n")

                if details['frr_conf']['missing_parameters']:
                    txt_file.write("Missing Parameters:\n")
                    for section, params in details['frr_conf']['missing_parameters'].items():
                        txt_file.write(f"  {section}:\n")
                        for param in params:
                            txt_file.write(f"    {param}\n")
                else:
                    txt_file.write("No missing parameters.\n")
                
                txt_file.write("\n")
        print(f"Summary successfully saved to {filename}")
    except IOError as e:
        print(f"Error saving summary to file: {e}")

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
                    "frr_conf": parsed_conf
                }
            else:
                print(f"Could not retrieve frr.conf from container {cid}.")
        
        if frr_conf_data:
            save_summary_to_txt(frr_conf_data)
        else:
            print("No frr.conf data to save.")
    else:
        print(f"No running containers found for image '{image_name}'.")
