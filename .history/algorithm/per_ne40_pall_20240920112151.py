import json
import telnetlib
import os
import argparse
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

class RouterTelnetManager:
    def __init__(self, telnet_info, max_threads=10):
        self.telnet_info = telnet_info
        self.sysnames = {}
        self.ospf_routes = {}
        self.max_threads = max_threads  # 最大并发线程数

    def get_sysname_and_routing_table(self, host, port):
        """通过 Telnet 获取节点的 sysname 和 OSPF 路由表信息"""
        try:
            tn = telnetlib.Telnet(host, port, timeout=10)

            # 发送一个空命令（如回车字符 '\n'），以确保接收到提示符
            tn.write(b'\n')

            sysname = None
            ospf_ip = None
            nqa_result = None
            while True:
                output = tn.read_until(b'>', timeout=5)
                if not output:
                    break
                lines = output.decode('ascii', errors='ignore').splitlines()

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

                    # 发送 scr 0 t 指令以确保可以正确输出路由表
                    tn.write(b'scr 0 t\n')
                    tn.read_until(b'>', timeout=3)

                    # 发送命令获取路由表
                    tn.write(b'display ip routing-table\n')
                    routing_output = tn.read_until(b'>', timeout=5).decode('ascii', errors='ignore')
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
        """配置并执行 NQA 测试，最多尝试 max_attempts 次获取 NQA 测试结果，返回性能指标"""
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
                    print(f"NQA test finished for {dest_ip} on attempt {attempt_count + 1}")
                    break

                attempt_count += 1
                print(f"Attempt {attempt_count}/{max_attempts} for NQA result on {dest_ip}...")

                if attempt_count >= max_attempts:
                    print(f"Max attempts reached for {dest_ip}. Test result may be incomplete.")
                    break

            # 记录并返回结果
            print(f"NQA Test Result for {dest_ip}:")
            print(result)

            # 执行结束和清理命令
            tn.write(b'stop\n')
            tn.read_until(b'>', timeout=3)

            tn.write(b'q\n')
            tn.read_until(b'>', timeout=3)

            tn.write(b'undo nqa test-instance admin perfor_test\n')
            tn.read_until(b']', timeout=3)

            tn.write(b'commit\n')
            tn.read_until(b']', timeout=3)

            # 解析并返回性能评估输入
            metrics = self.parse_nqa_result(result)
            if metrics is None:
                print(f"Failed to parse NQA results for {dest_ip}.")
            return metrics

        except Exception as e:
            print(f"Error during NQA test - {e}")
            return None

    def parse_nqa_result(self, nqa_result):
        """解析 NQA 结果，提取性能指标。"""
        metrics = {
            "latency": None,
            "jitter": None,
            "packet_loss": None
        }

        # 定义每个性能指标的正则表达式
        rtt_pattern = re.compile(r'Min/Max/Avg/Sum RTT:(\d+)/(\d+)/(\d+)/(\d+)')
        jitter_pattern = re.compile(r'Average of Jitter:\s*(\d+(\.\d+)?)')
        packet_loss_pattern = re.compile(r'Packet Loss Ratio:\s*(\d+(\.\d+)?)\s*%')

        for line in nqa_result.splitlines():
            line = line.strip()
            
            # 解析 RTT (延迟)
            rtt_match = rtt_pattern.search(line)
            if rtt_match:
                try:
                    latency_avg = float(rtt_match.group(3))  # 平均 RTT 是第3个捕获组
                    metrics["latency"] = latency_avg
                    print(f"解析延迟 (平均 RTT): {latency_avg} ms")
                except ValueError as e:
                    print(f"无法解析延迟，行: '{line}'。错误: {e}")
                    metrics["latency"] = None

            # 解析抖动 (Jitter)
            jitter_match = jitter_pattern.search(line)
            if jitter_match:
                try:
                    jitter_value = float(jitter_match.group(1))
                    metrics["jitter"] = jitter_value
                    print(f"解析抖动 (平均抖动): {jitter_value} ms")
                except ValueError as e:
                    print(f"无法解析抖动，行: '{line}'。错误: {e}")
                    metrics["jitter"] = None

            # 解析丢包率 (Packet Loss Ratio)
            packet_loss_match = packet_loss_pattern.search(line)
            if packet_loss_match:
                try:
                    packet_loss = float(packet_loss_match.group(1))
                    metrics["packet_loss"] = packet_loss
                    print(f"解析丢包率: {packet_loss} %")
                except ValueError as e:
                    print(f"无法解析丢包率，行: '{line}'。错误: {e}")
                    metrics["packet_loss"] = None

        # 如果所有指标都无法解析，记录原始 NQA 结果以便调试
        if all(value is None for value in metrics.values()):
            print("所有性能指标均为 None。原始 NQA 结果:")
            print(nqa_result)

        return metrics

    def evaluate_network_performance(self, metrics):
        """根据性能指标评估网络性能，并以中文表述结果。"""
        evaluation = {
            "latency": "未知",
            "jitter": "未知",
            "packet_loss": "未知",
            "overall_performance": "未知"
        }

        # 定义性能评估的阈值
        latency_thresholds = {"good": 50, "average": 100}
        jitter_thresholds = {"good": 20, "average": 50}
        packet_loss_thresholds = {"good": 1, "average": 5}

        # 评估延迟
        if metrics["latency"] is not None:
            if metrics["latency"] <= latency_thresholds["good"]:
                evaluation["latency"] = "良好"
            elif metrics["latency"] <= latency_thresholds["average"]:
                evaluation["latency"] = "中等"
            else:
                evaluation["latency"] = "差"

        # 评估抖动
        if metrics["jitter"] is not None:
            if metrics["jitter"] <= jitter_thresholds["good"]:
                evaluation["jitter"] = "良好"
            elif metrics["jitter"] <= jitter_thresholds["average"]:
                evaluation["jitter"] = "中等"
            else:
                evaluation["jitter"] = "差"

        # 评估丢包
        if metrics["packet_loss"] is not None:
            if metrics["packet_loss"] <= packet_loss_thresholds["good"]:
                evaluation["packet_loss"] = "良好"
            elif metrics["packet_loss"] <= packet_loss_thresholds["average"]:
                evaluation["packet_loss"] = "中等"
            else:
                evaluation["packet_loss"] = "差"

        # 综合评估
        metrics_values = [evaluation["latency"], evaluation["jitter"], evaluation["packet_loss"]]
        if all(v == "良好" for v in metrics_values):
            evaluation["overall_performance"] = "良好"
        elif any(v == "差" for v in metrics_values):
            evaluation["overall_performance"] = "差"
        elif any(v == "中等" for v in metrics_values):
            evaluation["overall_performance"] = "中等"

        return evaluation

    def generate_performance_summary(self, evaluation):
        """根据性能评估结果生成综合的网络性能评价。"""
        summaries = []
        
        # 延迟评价
        if evaluation["latency"] == "良好":
            summaries.append("延迟良好")
        elif evaluation["latency"] == "中等":
            summaries.append("延迟中等")
        elif evaluation["latency"] == "差":
            summaries.append("延迟较高")
        else:
            summaries.append("延迟未知")
        
        # 抖动评价
        if evaluation["jitter"] == "良好":
            summaries.append("抖动小")
        elif evaluation["jitter"] == "中等":
            summaries.append("抖动中等")
        elif evaluation["jitter"] == "差":
            summaries.append("抖动较大")
        else:
            summaries.append("抖动未知")
        
        # 丢包率评价
        if evaluation["packet_loss"] == "良好":
            summaries.append("丢包率低")
        elif evaluation["packet_loss"] == "中等":
            summaries.append("丢包率中等")
        elif evaluation["packet_loss"] == "差":
            summaries.append("丢包率较高")
        else:
            summaries.append("丢包率未知")
        
        # 综合评估
        if evaluation["overall_performance"] == "良好":
            overall = "网络性能良好。"
        elif evaluation["overall_performance"] == "中等":
            overall = "网络性能中等。"
        elif evaluation["overall_performance"] == "差":
            overall = "网络性能较差。"
        else:
            overall = "网络性能未知。"
        
        # 组合所有评价
        summary = "该设备网络性能" + "，".join(summaries) + "。" + overall
        return summary

    def process_router(self, node):
        """处理单个路由器的连接和测试，返回结果字典。"""
        host = node.get("hostip")
        port = node.get("port")
        result_data = {"host": host, "port": port, "sysname": None, "ospf_ip": None, "nqa_result": None, "performance_evaluation": None, "performance_summary": None}

        if not host or not port:
            print(f"无效的节点配置: {node}")
            result_data["performance_summary"] = "无效的节点配置。"
            return result_data

        sysname, ospf_ip, nqa_result = self.get_sysname_and_routing_table(host, port)
        if sysname:
            self.sysnames[f"{host}:{port}"] = sysname
            print(f"已连接到 {host}:{port} - Sysname: {sysname}")
            result_data["sysname"] = sysname

            if ospf_ip and nqa_result:
                self.ospf_routes[f"{host}:{port}"] = {"ospf_ip": ospf_ip, "nqa_result": nqa_result}
                print(f"{host}:{port} 的 OSPF 路由: {ospf_ip}")

                # 解析 NQA 测试结果并进行网络性能评估
                performance_metrics = nqa_result
                evaluation = self.evaluate_network_performance(performance_metrics)
                summary = self.generate_performance_summary(evaluation)

                result_data["ospf_ip"] = ospf_ip
                result_data["nqa_result"] = performance_metrics
                result_data["performance_evaluation"] = evaluation
                result_data["performance_summary"] = summary
            else:
                result_data["ospf_ip"] = ospf_ip if ospf_ip else None
                result_data["nqa_result"] = nqa_result if nqa_result else None
                result_data["performance_evaluation"] = None
                if not ospf_ip and not nqa_result:
                    result_data["performance_summary"] = "无法进行网络性能评估。"
                elif not ospf_ip:
                    result_data["performance_summary"] = "未找到 OSPF 路由，无法进行网络性能评估。"
                elif not nqa_result:
                    result_data["performance_summary"] = "无 NQA 测试结果，无法进行网络性能评估。"
        else:
            print(f"无法检索 {host}:{port} 的 sysname")
            result_data["performance_summary"] = "无法检索 sysname，无法进行网络性能评估。"

        return result_data

    def connect_and_get_sysnames_routes_and_nqa(self):
        """通过 Telnet 连接每个路由器，检索 sysname、OSPF 路由，执行 NQA 测试，并评估网络性能。"""
        results = []
        nodes = self.telnet_info.get("node", [])
        if not nodes:
            print("没有找到任何节点配置。")
            return results

        with ThreadPoolExecutor(max_workers=self.max_threads) as executor:
            # 提交所有路由器的处理任务
            future_to_node = {executor.submit(self.process_router, node): node for node in nodes}
            for future in as_completed(future_to_node):
                node = future_to_node[future]
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    print(f"处理节点 {node} 时发生错误: {e}")
                    results.append({
                        "host": node.get("hostip"),
                        "port": node.get("port"),
                        "sysname": None,
                        "ospf_ip": None,
                        "nqa_result": None,
                        "performance_evaluation": None,
                        "performance_summary": "处理过程中发生错误。"
                    })

        return results

