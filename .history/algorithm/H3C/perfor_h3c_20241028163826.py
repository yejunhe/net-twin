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

                    # 发送 screen-length disable 指令以确保可以正确输出路由表
                    tn.write(b'screen-length disable\n')
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
        """
        解析路由表，找到第一次出现 IS_L1、O_INTRA 或 BGP 的目的地址，并去掉子网掩码。

        优先级按照在路由表中出现的顺序，不是固定的优先级。
        """
        # 定义要查找的路由类型
        route_types = ['IS_L1', 'O_INTRA', 'BGP']

        for line in routing_table.splitlines():
            line = line.strip()
            # 检查每种路由类型
            for route_type in route_types:
                if route_type in line:
                    parts = line.split()
                    if parts:
                        # 提取目的地址并去除子网掩码（如果有）
                        dest_ip = parts[0].split('/')[0]
                        print(f"Found {route_type} route: {dest_ip}")
                        return dest_ip
        print("未找到 IS_L1、O_INTRA 或 BGP 路由")
        return None

    def perform_nqa_test(self, tn, dest_ip, max_attempts=5):
        """配置并执行 NQA 测试，最多尝试 max_attempts 次获取 NQA 测试结果，返回性能指标"""
        try:
            # 进入 system-view 模式
            tn.write(b'system-view\n')
            tn.read_until(b']', timeout=3)

            # 配置 NQA 测试实例
            nqa_commands = [
                b'nqa entry admin perfor_test\n',
                b'type icmp-jitter\n',
                f'destination ip {dest_ip}\n'.encode('ascii'),
                b'frequency 100\n',
                b'quit\n',
                
            ]
            for cmd in nqa_commands:
                tn.write(cmd)
                tn.read_until(b']', timeout=3)
            
            # 发送命令开始测试
            tn.write(b'nqa schedule admin perfor_test start-time now lifetime forever\n')
            tn.read_until(b']', timeout=3)

            # 尝试获取 NQA 测试结果
            attempt_count = 0
            result = ""
            while attempt_count < max_attempts:
                tn.write(b'display nqa result admin perfor_test\n')
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
            tn.write(b'undo nqa schedule admin perfor_test\n')
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
            "延迟": None,
            "抖动": None,
            "丢包率": None
        }

        # 定义每个性能指标的正则表达式
        rtt_pattern = re.compile(r'Min/Max/Average round trip time:\s*(\d+)/(\d+)/(\d+)')
        packet_loss_pattern = re.compile(r'Packet loss ratio:\s*(\d+(\.\d+)?)%')
        jitter_avg_pattern = re.compile(r'Positive SD average:\s*(\d+(\.\d+)?)')
        
        # 新增的正则表达式
        one_way_max_sd_pattern = re.compile(r'Max SD delay:\s*(\d+)')
        one_way_min_sd_pattern = re.compile(r'Min SD delay:\s*(\d+)')
        
        for line in nqa_result.splitlines():
            line = line.strip()
            
            # 解析 RTT (延迟)
            rtt_match = rtt_pattern.search(line)
            if rtt_match:
                try:
                    latency_avg = float(rtt_match.group(3))  # 平均 RTT 是第3个捕获组
                    metrics["延迟"] = latency_avg
                    print(f"解析延迟 (平均 RTT): {latency_avg} ms")
                except ValueError as e:
                    print(f"无法解析延迟，行: '{line}'。错误: {e}")
                    metrics["延迟"] = None

            # 解析丢包率 (Packet Loss Ratio)
            packet_loss_match = packet_loss_pattern.search(line)
            if packet_loss_match:
                try:
                    packet_loss = float(packet_loss_match.group(1))
                    metrics["丢包率"] = packet_loss
                    print(f"解析丢包率: {packet_loss} %")
                except ValueError as e:
                    print(f"无法解析丢包率，行: '{line}'。错误: {e}")
                    metrics["丢包率"] = None

            # 解析抖动 (Jitter)
            jitter_avg_match = jitter_avg_pattern.search(line)
            if jitter_avg_match:
                try:
                    jitter_value = float(jitter_avg_match.group(1))
                    metrics["抖动"] = jitter_value
                    print(f"解析抖动 (平均抖动): {jitter_value} ms")
                except ValueError as e:
                    print(f"无法解析抖动，行: '{line}'。错误: {e}")
                    metrics["抖动"] = None

            # 解析单向延迟的最大和最小延迟
            one_way_max_sd_match = one_way_max_sd_pattern.search(line)
            if one_way_max_sd_match:
                print(f"解析最大单向延迟: {one_way_max_sd_match.group(1)} ms")
            
            one_way_min_sd_match = one_way_min_sd_pattern.search(line)
            if one_way_min_sd_match:
                print(f"解析最小单向延迟: {one_way_min_sd_match.group(1)} ms")

        # 如果所有指标都无法解析，记录原始 NQA 结果以便调试
        if all(value is None for value in metrics.values()):
            print("所有性能指标均为 None。原始 NQA 结果:")
            print(nqa_result)

        return metrics

    def evaluate_network_performance(self, metrics):
        """根据性能指标评估网络性能，并以中文表述结果。"""
        evaluation = {
            "延迟": "未知",
            "抖动": "未知",
            "丢包率": "未知",
            "节点性能": "未知"
        }

        # 定义性能评估的阈值
        latency_thresholds = {"good": 50, "average": 100}
        jitter_thresholds = {"good": 20, "average": 50}
        packet_loss_thresholds = {"good": 1, "average": 5}

        # 评估延迟
        if metrics["延迟"] is not None:
            if metrics["延迟"] <= latency_thresholds["good"]:
                evaluation["延迟"] = "良好"
            elif metrics["延迟"] <= latency_thresholds["average"]:
                evaluation["延迟"] = "中等"
            else:
                evaluation["延迟"] = "差"

        # 评估抖动
        if metrics["抖动"] is not None:
            if metrics["抖动"] <= jitter_thresholds["good"]:
                evaluation["抖动"] = "良好"
            elif metrics["抖动"] <= jitter_thresholds["average"]:
                evaluation["抖动"] = "中等"
            else:
                evaluation["抖动"] = "差"

        # 评估丢包
        if metrics["丢包率"] is not None:
            if metrics["丢包率"] <= packet_loss_thresholds["good"]:
                evaluation["丢包率"] = "良好"
            elif metrics["丢包率"] <= packet_loss_thresholds["average"]:
                evaluation["丢包率"] = "中等"
            else:
                evaluation["丢包率"] = "差"

        # 综合评估
        metrics_values = [evaluation["延迟"], evaluation["抖动"], evaluation["丢包率"]]
        if all(v == "良好" for v in metrics_values):
            evaluation["节点性能"] = "良好"
        elif any(v == "差" for v in metrics_values):
            evaluation["节点性能"] = "差"
        elif any(v == "中等" for v in metrics_values):
            evaluation["节点性能"] = "中等"

        return evaluation

    def generate_performance_summary(self, evaluation):
        """根据性能评估结果生成综合的网络性能评价。"""
        summaries = []
        
        # 延迟评价
        if evaluation["延迟"] == "良好":
            summaries.append("延迟良好")
        elif evaluation["延迟"] == "中等":
            summaries.append("延迟中等")
        elif evaluation["延迟"] == "差":
            summaries.append("延迟较高")
        else:
            summaries.append("延迟未知")
        
        # 抖动评价
        if evaluation["抖动"] == "良好":
            summaries.append("抖动小")
        elif evaluation["抖动"] == "中等":
            summaries.append("抖动中等")
        elif evaluation["抖动"] == "差":
            summaries.append("抖动较大")
        else:
            summaries.append("抖动未知")
        
        # 丢包率评价
        if evaluation["丢包率"] == "良好":
            summaries.append("丢包率低")
        elif evaluation["丢包率"] == "中等":
            summaries.append("丢包率中等")
        elif evaluation["丢包率"] == "差":
            summaries.append("丢包率较高")
        else:
            summaries.append("丢包率未知")
        
        # 综合评估
        if evaluation["节点性能"] == "良好":
            overall = "网络性能良好。"
        elif evaluation["节点性能"] == "中等":
            overall = "网络性能中等。"
        elif evaluation["节点性能"] == "差":
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
        result_data = {
            "host": host,
            "port": port,
            "sysname": None,
            "目的IP": None,
            "nqa_result": None,
            "性能评估": None,
            "性能总结": None
        }

        if not host or not port:
            print(f"无效的节点配置: {node}")
            result_data["性能总结"] = "无效的节点配置。"
            return result_data

        sysname, ospf_ip, nqa_result = self.get_sysname_and_routing_table(host, port)
        if sysname:
            self.sysnames[f"{host}:{port}"] = sysname
            print(f"已连接到 {host}:{port} - Sysname: {sysname}")
            result_data["sysname"] = sysname

            if ospf_ip and nqa_result:
                self.ospf_routes[f"{host}:{port}"] = {"目的IP": ospf_ip, "nqa_result": nqa_result}
                print(f"{host}:{port} 的 OSPF 路由: {ospf_ip}")

                # 解析 NQA 测试结果并进行网络性能评估
                performance_metrics = nqa_result
                evaluation = self.evaluate_network_performance(performance_metrics)
                summary = self.generate_performance_summary(evaluation)

                result_data["目的IP"] = ospf_ip
                result_data["nqa_result"] = performance_metrics
                result_data["性能评估"] = evaluation
                result_data["性能总结"] = summary
            else:
                result_data["目的IP"] = ospf_ip if ospf_ip else None
                result_data["nqa_result"] = nqa_result if nqa_result else None
                result_data["性能评估"] = None
                if not ospf_ip and not nqa_result:
                    result_data["性能总结"] = "无法进行网络性能评估。"
                elif not ospf_ip:
                    result_data["性能总结"] = "未找到 OSPF 路由，无法进行网络性能评估。"
                elif not nqa_result:
                    result_data["性能总结"] = "无 NQA 测试结果，无法进行网络性能评估。"
        else:
            print(f"无法检索 {host}:{port} 的 sysname")
            result_data["性能总结"] = "无法检索 sysname，无法进行网络性能评估。"

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
                        "目的IP": None,
                        "nqa_result": None,
                        "性能评估": None,
                        "性能总结": "处理过程中发生错误。"
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
