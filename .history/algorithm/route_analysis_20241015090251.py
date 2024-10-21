import json
import telnetlib
import os
import argparse
import subprocess
import sys
import time


class RouterManager:
    def __init__(self, telnet_info):
        self.telnet_info = telnet_info
        self.telnet_sysnames = {}
        self.telnet_configurations = {}
        self.docker_sysnames = {}
        self.docker_configurations = {}

    def get_sysname_via_telnet(self, tn):
        """通过 Telnet 获取 sysname"""
        try:
            tn.write(b'\n')  # 发送回车
            time.sleep(1)  # 等待回车执行完成
            output = tn.read_very_eager().decode('ascii')  # 捕获所有输出
            print(f"Received output for sysname detection: {output}")
        
            # 查找 sysname
            sysname = None
            lines = output.splitlines()
            for line in lines:
                if line.startswith('<') and line.endswith('>'):  # 格式为 <H1>
                    sysname = line.strip('<> ')
                    break

            if sysname:
                print(f"Detected sysname: {sysname}")
                return sysname
            else:
                print("No sysname detected.")
                return None
        except Exception as e:
            print(f"Telnet Error while getting sysname: {e}")
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
            print(f"Sent newline command, output: {output}")

            # 发送 H3C 特定的命令序列
            commands = [
                'screen-length disable',
                'display ip routing-table'
            ]

            for cmd in commands:
                tn.write(cmd.encode('ascii') + b'\n')
                print(f"Sending '{cmd}' command...")
                time.sleep(1)  # 等待命令执行
                cmd_output = tn.read_very_eager().decode('ascii')
                output += cmd_output
                print(f"Command '{cmd}' output: {cmd_output}")

            # 检查提示符，决定是否发送 'quit'
            prompt = self.get_prompt(tn)
            if prompt:
                if not (prompt.startswith('<') and prompt.endswith('>')):
                    tn.write(b'quit\n')
                    print("Sending 'quit' command...")
                    time.sleep(1)
                    cmd_output = tn.read_very_eager().decode('ascii')
                    output += cmd_output
                    print(f"Command 'quit' output: {cmd_output}")
            return output

        except Exception as e:
            print(f"Telnet Error while executing H3C commands: {e}")
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
            print(f"Sent newline command, output: {output}")

            # 发送 Huawei NE40 特定的命令序列
            commands = [
                'scr 0 t',
                'display ip routing-table'
            ]

            for cmd in commands:
                tn.write(cmd.encode('ascii') + b'\n')
                print(f"Sending '{cmd}' command...")
                time.sleep(1)  # 等待命令执行
                cmd_output = tn.read_very_eager().decode('ascii')
                output += cmd_output
                print(f"Command '{cmd}' output: {cmd_output}")

            # 检查提示符，决定是否发送 'q'
            prompt = self.get_prompt(tn)
            if prompt:
                if not (prompt.startswith('<') and prompt.endswith('>')):
                    tn.write(b'q\n')
                    print("Sending 'q' command...")
                    time.sleep(1)
                    cmd_output = tn.read_very_eager().decode('ascii')
                    output += cmd_output
                    print(f"Command 'q' output: {cmd_output}")
            return output

        except Exception as e:
            print(f"Telnet Error while executing Huawei NE40 commands: {e}")
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
            print(f"Received output for prompt detection: {output}")
            lines = output.splitlines()
            if lines:
                prompt = lines[-1].strip()
                print(f"Detected prompt: {prompt}")
                return prompt
            return None
        except Exception as e:
            print(f"Error while getting prompt: {e}")
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
                        self.telnet_sysnames[f"{tn.host}:{tn.port}"] = sysname
                        self.telnet_configurations[f"{tn.host}:{tn.port}"] = config_output
            elif "huaweine40" in image_type:
                # 对于 Huawei NE40 设备，使用专门的方法执行命令
                config_output = self.execute_telnet_commands_huaweine40(tn)
                if config_output:
                    sysname = self.get_sysname_via_telnet(tn)
                    if sysname:
                        self.telnet_sysnames[f"{tn.host}:{tn.port}"] = sysname
                        self.telnet_configurations[f"{tn.host}:{tn.port}"] = config_output
            else:
                # 对于其他设备，已删除通用命令执行逻辑
                print(f"Unsupported image_type '{image_type}'. Skipping configuration retrieval.")
                return None

            if not config_output:
                print("No output received from Telnet commands.")
                return None
            return config_output
        except Exception as e:
            print(f"Telnet Error while getting configuration: {e}")
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
                print(f"Executing in Docker container {container_id}: {cmd}")
                output = subprocess.check_output(full_cmd, stderr=subprocess.STDOUT).decode('utf-8')
                combined_output += output
            except subprocess.CalledProcessError as e:
                print(f"Error executing command '{cmd}' in Docker container {container_id}: {e.output.decode('utf-8')}")
            except Exception as e:
                print(f"Unexpected error executing command '{cmd}' in Docker container {container_id}: {e}")
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
                print(f"Container not found for Docker ID: {docker_id}")
                return False

            print(f"Connected to Docker container: {container_id}")

            # 执行命令: vtysh 并获取 sysname
            # 仅执行必要的命令，删除获取 hostname 的命令
            commands = [
                'vtysh -c "terminal length 0"',
                'vtysh -c "show ip ospf route"'
            ]
            output = self.execute_docker_commands(container_id, commands)
            print(f"Docker command output:\n{output}")

            # 提取 sysname
            sysname = self.extract_sysname_from_docker_output(output)
            if sysname:
                print(f"Detected sysname from Docker container {docker_id}: {sysname}")
                self.docker_sysnames[docker_id] = sysname
            else:
                print(f"Sysname not detected in Docker container {docker_id} output.")
                self.docker_sysnames[docker_id] = "Unknown"

            self.docker_configurations[docker_id] = output

            return True
        except subprocess.CalledProcessError as e:
            print(f"Docker Connection Error: {e.output.decode('utf-8')}")
            return False
        except Exception as e:
            print(f"Docker Connection Error: {e}")
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
                print(f"Processing line for sysname extraction: '{line}'")  # 调试信息
                # 检查 'FR2#' 格式
                if line.endswith('#'):
                    sysname_candidate = line.split('#')[0]
                    if sysname_candidate:
                        sysname = sysname_candidate
                        print(f"Extracted sysname from prompt: {sysname}")  # 调试信息
                        break
            print(f"Final extracted sysname: {sysname}")  # 调试信息
            return sysname
        except Exception as e:
            print(f"Error extracting sysname from Docker output: {e}")
            return None

    def connect_and_get_sysnames_and_configs(self):
        """Connect to each router and retrieve sysnames and configurations based on image_type."""
        for node in self.telnet_info.get("node", []):
            image_type = node.get("image_type", "").lower()

            if "frrouting" in image_type:
                # 通过 Docker 执行命令
                docker_id = node.get("dockerid")
                if docker_id:
                    success = self.connect_via_docker(docker_id)
                    if success:
                        # 配置信息已通过 Docker 命令获取并执行
                        pass
                else:
                    print(f"No Docker ID provided for node with image_type 'frrouting'.")
            elif "h3c" in image_type or "huaweine40" in image_type:
                # 通过 Telnet 登录并获取 sysname 和配置
                host = node.get("hostip")
                port = node.get("port")

                if not host or not port:
                    print(f"Host IP or port missing for node with image_type '{image_type}'.")
                    continue

                try:
                    tn = telnetlib.Telnet(host, port, timeout=10)
                    tn.host = host  # 为了在 get_configuration_via_telnet 中使用
                    tn.port = port
                except Exception as e:
                    print(f"Failed to connect to {host}:{port} via Telnet: {e}")
                    continue

                # 获取 sysname 和配置
                config = self.get_configuration_via_telnet(tn, image_type)
                if config:
                    print(f"Successfully retrieved configuration from {host}:{port}")
                else:
                    print(f"Failed to retrieve configuration from {host}:{port}")

                tn.close()
            else:
                print(f"Unknown image_type '{image_type}' for node. Skipping.")


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
