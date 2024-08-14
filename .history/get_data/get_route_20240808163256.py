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

def save_to_json(data, filename="frr_conf_data.json"):
    try:
        with open(filename, 'w') as json_file:
            json.dump(data, json_file, indent=4)
        print(f"Data successfully saved to {filename}")
    except IOError as e:
        print(f"Error saving data to file: {e}")

if __name__ == "__main__":
    image_name = "25125/frrouting:10-dev-05221913"  # 镜像名称和标签
    container_ids = get_frr_container_ids(image_name)
    
    frr_conf_data = {}
    
    if container_ids:
        for cid in container_ids:
            print(f"Fetching frr.conf from container ID: {cid}")
            frr_conf = get_frr_conf(cid)
            if frr_conf:
                frr_conf_data[cid] = {
                    "image_name": image_name,
                    "frr_conf": frr_conf
                }
            else:
                print(f"Could not retrieve frr.conf from container {cid}.")
        
        if frr_conf_data:
            save_to_json(frr_conf_data)
        else:
            print("No frr.conf data to save.")
    else:
        print(f"No running containers found for image '{image_name}'.")
