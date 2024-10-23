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

# 配置日志记录，便于跟踪和控制
logging.basicConfig(
    level=logging.INFO,  # 可以根据需要调整为 DEBUG 以获取更多详细信息
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

class RouterManager:
    def __init__(self, telnet_info: Dict[str, Any], max_workers: int = 20):
        self.telnet_info = telnet_info
        self.telnet_sysnames: Dict[str, str] = {}
        self.telnet_configurations: Dict[str, str] = {}
        self.config_checks: Dict[str, Dict[str, str]] = {}
        self.max_workers = max_workers
        self.telnet_lock = Lock()
        # 为不同设备类型定义命令序列
        self.commands_map = {
            "huaweine40": (['scr 0 t', 'display current-configuration'], b'q\n')
        }
        # 定义需要检查的配置块及其对应的检查模式
        # 如果值为None，则只检查关键字的存在性
        self.required_blocks = {
            'sysname': None,  # 仅检查'sysname'关键字是否存在
            'bgp': None,       # 仅检查'bgp'关键字是否存在
            'ospf': None,      # 仅检查'ospf'关键字是否存在
            'isis': None,      # 仅检查'isis'关键字是否存在
            'interface Ethernet1/0/0': ['undo shutdown', 'ip address'],
            'interface Ethernet1/0/1': ['undo shutdown', 'ip address'],
            'interface Ethernet1/0/2': ['undo shutdown', 'ip address'],
            'interface LoopBack0': ['ip address', 'ospf enable'],
            'interface NULL0': None  # 仅检查'interface NULL0'关键字是否存在
            # 可根据需要添加更多需要检查的配置块及其检查模式
        }

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
        对接口配置块进行进一步处理，仅保留那些包含 'undo shutdown' 且后续有其他指令的接口。
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
                # 检查 'undo shutdown' 后是否有其他指令
                undo_shutdown_indices = [i for i, line in enumerate(lines) if 'undo shutdown' in line.lower()]
                if not undo_shutdown_indices:
                    # 如果接口块中没有 'undo shutdown'，根据需求决定是否保留
                    cleaned_blocks.append(block)
                    continue
                last_undo_shutdown = undo_shutdown_indices[-1]
                if last_undo_shutdown < len(lines) - 1:
                    # 'undo shutdown' 不是最后一条指令，保留该接口块
                    cleaned_blocks.append(block)
                else:
                    # 'undo shutdown' 是最后一条指令，删除该接口块
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
            elif first_line.startswith('ipv4-family'):
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

    def check_required_blocks(self, cleaned_config: str) -> Dict[str, str]:
        """
        检查清理后的配置中是否包含所有必需的配置块。
        对于每个配置块，如果配置模式为 None，则仅检查关键字是否存在于整个配置中。
        否则，检查配置块的存在性以及指定的配置模式是否匹配。
        返回一个字典，键为配置块名称，值为状态（'已配置' 或 '缺少配置'）。
        """
        checks = {}
        # 将清理后的配置块分割为列表
        cleaned_blocks = cleaned_config.split('#\n#\n')
        # 创建一个字典，键为配置块名称，值为配置块内容
        cleaned_blocks_content = {block.splitlines()[0].strip(): block for block in cleaned_blocks if block.strip()}

        for block, patterns in self.required_blocks.items():
            if patterns is None:
                # 对于模式为 None 的配置块，仅检查关键字是否存在于整个配置中
                if block in cleaned_config:
                    checks[block] = "已配置"
                else:
                    checks[block] = "缺少配置"
            else:
                # 对于有指定模式的配置块，检查配置块是否存在
                if block in cleaned_blocks_content:
                    block_content = cleaned_blocks_content[block]
                    # 检查所有指定的模式是否存在于配置块中
                    if all(any(pattern in line for line in block_content.splitlines()) for pattern in patterns):
                        checks[block] = "已配置"
                    else:
                        checks[block] = "缺少配置"
                else:
                    checks[block] = "缺少配置"
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
            sysname = self.get_sysname_via_telnet(tn)
            if sysname:
                # 检查必需的配置块
                checks = self.check_required_blocks(cleaned_output)
                key = f"{tn.host}:{tn.port}"
                with self.telnet_lock:
                    self.telnet_sysnames[key] = sysname
                    self.telnet_configurations[key] = cleaned_output  # 存储清理后的配置
                    self.config_checks[key] = checks  # 存储检查结果
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
                if "huaweine40" in image_type:
                    host, port = node.get("hostip"), node.get("port")
                    if not host or not port:
                        logging.warning(f"节点的host IP或端口缺失（image_type='{image_type}'）。跳过。")
                        continue
                    try:
                        tn = telnetlib.Telnet(host, port, timeout=10)
                        tn.host, tn.port = host, port
                        future = executor.submit(self.get_configuration_via_telnet, tn, image_type)
                        future_to_node[future] = node
                    except Exception as e:
                        logging.error(f"通过Telnet连接到 {host}:{port} 失败: {e}")

            for future in as_completed(future_to_node):
                node = future_to_node[future]
                host, port = node.get("hostip"), node.get("port")
                config = future.result()
                msg = "成功" if config else "失败"
                logging.info(f"[{host}:{port}] 配置检索 {msg}。")

    def collect_results(self) -> Dict[str, Any]:
        return {
            "telnet_devices": {
                host_port: {
                    "sysname": sysname,
                    "configuration": self.telnet_configurations.get(host_port, 'No config'),
                    "configuration_checks": self.config_checks.get(host_port, {})
                }
                for host_port, sysname in self.telnet_sysnames.items()
            }
        }

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

def main(input_path: str, output_path: str):
    base_path = "/uploadPath/reasoning"
    if "{t}" in input_path or "{t}" in output_path:
        latest_folder = find_latest_folder(base_path)
        input_path = input_path.replace("{t}", latest_folder)
        output_path = output_path.replace("{t}", latest_folder)
        logging.debug(f"解析后的input_path: {input_path}")
        logging.debug(f"解析后的output_path: {output_path}")

    telnet_info = load_telnet_info(input_path)
    router_manager = RouterManager(telnet_info)
    router_manager.connect_and_get_sysnames_and_configs()
    mapping = router_manager.collect_results()

    logging.info("收集到的路由器配置:")
    logging.info(json.dumps(mapping, indent=4, ensure_ascii=False))
    write_output(output_path, mapping)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="从param.json处理路由器配置。")
    parser.add_argument("-i", "--input", required=True, help="param.json的路径，使用 {t} 表示最新的文件夹编号。")
    parser.add_argument("-o", "--output", required=True, help="处理信息的输出路径，使用 {t} 表示最新的文件夹编号。")
    args = parser.parse_args()
    main(args.input, args.output)
