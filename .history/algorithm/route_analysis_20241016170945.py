import json
import telnetlib
import os
import argparse
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import logging
from typing import Optional, Dict, Any

# Configure logging for better traceability and control
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

class RouterManager:
    def __init__(self, telnet_info: Dict[str, Any], max_workers: int = 20):
        self.telnet_info = telnet_info
        self.telnet_sysnames: Dict[str, str] = {}
        self.telnet_configurations: Dict[str, str] = {}
        self.docker_sysnames: Dict[str, str] = {}
        self.docker_configurations: Dict[str, str] = {}
        self.max_workers = max_workers
        self.telnet_lock = Lock()
        self.docker_lock = Lock()

    def execute_telnet_commands(self, tn: telnetlib.Telnet, commands: list, quit_cmd: bytes) -> str:
        try:
            tn.write(b'\n')
            time.sleep(1)
            output = tn.read_very_eager().decode('ascii', errors='ignore')
            logging.debug(f"[{tn.host}:{tn.port}] Initial Telnet output:\n{output}")

            for cmd in commands:
                tn.write(cmd.encode('ascii') + b'\n')
                logging.info(f"[{tn.host}:{tn.port}] Sending command: {cmd}")
                time.sleep(1)
                cmd_output = tn.read_very_eager().decode('ascii', errors='ignore')
                output += cmd_output
                logging.debug(f"[{tn.host}:{tn.port}] Output for '{cmd}':\n{cmd_output}")

            prompt = self.get_prompt(tn)
            if prompt and not (prompt.startswith('<') and prompt.endswith('>')):
                tn.write(quit_cmd)
                logging.info(f"[{tn.host}:{tn.port}] Sending quit command.")
                time.sleep(1)
                output += tn.read_very_eager().decode('ascii', errors='ignore')
            return output
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] Telnet Error: {e}")
            return ""

    def get_prompt(self, tn: telnetlib.Telnet) -> Optional[str]:
        try:
            time.sleep(1)
            output = tn.read_very_eager().decode('ascii', errors='ignore')
            lines = output.splitlines()
            prompt = lines[-1].strip() if lines else None
            logging.debug(f"[{tn.host}:{tn.port}] Detected prompt: {prompt}")
            return prompt
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] Error getting prompt: {e}")
            return None

    def get_sysname_via_telnet(self, tn: telnetlib.Telnet) -> Optional[str]:
        try:
            tn.write(b'\n')
            time.sleep(1)
            output = tn.read_very_eager().decode('ascii', errors='ignore')
            for line in output.splitlines():
                if line.startswith('<') and line.endswith('>'):
                    sysname = line.strip('<> ').strip()
                    logging.info(f"[{tn.host}:{tn.port}] Detected sysname: {sysname}")
                    return sysname
            logging.warning(f"[{tn.host}:{tn.port}] No sysname detected.")
            return None
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] Telnet Error while getting sysname: {e}")
            return None

    def get_configuration_via_telnet(self, tn: telnetlib.Telnet, image_type: str) -> Optional[str]:
        commands_map = {
            "h3c": (['screen-length disable', 'display ip routing-table'], b'quit\n'),
            "huaweine40": (['scr 0 t', 'display ip routing-table'], b'q\n')
        }
        commands, quit_cmd = commands_map.get(image_type, ([], b''))
        if not commands:
            logging.warning(f"[{tn.host}:{tn.port}] Unsupported image_type '{image_type}'. Skipping.")
            return None

        output = self.execute_telnet_commands(tn, commands, quit_cmd)
        if output:
            sysname = self.get_sysname_via_telnet(tn)
            if sysname:
                key = f"{tn.host}:{tn.port}"
                with self.telnet_lock:
                    self.telnet_sysnames[key] = sysname
                    self.telnet_configurations[key] = output
        else:
            logging.warning(f"[{tn.host}:{tn.port}] No output received from Telnet commands.")
        return output

    def execute_docker_commands(self, container_id: str, commands: list) -> str:
        combined_output = ""
        for cmd in commands:
            try:
                full_cmd = ['docker', 'exec', container_id, 'bash', '-c', cmd]
                logging.info(f"[Docker:{container_id}] Executing: {cmd}")
                output = subprocess.check_output(full_cmd, stderr=subprocess.STDOUT).decode('utf-8', errors='ignore')
                combined_output += output
                logging.debug(f"[Docker:{container_id}] Output for '{cmd}':\n{output}")
            except subprocess.CalledProcessError as e:
                logging.error(f"[Docker:{container_id}] Error executing '{cmd}': {e.output.decode('utf-8', errors='ignore')}")
            except Exception as e:
                logging.error(f"[Docker:{container_id}] Unexpected error executing '{cmd}': {e}")
        return combined_output

    def extract_sysname_from_docker_output(self, output: str) -> Optional[str]:
        for line in output.splitlines():
            if '#' in line:
                sysname_candidate = line.split('#')[0].strip()
                if sysname_candidate:
                    logging.info(f"[Docker] Extracted sysname: {sysname_candidate}")
                    return sysname_candidate
        logging.warning("[Docker] Sysname not detected in output.")
        return None

    def get_container_id(self, docker_id: str) -> Optional[str]:
        try:
            docker_ps = subprocess.check_output(['docker', 'ps', '--format', '{{.ID}} {{.Names}}'], stderr=subprocess.STDOUT).decode('utf-8', errors='ignore')
            for line in docker_ps.splitlines():
                cid, name = line.strip().split(None, 1)
                if docker_id in cid or docker_id == name:
                    return cid
            return None
        except subprocess.CalledProcessError as e:
            logging.error(f"[Docker:{docker_id}] Error retrieving Docker containers: {e.output.decode('utf-8', errors='ignore')}")
            return None
        except Exception as e:
            logging.error(f"[Docker:{docker_id}] Unexpected error retrieving container ID: {e}")
            return None

    def connect_via_docker(self, docker_id: str) -> bool:
        container_id = self.get_container_id(docker_id)
        if not container_id:
            logging.error(f"[Docker:{docker_id}] Container not found.")
            return False

        logging.info(f"[Docker:{docker_id}] Connected to Docker container: {container_id}")
        initial_output = self.execute_docker_commands(container_id, ['vtysh', 'echo ""'])
        sysname = self.extract_sysname_from_docker_output(initial_output)
        with self.docker_lock:
            self.docker_sysnames[docker_id] = sysname if sysname else "Unknown"

        subsequent_output = self.execute_docker_commands(container_id, [
            'vtysh -c "terminal length 0"',
            'vtysh -c "show ip ospf route"'
        ])
        with self.docker_lock:
            self.docker_configurations[docker_id] = initial_output + subsequent_output

        logging.info(f"[Docker:{docker_id}] Successfully retrieved configuration.")
        return True

    def connect_and_get_sysnames_and_configs(self):
        nodes = self.telnet_info.get("node", [])
        if not nodes:
            logging.warning("No nodes found in telnet_info.")
            return

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_node = {}
            for node in nodes:
                image_type = node.get("image_type", "").lower()
                if "frrouting" in image_type:
                    docker_id = node.get("dockerid")
                    if docker_id:
                        future_to_node[executor.submit(self.connect_via_docker, docker_id)] = node
                    else:
                        logging.warning("No Docker ID provided for node with image_type 'frrouting'. Skipping.")
                elif "h3c" in image_type or "huaweine40" in image_type:
                    host, port = node.get("hostip"), node.get("port")
                    if not host or not port:
                        logging.warning(f"Host IP or port missing for node with image_type '{image_type}'. Skipping.")
                        continue
                    try:
                        tn = telnetlib.Telnet(host, port, timeout=10)
                        tn.host, tn.port = host, port
                        future_to_node[executor.submit(self.get_configuration_via_telnet, tn, image_type)] = node
                    except Exception as e:
                        logging.error(f"Failed to connect to {host}:{port} via Telnet: {e}")
            for future in as_completed(future_to_node):
                node = future_to_node[future]
                image_type = node.get("image_type", "").lower()
                if "frrouting" in image_type:
                    docker_id = node.get("dockerid")
                    success = future.result()
                    msg = "successful" if success else "failed"
                    logging.info(f"[Docker:{docker_id}] Configuration retrieval {msg}.")
                elif "h3c" in image_type or "huaweine40" in image_type:
                    host, port = node.get("hostip"), node.get("port")
                    config = future.result()
                    msg = "successful" if config else "failed"
                    logging.info(f"[{host}:{port}] Configuration retrieval {msg}.")

    def collect_results(self) -> Dict[str, Any]:
        return {
            "telnet_devices": {
                host_port: {"sysname": sysname, "configuration": self.telnet_configurations.get(host_port, 'No config')}
                for host_port, sysname in self.telnet_sysnames.items()
            },
            "docker_devices": {
                docker_id: {"sysname": sysname, "configuration": self.docker_configurations.get(docker_id, 'No config')}
                for docker_id, sysname in self.docker_sysnames.items()
            }
        }

