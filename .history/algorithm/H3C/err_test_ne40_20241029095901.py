import json
import telnetlib
import os
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import logging
from typing import Optional, Dict, Any, List
import re
import xml.etree.ElementTree as ET

# 配置日志记录，便于跟踪和控制
logging.basicConfig(
    level=logging.DEBUG,  # 设置为 DEBUG 以获取更多详细信息
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

class RouterManager:
    def __init__(self, telnet_info: Dict[str, Any], max_workers: int = 20, max_threads: int = 20):
        self.telnet_info = telnet_info
        self.telnet_sysnames: Dict[str, str] = {}
        self.config_checks: Dict[str, Dict[str, Any]] = {}
        self.performance_evaluations: Dict[str, Dict[str, Any]] = {}
        self.max_workers = max_workers
        self.max_threads = max_threads
        self.telnet_lock = Lock()
        # 为不同设备类型定义命令序列
        self.commands_map = {
            "h3c": (['display current-configuration'], b'quit\n'),
            # 可以根据需要为其他设备类型添加命令序列
        }
        # 定义需要检查的配置块及其对应的检查模式
        # 如果值为None，则只检查关键字的存在性
        self.required_blocks = {
            'sysname': None,  # 仅检查'sysname'关键字是否存在
            'bgp': None,       # 检查'bgp'块是否存在
            'ospf 1': None,    # 检查'ospf 1'块是否存在
            'isis 1': None,    # 检查'isis 1'块是否存在
            # 接口配置块及其需要检查的关键字
            'interface GigabitEthernet1/0': ['port link-mode route','ip address'],
            'interface GigabitEthernet2/0': ['port link-mode route','ip address' ],
            'interface GigabitEthernet3/0': ['port link-mode route','ip address',],
            'interface LoopBack0': ['ip address'],
            # 可根据需要添加更多需要检查的配置块及其检查模式
        }
        # 定义需要查找的路由类型
        self.route_types = ['IS_L1', 'O_INTRA', 'BGP']

    # ----------------------- Telnet 配置检查相关方法 -----------------------

    def execute_telnet_commands(self, tn: telnetlib.Telnet, commands: List[str], quit_cmd: bytes) -> str:
        try:
            tn.write(b'\n')
            time.sleep(1)
            output = tn.read_very_eager().decode('ascii', errors='ignore')
            logging.debug(f"[{tn.host}:{tn.port}] 初始Telnet输出:\n{output}")

            for cmd in commands:
                tn.write(cmd.encode('ascii') + b'\n')
                logging.info(f"[{tn.host}:{tn.port}] 发送命令: {cmd}")
                time.sleep(1)
                cmd_output = tn.read_very_eager().decode('ascii', errors='ignore')
                output += cmd_output
                logging.debug(f"[{tn.host}:{tn.port}] 命令 '{cmd}' 的输出:\n{cmd_output}")

            prompt = self.get_prompt(tn)
            if prompt and not (prompt.startswith('<') and prompt.endswith('>')):
                tn.write(quit_cmd)
                logging.info(f"[{tn.host}:{tn.port}] 发送退出命令.")
                time.sleep(1)
                output += tn.read_very_eager().decode('ascii', errors='ignore')
            return output
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] Telnet错误: {e}")
            return ""

    def get_prompt(self, tn: telnetlib.Telnet) -> Optional[str]:
        try:
            time.sleep(1)
            output = tn.read_very_eager().decode('ascii', errors='ignore')
            lines = output.splitlines()
            prompt = lines[-1].strip() if lines else None
            logging.debug(f"[{tn.host}:{tn.port}] 检测到的提示符: {prompt}")
            return prompt
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] 获取提示符时出错: {e}")
            return None

    def get_sysname_via_telnet(self, tn: telnetlib.Telnet) -> Optional[str]:
        """
        获取路由器的sysname。如果当前提示符为[ ]，则发送quit命令，直到提示符变为<>。
        一旦提示符为<>, 发送'screen-length disable' 命令。
        """
        max_retries = 5  # 设置最大重试次数以防止无限循环
        for attempt in range(max_retries):
            try:
                tn.write(b'\n')
                time.sleep(1)
                output = tn.read_very_eager().decode('ascii', errors='ignore')
                lines = output.splitlines()
                if not lines:
                    logging.warning(f"[{tn.host}:{tn.port}] 获取sysname时未收到输出。")
                    continue
                prompt = lines[-1].strip()
                logging.debug(f"[{tn.host}:{tn.port}] 检测到的提示符: {prompt}")

                if prompt.startswith('[') and prompt.endswith(']'):
                    # 发送quit命令并继续重试
                    tn.write(b'quit\n')
                    logging.info(f"[{tn.host}:{tn.port}] 提示符为 '[ ]'。发送 'quit' 命令。")
                    time.sleep(1)
                    continue
                elif prompt.startswith('<') and prompt.endswith('>'):
                    # 提取sysname
                    sysname = prompt.strip('<> ').strip()
                    logging.info(f"[{tn.host}:{tn.port}] 检测到的sysname: {sysname}")
                    # 发送 'screen-length disable' 命令
                    tn.write(b'screen-length disable\n')
                    logging.info(f"[{tn.host}:{tn.port}] 发送 'screen-length disable' 命令。")
                    time.sleep(1)
                    screen_length_output = tn.read_very_eager().decode('ascii', errors='ignore')
                    output += screen_length_output
                    logging.debug(f"[{tn.host}:{tn.port}] 'screen-length disable' 输出:\n{screen_length_output}")
                    return sysname
                else:
                    logging.warning(f"[{tn.host}:{tn.port}] 意外的提示符格式: {prompt}")
                    # 根据需要发送quit命令或采取其他行动
                    tn.write(b'quit\n')
                    logging.info(f"[{tn.host}:{tn.port}] 由于意外的提示符，发送 'quit' 命令。")
                    time.sleep(1)
            except Exception as e:
                logging.error(f"[{tn.host}:{tn.port}] 获取sysname时发生Telnet错误: {e}")
                return None
        logging.warning(f"[{tn.host}:{tn.port}] 在 {max_retries} 次尝试后未能检测到sysname。")
        return None

    def clean_configuration(self, raw_config: str) -> str:
        """
        清理原始配置，仅保留有用的配置块，如 sysname、interface、bgp、ospf 等。
        对接口配置块进行进一步处理，仅保留那些包含 'port link-mode route' 且后续有其他指令的接口。
        """
        cleaned_blocks = []
        blocks = raw_config.split('#')
        for block in blocks:
            block = block.strip()
            if not block:
                continue
            lines = block.splitlines()
            if not lines:
                continue
            first_line = lines[0].strip().lower()

            # 判断是否为有用的配置块
            if first_line.startswith('sysname'):
                cleaned_blocks.append(block)
                continue
            elif first_line.startswith('interface'):
                # 处理 interface 块
                # 检查 'port link-mode route' 后是否有其他指令
                port_link_mode_indices = [i for i, line in enumerate(lines) if 'port link-mode route' in line.lower()]
                if not port_link_mode_indices:
                    # 如果接口块中没有 'port link-mode route'，根据需求决定是否保留
                    cleaned_blocks.append(block)
                    continue
                last_port_link_mode = port_link_mode_indices[-1]
                if last_port_link_mode < len(lines) - 1:
                    # 'port link-mode route' 不是最后一条指令，保留该接口块
                    cleaned_blocks.append(block)
                else:
                    # 'port link-mode route' 是最后一条指令，删除该接口块
                    logging.debug(f"移除未配置的接口块: {lines[0].strip()}")
                continue
            elif first_line.startswith('bgp'):
                # 保留 bgp 块
                cleaned_blocks.append(block)
                continue
            elif first_line.startswith('ospf'):
                # 保留 ospf 块
                cleaned_blocks.append(block)
                continue
            elif first_line.startswith('isis'):
                # 保留 isis 块
                cleaned_blocks.append(block)
                continue
            elif first_line.startswith('address-family'):
                # 保留 ipv4-family 块
                cleaned_blocks.append(block)
                continue
            # 可以根据需要添加更多有用的配置块判断条件
            else:
                # 其他不需要的配置块删除
                continue

        # 使用 '#\n#\n' 作为块之间的分隔符，确保配置格式清晰
        cleaned_config = '#\n#\n'.join(cleaned_blocks)
        logging.debug("清理后的配置:\n" + cleaned_config)
        return cleaned_config

    def check_required_blocks(self, cleaned_config: str) -> Dict[str, Dict[str, Any]]:
        """
        检查清理后的配置中是否包含所有必需的配置块。
        对于每个配置块，如果配置模式为 None，则仅检查关键字是否存在于整个配置中。
        否则，检查配置块的存在性以及指定的配置模式是否匹配。
        返回一个字典，键为配置块名称，值为包含状态和内容的子字典。
        """
        checks = {}
        # 将清理后的配置块分割为列表
        cleaned_blocks = cleaned_config.split('#\n#\n')

        for block, patterns in self.required_blocks.items():
            if patterns is None:
                # 对于模式为 None 的配置块，仅检查关键字是否存在于整个配置中
                # 使用正则表达式确保关键字为独立的词
                pattern = re.compile(r'\b' + re.escape(block) + r'\b', re.IGNORECASE)
                matching_blocks = [blk.strip() for blk in cleaned_blocks if pattern.search(blk)]
                if matching_blocks:
                    # 提取所有匹配的块内容，并用分号分隔
                    content = '; '.join(matching_blocks)
                    checks[block] = {
                        "状态": "已配置",
                        "内容": content
                    }
                    logging.debug(f"配置块 '{block}' 已配置，内容: {content}")
                else:
                    checks[block] = {
                        "状态": "缺少配置",
                        "内容": ""
                    }
                    logging.debug(f"配置块 '{block}' 缺少配置。")
            else:
                # 对于有指定模式的配置块，检查配置块是否存在
                # 查找以该块名称开头的配置块
                block_pattern = re.compile(r'^' + re.escape(block) + r'\b', re.IGNORECASE)
                matched_blocks = [blk for blk in cleaned_blocks if block_pattern.match(blk)]
                if matched_blocks:
                    block_content = matched_blocks[0]
                    # 检查所有指定的模式是否存在于配置块中
                    if all(any(pattern.lower() in line.lower() for line in block_content.splitlines()) for pattern in patterns):
                        checks[block] = {
                            "状态": "已配置",
                            "内容": block_content.strip()
                        }
                        logging.debug(f"配置块 '{block}' 已配置，内容: {block_content.strip()}")
                    else:
                        checks[block] = {
                            "状态": "缺少配置",
                            "内容": ""
                        }
                        logging.debug(f"配置块 '{block}' 缺少部分配置。")
                else:
                    checks[block] = {
                        "状态": "缺少配置",
                        "内容": ""
                    }
                    logging.debug(f"配置块 '{block}' 不存在。")
        return checks

    def get_configuration_via_telnet(self, tn: telnetlib.Telnet, image_type: str) -> Optional[str]:
        # 根据部分image_type找到匹配的设备类型
        matched_key = next((key for key in self.commands_map if key in image_type), None)
        if not matched_key:
            logging.warning(f"[{tn.host}:{tn.port}] 不支持的 image_type '{image_type}'。跳过。")
            return None

        commands, quit_cmd = self.commands_map[matched_key]
        output = self.execute_telnet_commands(tn, commands, quit_cmd)
        if output:
            # 清理配置
            cleaned_output = self.clean_configuration(output)
            sysname = self.telnet_sysnames.get(f"{tn.host}:{tn.port}", "未知")
            if sysname:
                # 检查必需的配置块
                checks = self.check_required_blocks(cleaned_output)
                key = f"{tn.host}:{tn.port}"
                with self.telnet_lock:
                    self.config_checks[key] = checks  # 存储检查结果
                logging.debug(f"[{tn.host}:{tn.port}] 配置检查结果: {checks}")
        else:
            logging.warning(f"[{tn.host}:{tn.port}] 从Telnet命令未收到输出。")
        return output

    def connect_and_get_sysnames_and_configs(self):
        nodes = self.telnet_info.get("node", [])
        if not nodes:
            logging.warning("telnet_info中未找到节点。")
            return

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_node = {}
            for node in nodes:
                image_type = node.get("image_type", "").lower()
                # 修改这里以匹配 'h3c' 而不是 'huaweine40'
                if "h3c" in image_type:  # 或者使用更通用的匹配条件
                    host, port = node.get("hostip"), node.get("port")
                    if not host or not port:
                        logging.warning(f"节点的host IP或端口缺失（image_type='{image_type}'）。跳过。")
                        continue
                    try:
                        tn = telnetlib.Telnet(host, port, timeout=10)
                        tn.host, tn.port = host, port
                        # 首先获取sysname，并发送 'screen-length disable'
                        sysname = self.get_sysname_via_telnet(tn)
                        if sysname:
                            self.telnet_sysnames[f"{host}:{port}"] = sysname
                            # 提交获取配置的任务
                            future = executor.submit(self.get_configuration_via_telnet, tn, image_type)
                            future_to_node[future] = node
                        else:
                            logging.warning(f"[{host}:{port}] 无法获取sysname。跳过配置检索。")
                    except Exception as e:
                        logging.error(f"通过Telnet连接到 {host}:{port} 失败: {e}")

            for future in as_completed(future_to_node):
                node = future_to_node[future]
                host, port = node.get("hostip"), node.get("port")
                try:
                    config = future.result()
                    msg = "成功" if config else "失败"
                    logging.info(f"[{host}:{port}] 配置检索 {msg}。")
                except Exception as e:
                    logging.error(f"[{host}:{port}] 配置检索时发生错误: {e}")

    # ----------------------- 性能评估相关方法 -----------------------

    def get_ospf_route(self, tn: telnetlib.Telnet) -> Optional[str]:
        """
        获取OSPF路由表中的目的IP。
        """
        try:
            tn.write(b'display ip routing-table\n')
            routing_output = tn.read_until(b'>', timeout=5).decode('ascii', errors='ignore')
            ospf_ip = self.parse_routing_table(routing_output)
            return ospf_ip
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] 获取OSPF路由表时出错: {e}")
            return None

    def parse_routing_table(self, routing_table: str) -> Optional[str]:
        """
        解析路由表，找到第一次出现 IS_L1、O_INTRA 或 BGP 的目的地址，并去掉子网掩码。
        优先级按照在路由表中出现的顺序，不是固定的优先级。
        """
        for line in routing_table.splitlines():
            line = line.strip()
            # 检查每种路由类型
            for route_type in self.route_types:
                if route_type in line:
                    parts = line.split()
                    if parts:
                        # 提取目的地址并去除子网掩码（如果有）
                        dest_ip = parts[0].split('/')[0]
                        logging.debug(f"[{tn.host}:{tn.port}] Found {route_type} route: {dest_ip}")
                        return dest_ip
        logging.warning(f"[{tn.host}:{tn.port}] 未找到 IS_L1、O_INTRA 或 BGP 路由")
        return None

    def perform_nqa_test(self, tn: telnetlib.Telnet, dest_ip: str, max_attempts: int = 5) -> Optional[Dict[str, Any]]:
        """
        配置并执行 NQA 测试，最多尝试 max_attempts 次获取 NQA 测试结果，返回性能指标
        """
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
                partial_output = tn.read_until(b'>', timeout=5).decode('ascii', errors='ignore')
                result += partial_output

                if "The test is finished" in partial_output:
                    logging.info(f"[{tn.host}:{tn.port}] NQA测试完成（第 {attempt_count + 1} 次尝试）")
                    break

                attempt_count += 1
                logging.debug(f"[{tn.host}:{tn.port}] 第 {attempt_count} 次尝试获取 NQA 结果...")

                if attempt_count >= max_attempts:
                    logging.warning(f"[{tn.host}:{tn.port}] 达到最大尝试次数 ({max_attempts})，NQA测试结果可能不完整。")
                    break

            # 记录并返回结果
            logging.debug(f"[{tn.host}:{tn.port}] NQA 测试结果:\n{result}")

            # 执行结束和清理命令
            tn.write(b'undo nqa schedule admin perfor_test\n')
            tn.read_until(b']', timeout=3)

            # 解析并返回性能评估输入
            metrics = self.parse_nqa_result(result)
            if metrics is None:
                logging.warning(f"[{tn.host}:{tn.port}] 解析NQA结果失败。")
            return metrics

        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] 执行NQA测试时出错: {e}")
            return None

    def parse_nqa_result(self, nqa_result: str) -> Optional[Dict[str, Any]]:
        """
        解析 NQA 结果，提取性能指标。
        """
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
                    logging.debug(f"[{tn.host}:{tn.port}] 解析延迟 (平均 RTT): {latency_avg} ms")
                except ValueError as e:
                    logging.error(f"[{tn.host}:{tn.port}] 无法解析延迟，行: '{line}'。错误: {e}")
                    metrics["延迟"] = None

            # 解析丢包率 (Packet Loss Ratio)
            packet_loss_match = packet_loss_pattern.search(line)
            if packet_loss_match:
                try:
                    packet_loss = float(packet_loss_match.group(1))
                    metrics["丢包率"] = packet_loss
                    logging.debug(f"[{tn.host}:{tn.port}] 解析丢包率: {packet_loss} %")
                except ValueError as e:
                    logging.error(f"[{tn.host}:{tn.port}] 无法解析丢包率，行: '{line}'。错误: {e}")
                    metrics["丢包率"] = None

            # 解析抖动 (Jitter)
            jitter_avg_match = jitter_avg_pattern.search(line)
            if jitter_avg_match:
                try:
                    jitter_value = float(jitter_avg_match.group(1))
                    metrics["抖动"] = jitter_value
                    logging.debug(f"[{tn.host}:{tn.port}] 解析抖动 (平均抖动): {jitter_value} ms")
                except ValueError as e:
                    logging.error(f"[{tn.host}:{tn.port}] 无法解析抖动，行: '{line}'。错误: {e}")
                    metrics["抖动"] = None

            # 解析单向延迟的最大和最小延迟
            one_way_max_sd_match = one_way_max_sd_pattern.search(line)
            if one_way_max_sd_match:
                logging.debug(f"[{tn.host}:{tn.port}] 解析最大单向延迟: {one_way_max_sd_match.group(1)} ms")
            
            one_way_min_sd_match = one_way_min_sd_pattern.search(line)
            if one_way_min_sd_match:
                logging.debug(f"[{tn.host}:{tn.port}] 解析最小单向延迟: {one_way_min_sd_match.group(1)} ms")

        # 如果所有指标都无法解析，记录原始 NQA 结果以便调试
        if all(value is None for value in metrics.values()):
            logging.warning(f"[{tn.host}:{tn.port}] 所有性能指标均为 None。原始 NQA 结果:\n{nqa_result}")

        return metrics

    def evaluate_network_performance(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        """
        根据性能指标评估网络性能，并以中文表述结果。
        """
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

    def generate_performance_summary(self, evaluation: Dict[str, Any]) -> str:
        """
        根据性能评估结果生成综合的网络性能评价。
        """
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

    def process_router(self, tn: telnetlib.Telnet, node: Dict[str, Any]) -> None:
        """
        处理单个路由器的连接、配置检索、OSPF路由获取、执行NQA测试和性能评估。
        """
        host, port = tn.host, tn.port
        key = f"{host}:{port}"
        result_data = {
            "sysname": self.telnet_sysnames.get(key, "未知"),
            "配置检查": self.config_checks.get(key, {}),
            "性能评估": {}
        }

        # 获取 OSPF 路由
        ospf_ip = self.get_ospf_route(tn)
        if ospf_ip:
            # 执行 NQA 测试
            metrics = self.perform_nqa_test(tn, ospf_ip)
            if metrics:
                # 评估性能
                evaluation = self.evaluate_network_performance(metrics)
                summary = self.generate_performance_summary(evaluation)
                # 更新结果
                self.performance_evaluations[key] = {
                    "目的IP": ospf_ip,
                    "nqa_result": metrics,
                    "性能评估": evaluation,
                    "性能总结": summary
                }
                # 更新结果数据
                result_data["性能评估"] = self.performance_evaluations[key]
            else:
                logging.warning(f"[{host}:{port}] 无法获取NQA测试结果。")
        else:
            logging.warning(f"[{host}:{port}] 未找到OSPF路由，无法进行NQA测试。")

        # 存储结果
        with self.telnet_lock:
            self.config_checks[key].update(result_data["配置检查"])
            self.performance_evaluations[key] = result_data["性能评估"]

    def connect_and_get_sysnames_configs_and_performance(self):
        nodes = self.telnet_info.get("node", [])
        if not nodes:
            logging.warning("telnet_info中未找到节点。")
            return

        with ThreadPoolExecutor(max_workers=self.max_threads) as executor:
            future_to_node = {}
            for node in nodes:
                image_type = node.get("image_type", "").lower()
                # 修改这里以匹配 'h3c' 而不是 'huaweine40'
                if "h3c" in image_type:  # 或者使用更通用的匹配条件
                    host, port = node.get("hostip"), node.get("port")
                    if not host or not port:
                        logging.warning(f"节点的host IP或端口缺失（image_type='{image_type}'）。跳过。")
                        continue
                    try:
                        tn = telnetlib.Telnet(host, port, timeout=10)
                        tn.host, tn.port = host, port
                        # 首先获取sysname，并发送 'screen-length disable'
                        sysname = self.get_sysname_via_telnet(tn)
                        if sysname:
                            self.telnet_sysnames[f"{host}:{port}"] = sysname
                            # 提交获取配置的任务
                            future = executor.submit(self.get_configuration_via_telnet, tn, image_type)
                            future_to_node[future] = node
                        else:
                            logging.warning(f"[{host}:{port}] 无法获取sysname。跳过配置检索。")
                    except Exception as e:
                        logging.error(f"通过Telnet连接到 {host}:{port} 失败: {e}")

            for future in as_completed(future_to_node):
                node = future_to_node[future]
                host, port = node.get("hostip"), node.get("port")
                key = f"{host}:{port}"
                try:
                    config = future.result()
                    msg = "成功" if config else "失败"
                    logging.info(f"[{host}:{port}] 配置检索 {msg}。")
                except Exception as e:
                    logging.error(f"[{host}:{port}] 配置检索时发生错误: {e}")

        # 执行性能评估
        with ThreadPoolExecutor(max_workers=self.max_threads) as executor:
            future_to_node = {}
            for node in nodes:
                host, port = node.get("hostip"), node.get("port")
                key = f"{host}:{port}"
                if key in self.telnet_sysnames:
                    tn = telnetlib.Telnet(host, port, timeout=10)
                    tn.host, tn.port = host, port
                    future = executor.submit(self.process_router, tn, node)
                    future_to_node[future] = node

            for future in as_completed(future_to_node):
                node = future_to_node[future]
                host, port = node.get("hostip"), node.get("port")
                key = f"{host}:{port}"
                try:
                    future.result()
                    logging.info(f"[{host}:{port}] 性能评估完成。")
                except Exception as e:
                    logging.error(f"[{host}:{port}] 性能评估时发生错误: {e}")

    # ----------------------- 结果收集和输出相关方法 -----------------------

    def collect_results(self) -> Dict[str, Any]:
        combined_checks = {}
        for host_port, checks in self.config_checks.items():
            combined_checks[host_port] = {
                "sysname": self.telnet_sysnames.get(host_port, "未知"),
                "配置检查": checks,
                "性能评估": self.performance_evaluations.get(host_port, {})
            }
        return {
            "telnet_devices": combined_checks
        }

# ----------------------- 支持函数 -----------------------

def find_latest_folder(base_path: str) -> str:
    try:
        all_folders = [f for f in os.listdir(base_path) if f.isdigit()]
        if not all_folders:
            raise ValueError("基础路径中未找到编号文件夹。")
        latest_folder = max(all_folders, key=int)
        logging.info(f"识别到最新的文件夹: {latest_folder}")
        return latest_folder
    except FileNotFoundError:
        logging.error(f"基础路径未找到: {base_path}")
        sys.exit(1)
    except ValueError as ve:
        logging.error(ve)
        sys.exit(1)

def load_telnet_info(input_path: str) -> Dict[str, Any]:
    try:
        with open(input_path, 'r', encoding='utf-8') as f:
            telnet_info = json.load(f)
        logging.info(f"成功从 {input_path} 加载 telnet_info")
        return telnet_info
    except FileNotFoundError:
        logging.error(f"param.json文件未在路径找到: {input_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logging.error(f"从param.json解码JSON时出错: {e}")
        sys.exit(1)

def write_output(output_path: str, data: Dict[str, Any]):
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        logging.info(f"映射结果已写入 {output_path}")
    except IOError as e:
        logging.error(f"写入输出文件时出错: {e}")
        sys.exit(1)

def read_unl_file(lab_id: int) -> Optional[str]:
    """
    根据 labId 读取对应的 .unl 文件内容。
    """
    unl_file_path = f"/opt/unetlab/labs/{lab_id}.unl"  # 确保路径正确
    if os.path.exists(unl_file_path):
        try:
            with open(unl_file_path, 'r', encoding='utf-8') as f:
                unl_content = f.read()
            logging.info(f"成功读取 .unl 文件: {unl_file_path}")
            return unl_content
        except Exception as e:
            logging.error(f"读取 .unl 文件时出错: {e}")
            return None
    else:
        logging.error(f".unl 文件不存在: {unl_file_path}")
        return None

def parse_unl_content(unl_content: str) -> Optional[Dict[str, Any]]:
    """
    解析 .unl 文件的 XML 内容，整理为网络拓扑结构。
    """
    try:
        root = ET.fromstring(unl_content)
        topology = root.find('topology')
        if topology is None:
            logging.error("XML中未找到'topology'元素。")
            return None

        # 解析节点
        nodes = []
        nodes_xml = topology.find('nodes')
        if nodes_xml is not None:
            for node in nodes_xml.findall('node'):
                node_info = {
                    "id": node.get("id"),
                    "name": node.get("name"),
                    "image": node.get("image"),
                    "ethernet": node.get("ethernet"),
                    "interfaces": []
                }

                # 解析接口
                for interface in node.findall('interface'):
                    interface_info = {
                        "id": interface.get("id"),
                        "name": interface.get("name"),
                        "type": interface.get("type"),
                        "network_id": interface.get("network_id")
                    }
                    node_info["interfaces"].append(interface_info)
                nodes.append(node_info)

        # 解析网络
        networks = []
        networks_xml = topology.find('networks')
        if networks_xml is not None:
            for network in networks_xml.findall('network'):
                network_info = {
                    "id": network.get("id"),
                    "name": network.get("name")
                }
                networks.append(network_info)

        network_topology = {
            "nodes": nodes,
            "networks": networks
        }

        logging.info("成功解析 .unl 文件内容为网络拓扑结构。")
        return network_topology

    except ET.ParseError as e:
        logging.error(f"解析 XML 时出错: {e}")
        return None
    except Exception as e:
        logging.error(f"解析 .unl 文件内容时发生错误: {e}")
        return None

def annotate_network_topology(network_topology: Dict[str, Any],
                              telnet_devices: Dict[str, Any]) -> Dict[str, Any]:
    """
    将网络拓扑结构中的节点名称与telnet获取的sysname对应，
    并判断拓扑结构中哪些接口已配置，哪些尚未配置。
    """
    # 构建sysname到配置检查的映射
    sysname_to_checks = {}
    for device in telnet_devices.values():
        sysname = device.get("sysname")
        checks = device.get("配置检查", {})
        if sysname:
            sysname_to_checks[sysname] = checks

    # 接口类型映射
    type_mapping = {
        "ethernet": "Ethernet",
        "gigabitethernet": "GigabitEthernet",
        "gi": "GigabitEthernet",
        "eth": "Ethernet",
        "gi1/0": "GigabitEthernet1/0",
        "gi2/0": "GigabitEthernet2/0",
        "gi3/0": "GigabitEthernet3/0",
        "lo": "LoopBack",
        # 根据实际情况添加更多类型映射
    }

    # 创建节点名称到接口列表的映射
    node_to_interfaces = {node['name']: node['interfaces'] for node in network_topology.get("nodes", [])}

    # 遍历 Telnet 连接成功的节点
    annotated_topology = {}
    for host_port, device in telnet_devices.items():
        sysname = device.get("sysname", "未知")
        interfaces = node_to_interfaces.get(sysname, [])
        annotated_interfaces = []

        # 添加调试日志
        logging.debug(f"[{host_port}] 配置检查: {device.get('配置检查', {})}")
        logging.debug(f"[{host_port}] 性能评估: {device.get('性能评估', {})}")

        for interface in interfaces:
            interface_type = interface.get("type")
            interface_name = interface.get("name")

            # 映射接口类型
            interface_type_mapped = type_mapping.get(interface_type.lower(), interface_type.capitalize())

            # 处理接口名称：例如将 'Gi1/0' 转换为 'GigabitEthernet1/0'
            match = re.match(r'^([a-zA-Z]+)(\d+/\d+)$', interface_name)
            if match:
                interface_abbr = match.group(1).lower()
                interface_number = match.group(2)
                # 根据映射后的类型重新构造接口名称
                interface_type_full = type_mapping.get(interface_abbr, interface_abbr.capitalize())
                required_block_name = f"interface {interface_type_full}{interface_number}"
                logging.debug(f"映射接口 '{interface_name}' 为 '{required_block_name}'")
            else:
                # 如果没有匹配，保持原样
                required_block_name = f"interface {interface_type_mapped}{interface_name}"
                logging.debug(f"未匹配到编号，直接使用接口名称 '{interface_name}' 构造 '{required_block_name}'")

            # 去除键名中的空格
            blocks_cleaned = {k.strip(): v for k, v in device.get("配置检查", {}).items()}

            # 检查该接口是否已配置
            is_configured = blocks_cleaned.get(required_block_name, {}).get("状态") == "已配置"
            logging.debug(f"[{host_port}] Interface '{required_block_name}' 配置状态: {'已配置' if is_configured else '未配置'}")

            # 检查性能评估中接口相关的配置状态（假设有相关信息）
            performance_status = "未知"
            performance_info = device.get("性能评估", {})
            if performance_info:
                performance_status = performance_info.get("性能总结", "未知")

            annotated_interfaces.append({
                "名称": interface_name,
                "配置状态": "已配置" if is_configured else "未配置",
                "性能状态": performance_status
            })

        # 记录节点及其接口信息
        annotated_topology[sysname] = {
            "接口信息": annotated_interfaces,
            "协议配置状态": {}  # 协议信息将在摘要中处理
        }

    logging.info("已注释网络拓扑结构中的接口配置状态。")
    return annotated_topology

def generate_summary(network_topology: Dict[str, Any],
                    telnet_devices: Dict[str, Any]) -> Dict[str, Any]:
    """
    生成网络摘要，包括节点、接口状态和配置的协议。
    输出内容为中文，并包含协议配置状态。
    """
    summary = {"节点": []}
    # 创建sysname到接口信息的映射
    annotated_topology = annotate_network_topology(network_topology, telnet_devices)

    for host_port, device in telnet_devices.items():
        sysname = device.get("sysname", "未知")
        protocols = {}
        # 定义所有可能的协议
        all_protocols = ['BGP', 'OSPF', 'ISIS']
        for protocol in all_protocols:
            # 根据required_blocks中的定义来确定协议块的名称
            block_key = protocol.lower()
            if protocol.upper() == 'OSPF':
                block_key = 'ospf 1'
            elif protocol.upper() == 'ISIS':
                block_key = 'isis 1'

            if block_key in device.get("配置检查", {}):
                protocols[protocol] = device["配置检查"][block_key]["状态"]
            else:
                protocols[protocol] = "未配置"

        # 获取接口信息
        interfaces = annotated_topology.get(sysname, {}).get("接口信息", [])

        # 获取性能评估信息
        performance = device.get("性能评估", {})
        performance_evaluation = performance.get("性能评估", {})
        performance_summary = performance.get("性能总结", "无")

        node_summary = {
            "名称": sysname,
            "协议配置状态": protocols if protocols else "无",
            "接口信息": interfaces,
            "性能评估": performance_evaluation,
            "性能总结": performance_summary
        }
        summary["节点"].append(node_summary)

    return summary

# ----------------------- 主函数 -----------------------

def main(input_path: str, output_path: str, max_threads: int):
    base_path = "/uploadPath/reasoning"
    if "{t}" in input_path or "{t}" in output_path:
        latest_folder = find_latest_folder(base_path)
        input_path = input_path.replace("{t}", latest_folder)
        output_path = output_path.replace("{t}", latest_folder)
        logging.debug(f"解析后的input_path: {input_path}")
        logging.debug(f"解析后的output_path: {output_path}")

    telnet_info = load_telnet_info(input_path)

    # 读取 labId 并读取对应的 .unl 文件
    lab_id = telnet_info.get("labId")
    if lab_id is not None:
        logging.info(f"找到 labId: {lab_id}")
        unl_content = read_unl_file(lab_id)
        if unl_content:
            network_topology = parse_unl_content(unl_content)
        else:
            network_topology = None
    else:
        logging.warning("param.json中未找到labId。")
        unl_content = None
        network_topology = None

    router_manager = RouterManager(telnet_info, max_workers=20, max_threads=max_threads)
    router_manager.connect_and_get_sysnames_configs_and_performance()
    mapping = router_manager.collect_results()

    # 生成摘要
    if network_topology and mapping.get("telnet_devices"):
        summary = generate_summary(network_topology, mapping.get("telnet_devices", {}))
    else:
        summary = {
            "节点": []
        }

    # 将摘要写入输出文件中
    write_output(output_path, summary)

    logging.info("收集到的网络摘要已写入输出文件。")
    logging.info(json.dumps(summary, indent=4, ensure_ascii=False))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="从param.json处理路由器配置、执行NQA测试并生成网络摘要。")
    parser.add_argument("-i", "--input", required=True, help="param.json的路径，使用 {t} 表示最新的文件夹编号。")
    parser.add_argument("-o", "--output", required=True, help="处理信息的输出路径，使用 {t} 表示最新的文件夹编号。")
    parser.add_argument("--max-threads", type=int, default=20, help="最大并发线程数（默认为 20）。")
    args = parser.parse_args()
    main(args.input, args.output, max_threads=args.max_threads)
