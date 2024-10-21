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
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

class RouterManager:
    def __init__(self, telnet_info: Dict[str, Any], max_workers: int = 10):
        """
        Initialize the RouterManager with telnet information and set up data structures.

        :param telnet_info: Dictionary containing router information.
        :param max_workers: Maximum number of threads to use for parallel execution.
        """
        self.telnet_info = telnet_info
        self.telnet_sysnames: Dict[str, str] = {}
        self.telnet_configurations: Dict[str, str] = {}
        self.docker_sysnames: Dict[str, str] = {}
        self.docker_configurations: Dict[str, str] = {}
        self.max_workers = max_workers

        # Locks to ensure thread-safe writes to shared dictionaries
        self.telnet_lock = Lock()
        self.docker_lock = Lock()

    def get_sysname_via_telnet(self, tn: telnetlib.Telnet) -> Optional[str]:
        """
        Retrieve the sysname from the Telnet session.

        :param tn: telnetlib.Telnet object
        :return: Extracted sysname or None
        """
        try:
            tn.write(b'\n')  # Send newline to prompt sysname
            time.sleep(1)  # Wait for response
            output = tn.read_very_eager().decode('ascii', errors='ignore')
            logging.debug(f"[{tn.host}:{tn.port}] Output for sysname detection:\n{output}")

            # Extract sysname formatted as <SYSNAME>
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

    def execute_telnet_commands(self, tn: telnetlib.Telnet, commands: list, quit_cmd: bytes) -> str:
        """
        Execute a sequence of Telnet commands and handle session termination.

        :param tn: telnetlib.Telnet object
        :param commands: List of commands to execute
        :param quit_cmd: Command to terminate the session (e.g., b'quit\n')
        :return: Combined output from all commands
        """
        output = ""
        try:
            tn.write(b'\n')  # Ensure connection stability
            time.sleep(1)
            initial_output = tn.read_very_eager().decode('ascii', errors='ignore')
            output += initial_output
            logging.debug(f"[{tn.host}:{tn.port}] Initial output after sending newline:\n{initial_output}")

            for cmd in commands:
                tn.write(cmd.encode('ascii') + b'\n')
                logging.info(f"[{tn.host}:{tn.port}] Sending command: {cmd}")
                time.sleep(1)
                cmd_output = tn.read_very_eager().decode('ascii', errors='ignore')
                output += cmd_output
                logging.debug(f"[{tn.host}:{tn.port}] Output for '{cmd}':\n{cmd_output}")

            # Check prompt to decide whether to send quit command
            prompt = self.get_prompt(tn)
            if prompt and not (prompt.startswith('<') and prompt.endswith('>')):
                tn.write(quit_cmd)
                logging.info(f"[{tn.host}:{tn.port}] Sending quit command.")
                time.sleep(1)
                quit_output = tn.read_very_eager().decode('ascii', errors='ignore')
                output += quit_output
                logging.debug(f"[{tn.host}:{tn.port}] Output after quitting:\n{quit_output}")

            return output
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] Telnet Error while executing commands: {e}")
            return output

    def get_prompt(self, tn: telnetlib.Telnet) -> Optional[str]:
        """
        Retrieve the current prompt from the Telnet session.

        :param tn: telnetlib.Telnet object
        :return: Prompt string or None
        """
        try:
            time.sleep(1)  # Wait for prompt to appear
            output = tn.read_very_eager().decode('ascii', errors='ignore')
            logging.debug(f"[{tn.host}:{tn.port}] Output for prompt detection:\n{output}")
            lines = output.splitlines()
            if lines:
                prompt = lines[-1].strip()
                logging.debug(f"[{tn.host}:{tn.port}] Detected prompt: {prompt}")
                return prompt
            return None
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] Error while getting prompt: {e}")
            return None

    def get_configuration_via_telnet(self, tn: telnetlib.Telnet, image_type: str) -> Optional[str]:
        """
        Retrieve the router configuration via Telnet based on the image type.

        :param tn: telnetlib.Telnet object
        :param image_type: Type of the router image (e.g., 'h3c', 'huaweine40')
        :return: Configuration output or None
        """
        try:
            if "h3c" in image_type:
                commands = ['screen-length disable', 'display ip routing-table']
                quit_cmd = b'quit\n'
            elif "huaweine40" in image_type:
                commands = ['scr 0 t', 'display ip routing-table']
                quit_cmd = b'q\n'
            else:
                logging.warning(f"[{tn.host}:{tn.port}] Unsupported image_type '{image_type}'. Skipping.")
                return None

            config_output = self.execute_telnet_commands(tn, commands, quit_cmd)
            if config_output:
                sysname = self.get_sysname_via_telnet(tn)
                if sysname:
                    key = f"{tn.host}:{tn.port}"
                    with self.telnet_lock:
                        self.telnet_sysnames[key] = sysname
                        self.telnet_configurations[key] = config_output
            else:
                logging.warning(f"[{tn.host}:{tn.port}] No output received from Telnet commands.")
            return config_output
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] Telnet Error while getting configuration: {e}")
            return None

    def execute_docker_commands(self, container_id: str, commands: list) -> str:
        """
        Execute a list of commands inside a Docker container.

        :param container_id: Docker container ID
        :param commands: List of commands to execute
        :return: Combined output from all commands
        """
        combined_output = ""
        for cmd in commands:
            try:
                full_cmd = ['docker', 'exec', container_id, 'bash', '-c', cmd]
                logging.info(f"[Docker:{container_id}] Executing: {cmd}")
                output = subprocess.check_output(full_cmd, stderr=subprocess.STDOUT).decode('utf-8', errors='ignore')
                combined_output += output
                logging.debug(f"[Docker:{container_id}] Output for '{cmd}':\n{output}")
            except subprocess.CalledProcessError as e:
                logging.error(f"[Docker:{container_id}] Error executing command '{cmd}': {e.output.decode('utf-8', errors='ignore')}")
            except Exception as e:
                logging.error(f"[Docker:{container_id}] Unexpected error executing command '{cmd}': {e}")
        return combined_output

    def extract_sysname_from_docker_output(self, output: str) -> Optional[str]:
        """
        Extract the sysname from Docker container output.

        :param output: Output string from Docker commands
        :return: Extracted sysname or None
        """
        try:
            for line in output.splitlines():
                line = line.strip()
                logging.debug(f"[Docker] Processing line for sysname extraction: '{line}'")
                if '#' in line:
                    sysname_candidate = line.split('#')[0].strip()
                    if sysname_candidate:
                        logging.info(f"[Docker] Extracted sysname: {sysname_candidate}")
                        return sysname_candidate
            logging.warning("[Docker] Sysname not detected in output.")
            return None
        except Exception as e:
            logging.error(f"[Docker] Error extracting sysname from Docker output: {e}")
            return None

    def connect_via_docker(self, docker_id: str) -> bool:
        """
        Connect to a Docker container and retrieve sysname and configuration.

        :param docker_id: Docker container identifier
        :return: True if successful, False otherwise
        """
        try:
            # Retrieve the actual container ID
            container_id = self.get_container_id(docker_id)
            if not container_id:
                logging.error(f"[Docker:{docker_id}] Container not found.")
                return False

            logging.info(f"[Docker:{docker_id}] Connected to Docker container: {container_id}")

            # Execute initial commands to enter vtysh and retrieve sysname
            initial_commands = ['vtysh', 'echo ""']
            initial_output = self.execute_docker_commands(container_id, initial_commands)
            logging.debug(f"[Docker:{docker_id}] Initial command output:\n{initial_output}")

            sysname = self.extract_sysname_from_docker_output(initial_output)
            if sysname:
                with self.docker_lock:
                    self.docker_sysnames[docker_id] = sysname
            else:
                with self.docker_lock:
                    self.docker_sysnames[docker_id] = "Unknown"

            # Execute subsequent commands to retrieve OSPF routes
            subsequent_commands = [
                'vtysh -c "terminal length 0"',
                'vtysh -c "show ip ospf route"'
            ]
            subsequent_output = self.execute_docker_commands(container_id, subsequent_commands)
            combined_output = initial_output + subsequent_output

            with self.docker_lock:
                self.docker_configurations[docker_id] = combined_output

            logging.info(f"[Docker:{docker_id}] Successfully retrieved configuration.")
            return True
        except Exception as e:
            logging.error(f"[Docker:{docker_id}] Docker Connection Error: {e}")
            return False

    def get_container_id(self, docker_id: str) -> Optional[str]:
        """
        Retrieve the full container ID from a partial or full docker_id.

        :param docker_id: Partial or full Docker container identifier
        :return: Full container ID or None
        """
        try:
            docker_ps_output = subprocess.check_output(['docker', 'ps', '--format', '{{.ID}} {{.Names}}'], stderr=subprocess.STDOUT).decode('utf-8', errors='ignore')
            for line in docker_ps_output.splitlines():
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

    def connect_and_get_sysnames_and_configs(self):
        """
        Connect to each router and retrieve sysnames and configurations based on image_type.
        Utilizes parallel execution for efficiency.
        """
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
                        future = executor.submit(self.connect_via_docker, docker_id)
                        future_to_node[future] = node
                    else:
                        logging.warning("No Docker ID provided for node with image_type 'frrouting'. Skipping.")
                elif "h3c" in image_type or "huaweine40" in image_type:
                    host = node.get("hostip")
                    port = node.get("port")

                    if not host or not port:
                        logging.warning(f"Host IP or port missing for node with image_type '{image_type}'. Skipping.")
                        continue

                    try:
                        tn = telnetlib.Telnet(host, port, timeout=10)
                        tn.host = host  # Assign attributes for logging
                        tn.port = port
                        future = executor.submit(self.get_configuration_via_telnet, tn, image_type)
                        future_to_node[future] = node
                    except Exception as e:
                        logging.error(f"Failed to connect to {host}:{port} via Telnet: {e}")
                        continue
                else:
                    logging.warning(f"Unknown image_type '{image_type}' for node. Skipping.")

            # Process completed futures
            for future in as_completed(future_to_node):
                node = future_to_node[future]
                image_type = node.get("image_type", "").lower()

                if "frrouting" in image_type:
                    docker_id = node.get("dockerid")
                    try:
                        success = future.result()
                        if success:
                            logging.info(f"[Docker:{docker_id}] Configuration retrieval successful.")
                        else:
                            logging.error(f"[Docker:{docker_id}] Configuration retrieval failed.")
                    except Exception as e:
                        logging.error(f"[Docker:{docker_id}] Exception occurred: {e}")
                elif "h3c" in image_type or "huaweine40" in image_type:
                    host = node.get("hostip")
                    port = node.get("port")
                    try:
                        config = future.result()
                        if config:
                            logging.info(f"[{host}:{port}] Configuration retrieval successful.")
                        else:
                            logging.error(f"[{host}:{port}] Configuration retrieval failed.")
                    except Exception as e:
                        logging.error(f"[{host}:{port}] Exception occurred: {e}")

    def collect_results(self) -> Dict[str, Any]:
        """
        Compile the collected sysnames and configurations into a structured dictionary.

        :return: Dictionary containing Telnet and Docker device information
        """
        mapping = {
            "telnet_devices": {},
            "docker_devices": {}
        }

        # Collect Telnet-based router information
        for host_port, sysname in self.telnet_sysnames.items():
            config = self.telnet_configurations.get(host_port, 'No config')
            mapping["telnet_devices"][host_port] = {
                "sysname": sysname,
                "configuration": config
            }

        # Collect Docker-based router information
        for docker_id, sysname in self.docker_sysnames.items():
            config = self.docker_configurations.get(docker_id, 'No config')
            mapping["docker_devices"][docker_id] = {
                "sysname": sysname,
                "configuration": config
            }

        return mapping

