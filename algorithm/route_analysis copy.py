import json
import telnetlib
import os
import argparse
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock


class RouterManager:
    def __init__(self, telnet_info, max_workers=10):
        """
        Initialize the RouterManager with telnet information and set up data structures.
        
        :param telnet_info: Dictionary containing router information.
        :param max_workers: Maximum number of threads to use for parallel execution.
        """
        self.telnet_info = telnet_info
        self.telnet_sysnames = {}
        self.telnet_configurations = {}
        self.docker_sysnames = {}
        self.docker_configurations = {}
        self.max_workers = max_workers

        # Locks to ensure thread-safe writes to shared dictionaries
        self.telnet_lock = Lock()
        self.docker_lock = Lock()

    def get_sysname_via_telnet(self, tn):
        """通过 Telnet 获取 sysname"""
        try:
            tn.write(b'\n')  # 发送回车
            time.sleep(1)  # 等待回车执行完成
            output = tn.read_very_eager().decode('ascii')  # 捕获所有输出
            print(f"[{tn.host}:{tn.port}] Received output for sysname detection:\n{output}")

            # 查找 sysname
            sysname = None
            lines = output.splitlines()
            for line in lines:
                if line.startswith('<') and line.endswith('>'):  # 格式为 <H1>
                    sysname = line.strip('<> ')
                    break

            if sysname:
                print(f"[{tn.host}:{tn.port}] Detected sysname: {sysname}")
                return sysname
            else:
                print(f"[{tn.host}:{tn.port}] No sysname detected.")
                return None
        except Exception as e:
            print(f"[{tn.host}:{tn.port}] Telnet Error while getting sysname: {e}")
            return None

    def execute_telnet_commands_h3c(self, tn):
        """
        Execute commands specific to H3C devices after sending an initial newline.
        :param tn: telnetlib.Telnet object
        :return: Output from the Telnet session
        """
        try:
            output = ""

            # 发送回车确保连接稳定
            tn.write(b'\n')
            time.sleep(1)
            output += tn.read_very_eager().decode('ascii')
            print(f"[{tn.host}:{tn.port}] Sent newline command, output: {output}")

            # 发送 H3C 特定的命令序列
            commands = [
                'screen-length disable',
                'display ip routing-table'
            ]

            for cmd in commands:
                tn.write(cmd.encode('ascii') + b'\n')
                print(f"[{tn.host}:{tn.port}] Sending '{cmd}' command...")
                time.sleep(1)  # 等待命令执行
                cmd_output = tn.read_very_eager().decode('ascii')
                output += cmd_output
                print(f"[{tn.host}:{tn.port}] Command '{cmd}' output: {cmd_output}")

            # 检查提示符，决定是否发送 'quit'
            prompt = self.get_prompt(tn)
            if prompt:
                if not (prompt.startswith('<') and prompt.endswith('>')):
                    tn.write(b'quit\n')
                    print(f"[{tn.host}:{tn.port}] Sending 'quit' command...")
                    time.sleep(1)
                    cmd_output = tn.read_very_eager().decode('ascii')
                    output += cmd_output
                    print(f"[{tn.host}:{tn.port}] Command 'quit' output: {cmd_output}")
            return output

        except Exception as e:
            print(f"[{tn.host}:{tn.port}] Telnet Error while executing H3C commands: {e}")
            return None

    def execute_telnet_commands_huaweine40(self, tn):
        """
        Execute commands specific to Huawei NE40 devices after sending an initial newline.
        :param tn: telnetlib.Telnet object
        :return: Output from the Telnet session
        """
        try:
            output = ""

            # 发送回车确保连接稳定
            tn.write(b'\n')
            time.sleep(1)
            output += tn.read_very_eager().decode('ascii')
            print(f"[{tn.host}:{tn.port}] Sent newline command, output: {output}")

            # 发送 Huawei NE40 特定的命令序列
            commands = [
                'scr 0 t',
                'display ip routing-table'
            ]

            for cmd in commands:
                tn.write(cmd.encode('ascii') + b'\n')
                print(f"[{tn.host}:{tn.port}] Sending '{cmd}' command...")
                time.sleep(1)  # 等待命令执行
                cmd_output = tn.read_very_eager().decode('ascii')
                output += cmd_output
                print(f"[{tn.host}:{tn.port}] Command '{cmd}' output: {cmd_output}")

            # 检查提示符，决定是否发送 'q'
            prompt = self.get_prompt(tn)
            if prompt:
                if not (prompt.startswith('<') and prompt.endswith('>')):
                    tn.write(b'q\n')
                    print(f"[{tn.host}:{tn.port}] Sending 'q' command...")
                    time.sleep(1)
                    cmd_output = tn.read_very_eager().decode('ascii')
                    output += cmd_output
                    print(f"[{tn.host}:{tn.port}] Command 'q' output: {cmd_output}")
            return output

        except Exception as e:
            print(f"[{tn.host}:{tn.port}] Telnet Error while executing Huawei NE40 commands: {e}")
            return None

    def get_prompt(self, tn):
        """
        获取当前提示符。
        :param tn: telnetlib.Telnet object
        :return: 提示符字符串
        """
        try:
            time.sleep(1)  # 等待提示符出现
            output = tn.read_very_eager().decode('ascii')
            print(f"[{tn.host}:{tn.port}] Received output for prompt detection:\n{output}")
            lines = output.splitlines()
            if lines:
                prompt = lines[-1].strip()
                print(f"[{tn.host}:{tn.port}] Detected prompt: {prompt}")
                return prompt
            return None
        except Exception as e:
            print(f"[{tn.host}:{tn.port}] Error while getting prompt: {e}")
            return None

    def get_configuration_via_telnet(self, tn, image_type):
        """通过 Telnet 获取路由器的配置信息"""
        try:
            if "h3c" in image_type:
                # 对于 H3C 设备，使用专门的方法执行命令
                config_output = self.execute_telnet_commands_h3c(tn)
                if config_output:
                    # 假设 H3c 设备的 sysname 是唯一的，不重复
                    sysname = self.get_sysname_via_telnet(tn)
                    if sysname:
                        with self.telnet_lock:
                            self.telnet_sysnames[f"{tn.host}:{tn.port}"] = sysname
                            self.telnet_configurations[f"{tn.host}:{tn.port}"] = config_output
            elif "huaweine40" in image_type:
                # 对于 Huawei NE40 设备，使用专门的方法执行命令
                config_output = self.execute_telnet_commands_huaweine40(tn)
                if config_output:
                    sysname = self.get_sysname_via_telnet(tn)
                    if sysname:
                        with self.telnet_lock:
                            self.telnet_sysnames[f"{tn.host}:{tn.port}"] = sysname
                            self.telnet_configurations[f"{tn.host}:{tn.port}"] = config_output
            else:
                # 对于其他设备，已删除通用命令执行逻辑
                print(f"[{tn.host}:{tn.port}] Unsupported image_type '{image_type}'. Skipping configuration retrieval.")
                return None

            if not config_output:
                print(f"[{tn.host}:{tn.port}] No output received from Telnet commands.")
                return None
            return config_output
        except Exception as e:
            print(f"[{tn.host}:{tn.port}] Telnet Error while getting configuration: {e}")
            return None

    def execute_docker_commands(self, container_id, commands):
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
                print(f"[Docker:{container_id}] Executing: {cmd}")
                output = subprocess.check_output(full_cmd, stderr=subprocess.STDOUT).decode('utf-8')
                combined_output += output
            except subprocess.CalledProcessError as e:
                print(f"[Docker:{container_id}] Error executing command '{cmd}': {e.output.decode('utf-8')}")
            except Exception as e:
                print(f"[Docker:{container_id}] Unexpected error executing command '{cmd}': {e}")
        return combined_output

    def connect_via_docker(self, docker_id):
        """通过 Docker 执行命令进入 FRR 路由器的容器并执行特定命令"""
        try:
            # 获取容器名称对应的 CONTAINER ID
            docker_ps_output = subprocess.check_output(['docker', 'ps'], stderr=subprocess.STDOUT).decode('utf-8')
            container_id = None
            for line in docker_ps_output.splitlines()[1:]:
                if docker_id in line:
                    container_id = line.split()[0]
                    break

            if not container_id:
                print(f"[Docker:{docker_id}] Container not found.")
                return False

            print(f"[Docker:{docker_id}] Connected to Docker container: {container_id}")

            # 执行命令: 进入 vtysh 后发送回车并提取 sysname
            commands = [
                'vtysh',               # 进入 vtysh
                'echo ""'              # 发送一个回车命令
            ]
            output = self.execute_docker_commands(container_id, commands)
            print(f"[Docker:{docker_id}] Command output for sysname detection:\n{output}")

            # 提取 sysname
            sysname = self.extract_sysname_from_docker_output(output)
            if sysname:
                print(f"[Docker:{docker_id}] Detected sysname: {sysname}")
                with self.docker_lock:
                    self.docker_sysnames[docker_id] = sysname
            else:
                print(f"[Docker:{docker_id}] Sysname not detected.")
                with self.docker_lock:
                    self.docker_sysnames[docker_id] = "Unknown"

            # 执行后续命令
            subsequent_commands = [
                'vtysh -c "terminal length 0"',   # 设置完整输出
                'vtysh -c "show ip ospf route"'   # 显示 OSPF 路由信息
            ]
            output += self.execute_docker_commands(container_id, subsequent_commands)
            print(f"[Docker:{docker_id}] Command output after sysname:\n{output}")

            with self.docker_lock:
                self.docker_configurations[docker_id] = output

            return True
        except subprocess.CalledProcessError as e:
            print(f"[Docker:{docker_id}] Docker Connection Error: {e.output.decode('utf-8')}")
            return False
        except Exception as e:
            print(f"[Docker:{docker_id}] Docker Connection Error: {e}")
            return False

    def extract_sysname_from_docker_output(self, output):
        """
        从 Docker 容器的输出中提取 sysname。
        假设 sysname 以类似于 'FR2#' 的格式显示，其中 'FR2' 是 sysname。
        """
        try:
            sysname = None
            lines = output.splitlines()
            for line in lines:
                line = line.strip()
                print(f"[Docker] Processing line for sysname extraction: '{line}'")  # 调试信息
                # 检查 '# ' 前缀的行，提取其中的 sysname
                if '#' in line:
                    parts = line.split('#')
                    if len(parts) >= 2:
                        sysname_candidate = parts[0].strip()
                        if sysname_candidate:
                            sysname = sysname_candidate
                            print(f"[Docker] Extracted sysname from line: {sysname}")  # 调试信息
                            break
            print(f"[Docker] Final extracted sysname: {sysname}")  # 调试信息
            return sysname
        except Exception as e:
            print(f"[Docker] Error extracting sysname from Docker output: {e}")
            return None

    def connect_and_get_sysnames_and_configs(self):
        """Connect to each router and retrieve sysnames and configurations based on image_type."""
        nodes = self.telnet_info.get("node", [])
        if not nodes:
            print("No nodes found in telnet_info.")
            return

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_node = {}
            for node in nodes:
                image_type = node.get("image_type", "").lower()

                if "frrouting" in image_type:
                    # 通过 Docker 执行命令
                    docker_id = node.get("dockerid")
                    if docker_id:
                        future = executor.submit(self.connect_via_docker, docker_id)
                        future_to_node[future] = node
                    else:
                        print("No Docker ID provided for node with image_type 'frrouting'. Skipping.")
                elif "h3c" in image_type or "huaweine40" in image_type:
                    # 通过 Telnet 登录并获取 sysname 和配置
                    host = node.get("hostip")
                    port = node.get("port")

                    if not host or not port:
                        print(f"Host IP or port missing for node with image_type '{image_type}'. Skipping.")
                        continue

                    try:
                        tn = telnetlib.Telnet(host, port, timeout=10)
                        tn.host = host  # 为了在 get_configuration_via_telnet 中使用
                        tn.port = port
                        # Submit Telnet task to executor
                        future = executor.submit(self.get_configuration_via_telnet, tn, image_type)
                        future_to_node[future] = node
                    except Exception as e:
                        print(f"Failed to connect to {host}:{port} via Telnet: {e}")
                        continue
                else:
                    print(f"Unknown image_type '{image_type}' for node. Skipping.")

            # Process completed futures
            for future in as_completed(future_to_node):
                node = future_to_node[future]
                image_type = node.get("image_type", "").lower()
                if "frrouting" in image_type:
                    docker_id = node.get("dockerid")
                    try:
                        success = future.result()
                        if success:
                            print(f"[Docker:{docker_id}] Successfully retrieved configuration.")
                        else:
                            print(f"[Docker:{docker_id}] Failed to retrieve configuration.")
                    except Exception as e:
                        print(f"[Docker:{docker_id}] Exception occurred: {e}")
                elif "h3c" in image_type or "huaweine40" in image_type:
                    host = node.get("hostip")
                    port = node.get("port")
                    try:
                        config = future.result()
                        if config:
                            print(f"[{host}:{port}] Successfully retrieved configuration.")
                        else:
                            print(f"[{host}:{port}] Failed to retrieve configuration.")
                    except Exception as e:
                        print(f"[{host}:{port}] Exception occurred: {e}")

    def execute_docker_commands(self, container_id, commands):
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
                print(f"[Docker:{container_id}] Executing: {cmd}")
                output = subprocess.check_output(full_cmd, stderr=subprocess.STDOUT).decode('utf-8')
                combined_output += output
            except subprocess.CalledProcessError as e:
                print(f"[Docker:{container_id}] Error executing command '{cmd}': {e.output.decode('utf-8')}")
            except Exception as e:
                print(f"[Docker:{container_id}] Unexpected error executing command '{cmd}': {e}")
        return combined_output

    # Rest of the class remains unchanged...


