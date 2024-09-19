import json
import telnetlib
import os
import argparse


class RouterTelnetManager:
    def __init__(self, telnet_info):
        self.telnet_info = telnet_info
        self.sysnames = {}
        self.ospf_routes = {}

    def get_sysname_and_routing_table(self, host, port):
        """通过 Telnet 获取节点的 sysname 和 OSPF 路由表信息"""
        try:
            tn = telnetlib.Telnet(host, port, timeout=10)

            # 发送一个空命令（如回车字符 '\n'），以确保接收到提示符
            tn.write(b'\n')

            sysname = None
            while True:
                output = tn.read_until(b'>', timeout=5)
                lines = output.decode('ascii').splitlines()

                # 检查 sysname
                for line in lines:
                    line = line.strip()
                    if line.startswith('[') and line.endswith(']'):
                        tn.write(b'q\n')
                    elif line.startswith('<') and line.endswith('>'):
                        sysname = line.strip('<> ')
                        break

                if sysname:
                    print(f"Sysname for {host}:{port} is {sysname}")

                    # 发送 `scr 0 t` 指令以确保可以正确输出路由表
                    tn.write(b'scr 0 t\n')
                    tn.read_until(b'>', timeout=3)

                    # 发送命令获取路由表
                    tn.write(b'display ip routing-table\n')
                    routing_output = tn.read_until(b'>', timeout=5).decode('ascii')
                    ospf_ip = self.parse_routing_table(routing_output)

                    if ospf_ip:
                        # 配置并执行 NQA 测试
                        nqa_result = self.perform_nqa_test(tn, ospf_ip)
                        tn.close()
                        return sysname, ospf_ip, nqa_result
                    else:
                        tn.close()
                        return sysname, None, None

                else:
                    print(f"Waiting for correct prompt from {host}:{port}...")

        except Exception as e:
            print(f"Error connecting to {host}:{port} - {e}")
            return None, None, None

    def parse_routing_table(self, routing_table):
        """解析路由表，找到第一次出现 OSPF 的目的地址，并去掉子网掩码"""
        for line in routing_table.splitlines():
            if 'OSPF' in line:
                parts = line.split()
                if parts:
                    # 提取目的地址并去除子网掩码（如果有）
                    dest_ip = parts[0].split('/')[0]
                    print(f"Found OSPF route: {dest_ip}")
                    return dest_ip
        print("No OSPF route found")
        return None

    def perform_nqa_test(self, tn, dest_ip):
        """配置并执行 NQA 测试，返回测试结果"""
        try:
            # 进入 system-view 模式
            tn.write(b'system-view\n')
            tn.read_until(b']', timeout=3)

            # 配置 NQA 测试实例
            nqa_commands = [
                b'nqa test-instance admin perfor_test\n',
                b'test-type icmpjitter\n',
                f'destination-address ipv4 {dest_ip}\n'.encode('ascii'),
                b'probe-count 2\n',
                b'interval milliseconds 100\n',
                b'timeout 1\n',
                b'frequency 0\n',
                b'start now\n',
                b'commit\n'
            ]
            for cmd in nqa_commands:
                tn.write(cmd)
                tn.read_until(b']', timeout=3)

            # 获取 NQA 测试结果
            tn.write(b'display nqa results test-instance admin perfor_test\n')
            result = tn.read_until(b'>', timeout=5).decode('ascii')
            print(f"NQA Test Result for {dest_ip}: {result}")

            return result
        except Exception as e:
            print(f"Error during NQA test - {e}")
            return None

    def connect_and_get_sysnames_routes_and_nqa(self):
        """Connect to each router via Telnet, retrieve sysname, OSPF route, and perform NQA test."""
        for node in self.telnet_info["node"]:
            host = node["hostip"]
            port = node["port"]
            sysname, ospf_ip, nqa_result = self.get_sysname_and_routing_table(host, port)
            if sysname:
                self.sysnames[host + ':' + str(port)] = sysname
                print(f"Connected to {host}:{port} - Sysname: {sysname}")
                if ospf_ip:
                    self.ospf_routes[host + ':' + str(port)] = {"ospf_ip": ospf_ip, "nqa_result": nqa_result}
                    print(f"OSPF route for {host}:{port}: {ospf_ip}")
                else:
                    print(f"No OSPF route found for {host}:{port}")
            else:
                print(f"Failed to retrieve sysname for {host}:{port}")


def find_latest_folder(base_path):
    """Find the latest folder by number under the given base path."""
    all_folders = [f for f in os.listdir(base_path) if f.isdigit()]
    if not all_folders:
        raise ValueError("No numbered folders found in the base path.")
    latest_folder = max(all_folders, key=int)
    return latest_folder


def main(input_path, output_path):
    # Resolve the latest folder number if {t} is used
    base_path = "/uploadPath/reasoning"
    if "{t}" in input_path or "{t}" in output_path:
        latest_folder = find_latest_folder(base_path)
        input_path = input_path.replace("{t}", latest_folder)
        output_path = output_path.replace("{t}", latest_folder)

    # Load param.json
    with open(input_path, 'r') as f:
        telnet_info = json.load(f)

    # Manage Telnet connections
    telnet_manager = RouterTelnetManager(telnet_info)
    telnet_manager.connect_and_get_sysnames_routes_and_nqa()

    # Output results to file
    results = {
        "sysnames": telnet_manager.sysnames,
        "ospf_routes": telnet_manager.ospf_routes
    }
    with open(output_path, 'w') as f:
        f.write(json.dumps(results, indent=4))
    print(f"Results written to {output_path}")


if __name__ == "__main__":
    # Set up argument parsing
    parser = argparse.ArgumentParser(description="Retrieve sysnames, OSPF routes, and perform NQA test via Telnet from routers.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json, use {t} for latest folder number.")
    parser.add_argument("-o", "--output", required=True, help="Output path for process information, use {t} for latest folder number.")
    args = parser.parse_args()

    main(args.input, args.output)