def find_latest_folder(base_path: str) -> str:
    """
    Find the latest folder by number under the given base path.

    :param base_path: Base directory path
    :return: Name of the latest numbered folder
    """
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
    """
    Load the Telnet information from the JSON input file.

    :param input_path: Path to param.json
    :return: Parsed JSON as a dictionary
    """
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
    """
    Write the collected router configurations to the output file.

    :param output_path: Path to the output JSON file
    :param data: Dictionary containing router configurations
    """
    try:
        with open(output_path, 'w') as f:
            json.dump(data, f, indent=4)
        logging.info(f"Mapping results written to {output_path}")
    except IOError as e:
        logging.error(f"Error writing to output file: {e}")
        sys.exit(1)

def main(input_path: str, output_path: str):
    """
    Main function to orchestrate the router configuration retrieval.

    :param input_path: Path to param.json, may include {t} placeholder
    :param output_path: Path to output JSON file, may include {t} placeholder
    """
    # Resolve the latest folder number if {t} is used
    base_path = "/uploadPath/reasoning"
    if "{t}" in input_path or "{t}" in output_path:
        latest_folder = find_latest_folder(base_path)
        input_path = input_path.replace("{t}", latest_folder)
        output_path = output_path.replace("{t}", latest_folder)
        logging.debug(f"Resolved input_path: {input_path}")
        logging.debug(f"Resolved output_path: {output_path}")

    # Load param.json
    telnet_info = load_telnet_info(input_path)

    # Initialize RouterManager with increased max_workers for better parallelism if needed
    router_manager = RouterManager(telnet_info, max_workers=20)
    router_manager.connect_and_get_sysnames_and_configs()

    # Collect results
    mapping = router_manager.collect_results()

    logging.info("Collected router configurations:")
    logging.info(json.dumps(mapping, indent=4))

    # Write output to file
    write_output(output_path, mapping)

if __name__ == "__main__":
    # Set up argument parsing
    parser = argparse.ArgumentParser(description="Process router configurations from param.json.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json, use {t} for latest folder number.")
    parser.add_argument("-o", "--output", required=True, help="Output path for process information, use {t} for latest folder number.")
    args = parser.parse_args()

    main(args.input, args.output)
