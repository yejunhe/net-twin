import xml.etree.ElementTree as ET
import json
import telnetlib
import os
import argparse
import subprocess
import sys
import time


class UNLParser: 
    def __init__(self, unl_file):
        self.unl_file = unl_file
        self.nodes = {}
        self.networks = {}

    def parse(self):
        """Parse the .unl file to extract node and network information."""
        try:
            tree = ET.parse(self.unl_file)
            root = tree.getroot()
        except ET.ParseError as e:
            print(f"Error parsing UNL file: {e}")
            sys.exit(1)
        except FileNotFoundError:
            print(f"UNL file not found at path: {self.unl_file}")
            sys.exit(1)

        # Extract node information
        for node in root.findall(".//node"):
            node_id = node.get('id')
            node_name = node.get('name')
            interfaces = []

            for interface in node.findall("interface"):
                interfaces.append({
                    'id': interface.get('id'),
                    'name': interface.get('name'),
                    'network_id': interface.get('network_id')
                })

            self.nodes[node_id] = {
                'name': node_name,
                'interfaces': interfaces
            }

        # Extract network information
        for network in root.findall(".//network"):
            network_id = network.get('id')
            network_name = network.get('name')
            self.networks[network_id] = network_name

        print("Parsed UNL File:")
        print("Nodes:", json.dumps(self.nodes, indent=4))
        print("Networks:", json.dumps(self.networks, indent=4))


class RouterManager:
    def __init__(self, telnet_info):
        self.telnet_info = telnet_info
        self.sysnames = {}
        self.configurations = {}

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
            elif "huaweine40" in image_type:
                # 对于 Huawei NE40 设备，使用专门的方法执行命令
                config_output = self.execute_telnet_commands_huaweine40(tn)
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
                self.sysnames[docker_id] = sysname
            else:
                print(f"Sysname not detected in Docker container {docker_id} output.")
                self.sysnames[docker_id] = "Unknown"

            # 删除后续命令执行逻辑

            self.configurations[docker_id] = output

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
                # 检查 'FR2#' 格式
                if line.endswith('#'):
                    sysname_candidate = line.split('#')[0]
                    if sysname_candidate:
                        sysname = sysname_candidate
                        break
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
                except Exception as e:
                    print(f"Failed to connect to {host}:{port} via Telnet: {e}")
                    continue

                # 获取 sysname
                sysname = self.get_sysname_via_telnet(tn)
                if sysname:
                    self.sysnames[f"{host}:{port}"] = sysname
                    # 执行后续命令，获取配置信息
                    config = self.get_configuration_via_telnet(tn, image_type)
                    if config:
                        self.configurations[f"{host}:{port}"] = config
                    else:
                        print(f"Failed to retrieve configuration from {host}:{port}")
                else:
                    print(f"Failed to retrieve sysname from {host}:{port}")

                tn.close()
            else:
                print(f"Unknown image_type '{image_type}' for node. Skipping.")


class TopologyMapper:
    def __init__(self, unl_parser, router_manager):
        self.unl_parser = unl_parser
        self.router_manager = router_manager

    def map_topology(self):
        """Map the sysnames and configurations retrieved to the UNL topology."""
        mapping = {}
        # Map Telnet-based routers
        for node_id, node_info in self.unl_parser.nodes.items():
            node_name = node_info['name']
            for host_port, sysname in self.router_manager.sysnames.items():
                if node_name == sysname:
                    mapping[node_id] = {
                        'node_name': node_name,
                        'host_port': host_port,
                        'sysname': sysname,
                        'configuration': self.router_manager.configurations.get(host_port, 'No config')
                    }

        # Map Docker-based routers
        for docker_id, sysname in self.router_manager.sysnames.items():
            if sysname and sysname != "Unknown":
                mapping[docker_id] = {
                    'docker_id': docker_id,
                    'sysname': sysname,
                    'configuration': self.router_manager.configurations.get(docker_id, 'No config')
                }
            elif sysname == "Unknown":
                mapping[docker_id] = {
                    'docker_id': docker_id,
                    'sysname': sysname,
                    'configuration': self.router_manager.configurations.get(docker_id, 'No config')
                }

        print("Mapping between UNL topology and router configurations:")
        print(json.dumps(mapping, indent=4))
        return mapping


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

    # Extract experiment ID and construct UNL file path
    lab_id = telnet_info.get("labId")
    if not lab_id:
        print("labId not found in param.json.")
        sys.exit(1)

    unl_file_path = f"/opt/unetlab/labs/{lab_id}.unl"

    if not os.path.exists(unl_file_path):
        print(f"UNL file does not exist at path: {unl_file_path}")
        sys.exit(1)

    # Parse the UNL file
    unl_parser = UNLParser(unl_file_path)
    unl_parser.parse()

    # Manage Telnet/SSH connections and retrieve sysnames and configurations
    router_manager = RouterManager(telnet_info)
    router_manager.connect_and_get_sysnames_and_configs()

    # Map topology
    mapper = TopologyMapper(unl_parser, router_manager)
    mapping = mapper.map_topology()

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
    parser = argparse.ArgumentParser(description="Process network topology from UNL and param.json.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json, use {t} for latest folder number.")
    parser.add_argument("-o", "--output", required=True, help="Output path for process information, use {t} for latest folder number.")
    args = parser.parse_args()

    main(args.input, args.output)
