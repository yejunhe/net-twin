import subprocess

def get_frr_container_ids(image_name="25125/frrouting:10-dev-05221913"):
    try:
        result = subprocess.run(['docker', 'ps', '--filter', f'ancestor={image_name}', '-q'], capture_output=True, text=True)
        container_ids = result.stdout.strip().split('\n')
        return container_ids if container_ids[0] else []
    except subprocess.CalledProcessError as e:
        log_to_file(f"Error occurred: {e}")
        return []

def get_frr_conf(container_id, file_path="/etc/frr/support_bundle_commands.conf"):
    try:
        result = subprocess.run(['docker', 'exec', container_id, 'cat', file_path], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout
        else:
            log_to_file(f"Error reading file: {result.stderr}")
            return None
    except subprocess.CalledProcessError as e:
        log_to_file(f"Error occurred: {e}")
        return None

def log_to_file(message, log_filename="frr_conf_output.txt"):
    try:
        with open(log_filename, 'a') as log_file:
            log_file.write(message + "\n")
    except IOError as e:
        print(f"Error writing to log file: {e}")

if __name__ == "__main__":
    image_name = "25125/frrouting:10-dev-05221913"
    container_ids = get_frr_container_ids(image_name)
    
    if container_ids:
        for cid in container_ids:
            log_to_file(f"Fetching frr.conf from container ID: {cid}")
            frr_conf = get_frr_conf(cid)
            if frr_conf:
                log_to_file(f"--- Configuration from container {cid} ---")
                log_to_file(frr_conf)
                log_to_file(f"--- End of configuration from container {cid} ---\n")
            else:
                log_to_file(f"Could not retrieve frr.conf from container {cid}.")
    else:
        log_to_file(f"No running containers found for image '{image_name}'.")