def find_latest_folder(base_path):
    """Find the latest folder by number under the given base path."""
    try:
        all_folders = [f for f in os.listdir(base_path) if f.isdigit()]
        if not all_folders:
            raise ValueError("No numbered folders found in the base path.")
        latest_folder = max(all_folders, key=int)
        return latest_folder
    except FileNotFoundError:
        print(f"Base path not found: {base_path}")
        sys.exit(1)
    except ValueError as ve:
        print(ve)
        sys.exit(1)


def main(input_path, output_path):
    # Resolve the latest folder number if {t} is used
    base_path = "/uploadPath/reasoning"
    if "{t}" in input_path or "{t}" in output_path:
        latest_folder = find_latest_folder(base_path)
        input_path = input_path.replace("{t}", latest_folder)
        output_path = output_path.replace("{t}", latest_folder)

    # Load param.json
    try:
        with open(input_path, 'r') as f:
            telnet_info = json.load(f)
    except FileNotFoundError:
        print(f"param.json file not found at path: {input_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON from param.json: {e}")
        sys.exit(1)

    # Manage Telnet/SSH connections and retrieve sysnames and configurations
    router_manager = RouterManager(telnet_info)
    router_manager.connect_and_get_sysnames_and_configs()

    # Prepare mapping results
    mapping = {
        "telnet_devices": {},
        "docker_devices": {}
    }

    # Collect Telnet-based router information
    for host_port, sysname in router_manager.telnet_sysnames.items():
        config = router_manager.telnet_configurations.get(host_port, 'No config')
        mapping["telnet_devices"][host_port] = {
            "sysname": sysname,
            "configuration": config
        }

    # Collect Docker-based router information
    for docker_id, sysname in router_manager.docker_sysnames.items():
        config = router_manager.docker_configurations.get(docker_id, 'No config')
        mapping["docker_devices"][docker_id] = {
            "sysname": sysname,
            "configuration": config
        }

    print("Collected router configurations:")
    print(json.dumps(mapping, indent=4))

    # Output results to file
    try:
        with open(output_path, 'w') as f:
            f.write(json.dumps(mapping, indent=4))
        print(f"Mapping results written to {output_path}")
    except IOError as e:
        print(f"Error writing to output file: {e}")
        sys.exit(1)


if __name__ == "__main__":
    # Set up argument parsing
    parser = argparse.ArgumentParser(description="Process router configurations from param.json.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json, use {t} for latest folder number.")
    parser.add_argument("-o", "--output", required=True, help="Output path for process information, use {t} for latest folder number.")
    args = parser.parse_args()

    main(args.input, args.output)
