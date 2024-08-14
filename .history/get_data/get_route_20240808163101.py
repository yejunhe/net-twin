import subprocess

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

if __name__ == "__main__":
    image_name = "25125/frrouting:10-dev-05221913"  # 镜像名称和标签
    container_ids = get_frr_container_ids(image_name)
    if container_ids:
        for cid in container_ids:
            print(f"Fetching frr.conf from container ID: {cid}")
            frr_conf = get_frr_conf(cid)
            if frr_conf:
                print(f"Contents of frr.conf in container {cid}:\n")
                print(frr_conf)
            else:
                print(f"Could not retrieve frr.conf from container {cid}.")
    else:
        print(f"No running containers found for image '{image_name}'.")
