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

    def perform_nqa_test(self, tn, dest_ip, max_attempts=5):
        """配置并执行 NQA 测试，最多尝试 max_attempts 次获取 NQA 测试结果，返回格式化的测试结果"""
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
                b'timeout 1\n'
            ]
            for cmd in nqa_commands:
                tn.write(cmd)
                tn.read_until(b']', timeout=3)

            # 发送命令开始测试
            tn.write(b'start now\n')
            tn.read_until(b']', timeout=3)

            # 确保配置提交
            tn.write(b'commit\n')
            tn.read_until(b']', timeout=3)

            # 尝试获取 NQA 测试结果
            attempt_count = 0
            result = ""
            while attempt_count < max_attempts:
                tn.write(b'display nqa results test-instance admin perfor_test\n')
                partial_output = tn.read_until(b'>', timeout=5).decode('ascii')
                result += partial_output

                if "The test is finished" in partial_output:
                    print(f"NQA 测试完成，目的地址：{dest_ip}，尝试次数：{attempt_count + 1}")
                    break

                attempt_count += 1
                print(f"第 {attempt_count}/{max_attempts} 次尝试获取 NQA 结果，目的地址：{dest_ip}...")

                if attempt_count >= max_attempts:
                    print(f"达到最大尝试次数，目的地址：{dest_ip}，测试结果可能不完整。")
                    break

        # 将结果整理为字典
            nqa_data = self.format_nqa_result(result)
            print(f"NQA 测试结果，目的地址：{dest_ip} - {nqa_data}")

            # 检测到测试结果后，停止测试并执行清理
            tn.write(b'stop\n')
            tn.read_until(b']', timeout=3)

            tn.write(b'q\n')  # 退出当前视图
            tn.read_until(b'>', timeout=3)

            # 删除 NQA 测试实例
            tn.write(b'undo nqa test-instance admin perfor_test\n')
            tn.read_until(b']', timeout=3)

            # 提交更改
            tn.write(b'commit\n')
            tn.read_until(b']', timeout=3)

            # Perform performance analysis based on NQA data
            analysis = self.analyze_nqa_performance(nqa_data)
            print(f"NQA 性能分析，目的地址：{dest_ip}：\n{analysis}")
            
            # Append analysis to the NQA data dictionary
            nqa_data['分析结果'] = analysis

            return nqa_data

        except Exception as e:
            print(f"NQA 测试过程中出现错误 - {e}")
            return None

    def analyze_nqa_performance(self, nqa_data):
        """对 NQA 测试结果进行分析，并返回中文分析结果及综合评价字符串"""
        analysis = []

        # RTT (Round Trip Time) Analysis
        rtt_min = int(nqa_data.get("Min/Max/Avg/Sum RTT", "").split('/')[0])
        rtt_max = int(nqa_data.get("Min/Max/Avg/Sum RTT", "").split('/')[1])
        rtt_avg = int(nqa_data.get("Min/Max/Avg/Sum RTT", "").split('/')[2])

        analysis.append(f"往返时延分析：最小 RTT = {rtt_min}ms，最大 RTT = {rtt_max}ms，平均 RTT = {rtt_avg}ms")
        
        # Packet Loss Analysis
        packet_loss_ratio = nqa_data.get("Packet Loss Ratio", "0 %").replace(" %", "")
        packet_loss_ratio = float(packet_loss_ratio)

        if packet_loss_ratio > 0:
            analysis.append(f"检测到丢包，丢包率 = {packet_loss_ratio}%")
        else:
            analysis.append(f"未检测到丢包，丢包率 = {packet_loss_ratio}%")

        # Jitter Analysis
        avg_jitter_sd = float(nqa_data.get("Average of Jitter SD", "0"))
        avg_jitter_ds = float(nqa_data.get("Average of Jitter DS", "0"))
        
        analysis.append(f"抖动分析：平均抖动 SD = {avg_jitter_sd}ms，平均抖动 DS = {avg_jitter_ds}ms")

        # 综合评价
        evaluation = "综合评价："
        if rtt_avg <= 50 and packet_loss_ratio == 0 and avg_jitter_sd <= 5:
            evaluation += "网络性能优秀，时延、丢包率和抖动均处于理想范围内。"
        elif rtt_avg <= 100 and packet_loss_ratio <= 1 and avg_jitter_sd <= 10:
            evaluation += "网络性能良好，适用于大部分应用场景。"
        else:
            evaluation += "网络性能较差，可能会影响实时应用或高带宽需求的服务。"

        analysis.append(evaluation)

        return "\n".join(analysis)

    def format_nqa_result(self, result):
        """将原始 NQA 测试结果进行格式化并返回为字典"""
        formatted_result = {}
        lines = result.splitlines()

        for line in lines:
            # 移除前后的空格
            line = line.strip()

            if ':' in line:
                # 将每一行结果分为键值对，并确保删除多余的空格
                key_value = line.split(':', 1)
                key = key_value[0].strip()
                value = key_value[1].strip()

                # 对于数值，尝试转换为浮点或整数，并捕获异常以避免程序中断
                try:
                    if value.isdigit():
                        value = int(value)
                    else:
                        value = float(value)
                except ValueError:
                    # 如果无法转换为数字，则保留为字符串
                    pass

                formatted_result[key] = value

        return formatted_result

    def connect_and_get_sysnames_routes_and_nqa(self):
        """Connect to each router via Telnet, retrieve sysname, OSPF route, and perform NQA test."""
        results = []
        for node in self.telnet_info["node"]:
            host = node["hostip"]
            port = node["port"]
            sysname, ospf_ip, nqa_result = self.get_sysname_and_routing_table(host, port)
            if sysname:
                self.sysnames[host + ':' + str(port)] = sysname
                result_data = {"host": host, "port": port, "sysname": sysname}

                if ospf_ip:
                    self.ospf_routes[host + ':' + str(port)] = {"ospf_ip": ospf_ip, "nqa_result": nqa_result}
                    result_data["ospf_ip"] = ospf_ip
                    result_data["nqa_result"] = nqa_result
                else:
                    result_data["ospf_ip"] = None
                    result_data["nqa_result"] = None

                # 将每个节点的结果追加到results列表
                results.append(result_data)

            else:
                print(f"Failed to retrieve sysname for {host}:{port}")

        return results


def find_latest_folder(base_path):
    """查找指定目录中最新的文件夹"""
    folders = [f for f in os.listdir(base_path) if os.path.isdir(os.path.join(base_path, f))]
    folders.sort(key=lambda x: os.path.getmtime(os.path.join(base_path, x)), reverse=True)
    return folders[0] if folders else None


def main(input_path, output_path):
    # 从给定的路径读取 param.json 文件
    with open(input_path, 'r') as f:
        telnet_info = json.load(f)

    # 管理 Telnet 连接
    telnet_manager = RouterTelnetManager(telnet_info)
    results = telnet_manager.connect_and_get_sysnames_routes_and_nqa()

    # 输出结果到文件
    with open(output_path, 'w') as f:
        f.write(json.dumps(results, indent=4))
    print(f"Results written to {output_path}")


if __name__ == "__main__":
    # 设置参数解析
    parser = argparse.ArgumentParser(description="Retrieve sysnames, OSPF routes, and perform NQA test via Telnet from routers.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json.")
    parser.add_argument("-o", "--output", required=True, help="Output path for process information.")
    args = parser.parse_args()

    # 调用主函数
    main(args.input, args.output)