def find_latest_folder(base_path):
    """在指定的基路径下按数字查找最新的文件夹。"""
    all_folders = [f for f in os.listdir(base_path) if f.isdigit()]
    if not all_folders:
        raise ValueError("基路径中未找到编号文件夹。")
    latest_folder = max(all_folders, key=int)
    return latest_folder

def main(input_path, output_path, max_threads=10):
    # 如果使用 {t}，则解析最新的文件夹编号
    base_path = "/uploadPath/reasoning"
    if "{t}" in input_path or "{t}" in output_path:
        latest_folder = find_latest_folder(base_path)
        input_path = input_path.replace("{t}", latest_folder)
        output_path = output_path.replace("{t}", latest_folder)

    # 加载 param.json
    try:
        with open(input_path, 'r') as f:
            telnet_info = json.load(f)
    except Exception as e:
        print(f"无法加载输入 JSON 文件 '{input_path}': {e}")
        return

    # 管理 Telnet 连接
    telnet_manager = RouterTelnetManager(telnet_info, max_threads=max_threads)
    results = telnet_manager.connect_and_get_sysnames_routes_and_nqa()

    # 输出结果到文件
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(json.dumps(results, indent=4, ensure_ascii=False))
        print(f"结果和性能评估已写入 {output_path}")
    except Exception as e:
        print(f"无法写入输出 JSON 文件 '{output_path}': {e}")

if __name__ == "__main__":
    # 设置参数解析
    parser = argparse.ArgumentParser(description="通过 Telnet 从路由器检索 sysname、OSPF 路由，并执行 NQA 测试以评估网络性能。")
    parser.add_argument("-i", "--input", required=True, help="param.json 的路径，使用 {t} 表示最新的文件夹编号。")
    parser.add_argument("-o", "--output", required=True, help="输出路径，用于存储处理信息，使用 {t} 表示最新的文件夹编号。")
    parser.add_argument("--max-threads", type=int, default=10, help="最大并发线程数（默认为 10）。")
    args = parser.parse_args()

    main(args.input, args.output, max_threads=args.max_threads)
