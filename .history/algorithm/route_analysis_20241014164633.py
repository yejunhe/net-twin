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
            output = tn.read_until(b'>', timeout=5)
            lines = output.decode('ascii').splitlines()
            sysname = None
            for line in lines:
                if line.startswith('<') and line.endswith('>'):
                    sysname = line.strip('<> ')
                    break
            return sysname if sysname else None
        except Exception as e:
            print(f"Telnet Error while getting sysname: {e}")
            return None

    def execute_telnet_commands(self, tn, image_type):
        """
        Execute a list of commands on a Telnet connection based on image_type.
        :param tn: telnetlib.Telnet object
        :param image_type: Type of the image ('h3c' or 'huaweine40')
        :return: Output from the Telnet session
        """
        try:
            output = ""

            # 先发送回车指令，确保连接稳定
            tn.write(b'\n')  # 发送回车
            time.sleep(1)  # 等待命令执行
            output += tn.read_very_eager().decode('ascii')  # 获取回车后的输出
            print(f"Sent newline command, output: {output}")

            # 判断是否匹配 h3c 或 huaweine40
            if "h3c" in image_type:
                while True:
                    # 读取并处理 Telnet 输出
                    data = tn.read_until(b'\n', timeout=5).decode('ascii').strip()
                    output += data + "\n"
                    print(f"Telnet Output: {data}")
                    
                    # 当出现 "]" 提示时发送 'quit'，">" 提示时发送 'screen-length disable'
                    if data.endswith("]"):
                        tn.write(b'quit\n')
                        print("Sending 'quit' command...")
                    elif data.endswith(">"):
                        tn.write(b'screen-length disable\n')
                        print("Sending 'screen-length disable' command...")
                        break  # 退出循环，执行完后续命令
                        
            elif "huaweine40" in image_type:
                while True:
                    data = tn.read_until(b'\n', timeout=5).decode('ascii').strip()
                    output += data + "\n"
                    print(f"Telnet Output: {data}")
                    
                    # 当出现 "]" 提示时发送 'q'，">" 提示时发送 'scr 0 t'
                    if data.endswith("]"):
                        tn.write(b'q\n')
                        print("Sending 'q' command...")
                    elif data.endswith(">"):
                        tn.write(b'scr 0 t\n')
                        print("Sending 'scr 0 t' command...")
                        break  # 退出循环，执行完后续命令
            
            # 让 Telnet 执行完前面的命令
            time.sleep(1)
            
            # 执行完 scr 0 t 或 screen-length disable 之后再执行 display ip routing-table
            tn.write(b'display ip routing-table\n')
            print("Sending 'display ip routing-table' command...")
            routing_table_output = tn.read_until(b'>', timeout=10).decode('ascii')
            output += routing_table_output
            
            return output
        
        except Exception as e:
            print(f"Telnet Error while executing commands: {e}")
            return None

    def get_configuration_via_telnet(self, tn, image_type):
        """通过 Telnet 获取路由器的配置信息"""
        try:
            # Execute the necessary command sequence based on image_type
            output = self.execute_telnet_commands(tn, image_type)
            if not output:
                print("No output received from Telnet commands.")
                return None

            # Now, send 'display ip routing-table' and capture its output
            tn.write(b'display ip routing-table\n')
            print("Sending 'display ip routing-table' command...")
            config_output = tn.read_until(b'>', timeout=10).decode('ascii')
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

            # 执行命令: vtysh 然后 terminal length 0
            commands = [
                'vtysh -c "terminal length 0"',
                'vtysh -c "show ip ospf route"'
            ]
            output = self.execute_docker_commands(container_id, commands)
            print(f"Docker command output:\n{output}")
            self.configurations[docker_id] = output
            self.sysnames[docker_id] = "Connected via Docker"
            return True
        except subprocess.CalledProcessError as e:
            print(f"Docker Connection Error: {e.output.decode('utf-8')}")
            return False
        except Exception as e:
            print(f"Docker Connection Error: {e}")
            return False

     def connect_and_get_sysnames_and_configs(self):
        """Connect to each router and retrieve sysnames and configurations based on image_type."""
        for node in self.telnet_info.get("node", []):
            image_type = node.get("image_type", "").lower()

            # 匹配包含 h3c 或 huaweine40 的镜像
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
                else:
                    print(f"Failed to retrieve sysname from {host}:{port}")

                # 获取配置信息
                config = self.get_configuration_via_telnet(tn, image_type)
                if config:
                    self.configurations[f"{host}:{port}"] = config
                else:
                    print(f"Failed to retrieve configuration from {host}:{port}")

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
        for docker_id, status in self.router_manager.sysnames.items():
            if status == "Connected via Docker":
                mapping[docker_id] = {
                    'docker_id': docker_id,
                    'status': status,
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