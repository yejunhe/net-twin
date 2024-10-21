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

    def execute_telnet_commands(self, tn, image_type):
        """
        Execute a list of commands on a Telnet connection based on image_type.
        :param tn: telnetlib.Telnet object
        :param image_type: Type of the image ('h3c' or 'huaweine40')
        :return: Output from the Telnet session
        """
        try:
            output = ""

            #    确保已经发送回车
            tn.write(b'\n')  # 发送回车
            time.sleep(1)  # 等待执行
            output += tn.read_very_eager().decode('ascii')  # 获取回车后的输出
            print(f"Sent newline command, output: {output}")

            # 根据 image_type 判断发送的命令
            if "h3c" in image_type:
                while True:
                    # 读取 Telnet 输出
                    data = tn.read_until(b'\n', timeout=5).decode('ascii').strip()
                    output += data + "\n"
                    print(f"Telnet Output: {data}")
                
                    # 根据提示符发送命令
                    if data.endswith("]"):
                        tn.write(b'quit\n')
                        print("Sending 'quit' command...")
                    elif data.endswith(">"):
                        tn.write(b'screen-length disable\n')
                        print("Sending 'screen-length disable' command...")
                        break  # 完成之后退出循环
                    
            elif "huaweine40" in image_type:
                while True:
                    data = tn.read_until(b'\n', timeout=5).decode('ascii').strip()
                    output += data + "\n"
                    print(f"Telnet Output: {data}")
                
                    # 根据提示符发送命令
                    if data.endswith("]"):
                        tn.write(b'q\n')
                        print("Sending 'q' command...")
                    elif data.endswith(">"):
                        tn.write(b'scr 0 t\n')
                        print("Sending 'scr 0 t' command...")
                        break  # 完成之后退出循环
        
            # 让命令执行完毕
            time.sleep(1)
        
            # 执行 display ip routing-table
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
                    # 执行后续命令
                    config = self.execute_telnet_commands(tn, image_type)
                    if config:
                        self.configurations[f"{host}:{port}"] = config
                tn.close()

def find_latest_folder(base_path):
    """找到 base_path 下最新的按编号命名的文件夹"""
    try:
        subfolders = [f for f in os.listdir(base_path) if os.path.isdir(os.path.join(base_path, f))]
        subfolders = [f for f in subfolders if f.isdigit()]  # 筛选数字命名的文件夹
        subfolders = sorted(subfolders, key=int)
        if subfolders:
            return os.path.join(base_path, subfolders[-1])
        else:
            return None
    except Exception as e:
        print(f"Error finding latest folder in {base_path}: {e}")
        return None

def main(input_path, output_path):
    # 加载 param.json 文件
    try:
        with open(input_path, 'r') as f:
            params = json.load(f)
    except FileNotFoundError:
        print(f"Error: File '{input_path}' not found.")
        return
    except json.JSONDecodeError as e:
        print(f"Error: Failed to decode JSON in file '{input_path}'. Error: {e}")
        return

    # 创建 RouterManager 实例
    router_manager = RouterManager(params)
    
    # 获取 sysname 和配置信息
    router_manager.connect_and_get_sysnames_and_configs()

    # 将结果保存到指定路径
    result = {
        "sysnames": router_manager.sysnames,
        "configurations": router_manager.configurations
    }

    with open(output_path, 'w') as f:
        json.dump(result, f, indent=4)

    print(f"Results saved to {output_path}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Process network topology and retrieve router configurations.')
    parser.add_argument('--input', required=True, help='Path to the param.json file')
    parser.add_argument('--output', required=True, help='Output file path for saving results')
    args = parser.parse_args()

    main(args.input, args.output)
