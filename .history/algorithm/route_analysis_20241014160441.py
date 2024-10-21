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
        """根据 image_type 执行 Telnet 命令"""
        try:
            output = ""
            while True:
                data = tn.read_until(b'\n', timeout=5).decode('ascii').strip()
                output += data + "\n"
                print(f"Telnet Output: {data}")

                if image_type == "h3c":
                    if data.endswith("]"):
                        tn.write(b'quit\n')
                        print("Sending 'quit' command...")
                    elif data.endswith(">"):
                        tn.write(b'screen-length disable\n')
                        print("Sending 'screen-length disable' command...")
                        break
                elif image_type == "huaweine40":
                    if data.endswith("]"):
                        tn.write(b'q\n')
                        print("Sending 'q' command...")
                    elif data.endswith(">"):
                        tn.write(b'scr 0 t\n')
                        print("Sending 'scr 0 t' command...")
                        break
                else:
                    print(f"Unsupported image_type: {image_type}")
                    break

            time.sleep(1)
            final_output = tn.read_until(b'>', timeout=10).decode('ascii')
            output += final_output
            return output
        except Exception as e:
            print(f"Telnet Error while executing commands: {e}")
            return None

    def get_configuration_via_telnet(self, tn, image_type):
        """通过 Telnet 获取路由器的配置信息"""
        try:
            output = self.execute_telnet_commands(tn, image_type)
            if not output:
                print("No output received from Telnet commands.")
                return None

            tn.write(b'display ip routing-table\n')
            print("Sending 'display ip routing-table' command...")
            config_output = tn.read_until(b'>', timeout=10).decode('ascii')
            return config_output
        except Exception as e:
            print(f"Telnet Error while getting configuration: {e}")
            return None

    def execute_docker_commands(self, container_id, commands):
        """在 Docker 容器内执行命令"""
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
        """通过 Docker 连接到 FRR 路由器并执行命令"""
        try:
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
        """连接到每个路由器并获取 sysnames 和配置"""
        for node in self.telnet_info.get("node", []):
            image_type = node.get("image_type", "").lower()

            if "frrouting" in image_type:
                docker_id = node.get("dockerid")
                if docker_id:
                    self.connect_via_docker(docker_id)
                else:
                    print(f"No Docker ID provided for node with image_type 'frrouting'.")
            elif "h3c" in image_type or "huaweine40" in image_type:
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

                sysname = self.get_sysname_via_telnet(tn)
                if sysname:
                    self.sysnames[f"{host}:{port}"] = sysname
                else:
                    print(f"Failed to retrieve sysname from {host}:{port}")

                config = self.get_configuration_via_telnet(tn, image_type)
                if config:
                    self.configurations[f"{host}:{port}"] = config
                else:
                    print(f"Failed to retrieve configuration from {host}:{port}")

                tn.close()


def main(input_path, output_path):
    try:
        with open(input_path, 'r') as f:
            telnet_info = json.load(f)
    except FileNotFoundError:
        print(f"param.json file not found at path: {input_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON from param.json: {e}")
        sys.exit(1)

    router_manager = RouterManager(telnet_info)
    router_manager.connect_and_get_sysnames_and_configs()

    try:
        with open(output_path, 'w') as f:
            f.write(json.dumps(router_manager.configurations, indent=4))
        print(f"Configuration results written to {output_path}")
    except IOError as e:
        print(f"Error writing to output file: {e}")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Retrieve router configurations via Telnet or Docker.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json.")
    parser.add_argument("-o", "--output", required=True, help="Output path for configuration results.")
    args = parser.parse_args()

    main(args.input, args.output)