def find_latest_folder(base_path: str) -> str:
    try:
        all_folders = [f for f in os.listdir(base_path) if f.isdigit()]
        if not all_folders:
            raise ValueError("No numbered folders found in the base path.")
        latest_folder = max(all_folders, key=int)
        logging.info(f"Latest folder identified: {latest_folder}")
        return latest_folder
    except FileNotFoundError:
        logging.error(f"Base path not found: {base_path}")
        sys.exit(1)
    except ValueError as ve:
        logging.error(ve)
        sys.exit(1)

def load_telnet_info(input_path: str) -> Dict[str, Any]:
    try:
        with open(input_path, 'r') as f:
            telnet_info = json.load(f)
        logging.info(f"Successfully loaded telnet_info from {input_path}")
        return telnet_info
    except FileNotFoundError:
        logging.error(f"param.json file not found at path: {input_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logging.error(f"Error decoding JSON from param.json: {e}")
        sys.exit(1)

def write_output(output_path: str, data: Dict[str, Any]):
    try:
        with open(output_path, 'w') as f:
            json.dump(data, f, indent=4)
        logging.info(f"Mapping results written to {output_path}")
    except IOError as e:
        logging.error(f"Error writing to output file: {e}")
        sys.exit(1)

def main(input_path: str, output_path: str):
    base_path = "/uploadPath/reasoning"
    if "{t}" in input_path or "{t}" in output_path:
        latest_folder = find_latest_folder(base_path)
        input_path = input_path.replace("{t}", latest_folder)
        output_path = output_path.replace("{t}", latest_folder)
        logging.debug(f"Resolved input_path: {input_path}")
        logging.debug(f"Resolved output_path: {output_path}")

    telnet_info = load_telnet_info(input_path)
    router_manager = RouterManager(telnet_info)
    router_manager.connect_and_get_sysnames_and_configs()
    mapping = router_manager.collect_results()

    logging.info("Collected router configurations:")
    logging.info(json.dumps(mapping, indent=4))
    write_output(output_path, mapping)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process router configurations from param.json.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json, use {t} for latest folder number.")
    parser.add_argument("-o", "--output", required=True, help="Output path for process information, use {t} for latest folder number.")
    args = parser.parse_args()
    main(args.input, args.output)
