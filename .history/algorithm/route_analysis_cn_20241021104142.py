import json
import telnetlib
import os
import argparse
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import logging
from typing import Optional, Dict, Any

# 配置日志，方便调试和监控
logging.basicConfig(
    level=logging.INFO,  # 设置日志级别，可根据需要调整为DEBUG
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]  # 日志输出到标准输出
)

class RouterManager:
    def __init__(self, telnet_info: Dict[str, Any], max_workers: int = 20):
        """
        初始化 RouterManager 类，设置 Telnet 信息和并发线程数。

        :param telnet_info: 包含路由器信息的字典。
        :param max_workers: 最大并发线程数，用于并行处理路由器。
        """
        self.telnet_info = telnet_info
        self.telnet_sysnames: Dict[str, str] = {}           # 存储 Telnet 设备的 sysname
        self.telnet_configurations: Dict[str, str] = {}    # 存储 Telnet 设备的配置信息
        self.docker_sysnames: Dict[str, str] = {}           # 存储 Docker 设备的 sysname
        self.docker_configurations: Dict[str, str] = {}    # 存储 Docker 设备的配置信息
        self.max_workers = max_workers
        self.telnet_lock = Lock()    # Telnet 数据的线程锁，确保线程安全
        self.docker_lock = Lock()    # Docker 数据的线程锁，确保线程安全
        # 定义不同设备类型对应的命令序列和退出命令
        self.commands_map = {
            "h3c": (['screen-length disable', 'display ip routing-table'], b'quit\n'),
            "huaweine40": (['scr 0 t', 'display ip routing-table'], b'q\n')
        }

    def execute_telnet_commands(self, tn: telnetlib.Telnet, commands: list, quit_cmd: bytes) -> str:
        """
        执行一系列 Telnet 命令并处理会话的退出。

        :param tn: telnetlib.Telnet 对象。
        :param commands: 要执行的命令列表。
        :param quit_cmd: 退出会话的命令。
        :return: 所有命令的合并输出。
        """
        try:
            tn.write(b'\n')  # 发送回车，确保连接稳定
            time.sleep(1)     # 等待命令执行
            output = tn.read_very_eager().decode('ascii', errors='ignore')
            logging.debug(f"[{tn.host}:{tn.port}] 初始 Telnet 输出:\n{output}")

            for cmd in commands:
                tn.write(cmd.encode('ascii') + b'\n')  # 发送命令
                logging.info(f"[{tn.host}:{tn.port}] 发送命令: {cmd}")
                time.sleep(1)  # 等待命令执行
                cmd_output = tn.read_very_eager().decode('ascii', errors='ignore')
                output += cmd_output
                logging.debug(f"[{tn.host}:{tn.port}] 命令 '{cmd}' 的输出:\n{cmd_output}")

            # 获取当前提示符，判断是否需要发送退出命令
            prompt = self.get_prompt(tn)
            if prompt and not (prompt.startswith('<') and prompt.endswith('>')):
                tn.write(quit_cmd)  # 发送退出命令
                logging.info(f"[{tn.host}:{tn.port}] 发送退出命令.")
                time.sleep(1)
                output += tn.read_very_eager().decode('ascii', errors='ignore')
            return output
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] Telnet 执行命令时出错: {e}")
            return ""

    def get_prompt(self, tn: telnetlib.Telnet) -> Optional[str]:
        """
        获取当前 Telnet 会话的提示符。

        :param tn: telnetlib.Telnet 对象。
        :return: 提示符字符串或 None。
        """
        try:
            time.sleep(1)  # 等待提示符出现
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
        通过 Telnet 获取设备的 sysname。

        :param tn: telnetlib.Telnet 对象。
        :return: sysname 字符串或 None。
        """
        try:
            tn.write(b'\n')  # 发送回车，触发设备返回 sysname
            time.sleep(1)
            output = tn.read_very_eager().decode('ascii', errors='ignore')
            for line in output.splitlines():
                if line.startswith('<') and line.endswith('>'):
                    sysname = line.strip('<> ').strip()
                    logging.info(f"[{tn.host}:{tn.port}] 检测到的 sysname: {sysname}")
                    return sysname
            logging.warning(f"[{tn.host}:{tn.port}] 未检测到 sysname.")
            return None
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] 通过 Telnet 获取 sysname 时出错: {e}")
            return None

    def get_configuration_via_telnet(self, tn: telnetlib.Telnet, image_type: str) -> Optional[str]:
        """
        根据设备类型通过 Telnet 获取配置。

        :param tn: telnetlib.Telnet 对象。
        :param image_type: 设备的镜像类型。
        :return: 配置输出字符串或 None。
        """
        # 根据部分匹配找到对应的设备类型
        matched_key = next((key for key in self.commands_map if key in image_type), None)
        if not matched_key:
            logging.warning(f"[{tn.host}:{tn.port}] 不支持的 image_type '{image_type}'. 跳过获取配置.")
            return None

        commands, quit_cmd = self.commands_map[matched_key]
        output = self.execute_telnet_commands(tn, commands, quit_cmd)
        if output:
            sysname = self.get_sysname_via_telnet(tn)
            if sysname:
                key = f"{tn.host}:{tn.port}"
                with self.telnet_lock:
                    self.telnet_sysnames[key] = sysname
                    self.telnet_configurations[key] = output
        else:
            logging.warning(f"[{tn.host}:{tn.port}] 未收到 Telnet 命令的输出.")
        return output

    def execute_docker_commands(self, container_id: str, commands: list) -> str:
        """
        在 Docker 容器内执行一系列命令。

        :param container_id: Docker 容器 ID。
        :param commands: 要执行的命令列表。
        :return: 所有命令的合并输出。
        """
        combined_output = ""
        for cmd in commands:
            try:
                full_cmd = ['docker', 'exec', container_id, 'bash', '-c', cmd]
                logging.info(f"[Docker:{container_id}] 执行命令: {cmd}")
                output = subprocess.check_output(full_cmd, stderr=subprocess.STDOUT).decode('utf-8', errors='ignore')
                combined_output += output
                logging.debug(f"[Docker:{container_id}] 命令 '{cmd}' 的输出:\n{output}")
            except subprocess.CalledProcessError as e:
                logging.error(f"[Docker:{container_id}] 执行命令 '{cmd}' 时出错: {e.output.decode('utf-8', errors='ignore')}")
            except Exception as e:
                logging.error(f"[Docker:{container_id}] 执行命令 '{cmd}' 时发生意外错误: {e}")
        return combined_output

    def extract_sysname_from_docker_output(self, output: str) -> Optional[str]:
        """
        从 Docker 命令输出中提取 sysname。

        :param output: Docker 命令的输出字符串。
        :return: sysname 字符串或 None。
        """
        for line in output.splitlines():
            if '#' in line:
                sysname_candidate = line.split('#')[0].strip()
                if sysname_candidate:
                    logging.info(f"[Docker] 提取到的 sysname: {sysname_candidate}")
                    return sysname_candidate
        logging.warning("[Docker] 未在输出中检测到 sysname.")
        return None

    def get_container_id(self, docker_id: str) -> Optional[str]:
        """
        根据部分或完整的 Docker 名称获取容器的完整 ID。

        :param docker_id: 部分或完整的 Docker 容器名称。
        :return: 完整的 Docker 容器 ID 或 None。
        """
        try:
            docker_ps = subprocess.check_output(['docker', 'ps', '--format', '{{.ID}} {{.Names}}'], stderr=subprocess.STDOUT).decode('utf-8', errors='ignore')
            for line in docker_ps.splitlines():
                cid, name = line.strip().split(None, 1)
                if docker_id in cid or docker_id == name:
                    return cid
            return None
        except subprocess.CalledProcessError as e:
            logging.error(f"[Docker:{docker_id}] 获取 Docker 容器时出错: {e.output.decode('utf-8', errors='ignore')}")
            return None
        except Exception as e:
            logging.error(f"[Docker:{docker_id}] 获取 Docker 容器 ID 时发生意外错误: {e}")
            return None

    def connect_via_docker(self, docker_id: str) -> bool:
        """
        连接到 Docker 容器并获取 sysname 和配置。

        :param docker_id: Docker 容器标识符。
        :return: 如果成功获取配置，则返回 True，否则返回 False。
        """
        container_id = self.get_container_id(docker_id)
        if not container_id:
            logging.error(f"[Docker:{docker_id}] 未找到对应的 Docker 容器.")
            return False

        logging.info(f"[Docker:{docker_id}] 已连接到 Docker 容器: {container_id}")
        initial_output = self.execute_docker_commands(container_id, ['vtysh', 'echo ""'])
        sysname = self.extract_sysname_from_docker_output(initial_output)
        with self.docker_lock:
            self.docker_sysnames[docker_id] = sysname if sysname else "Unknown"

        subsequent_output = self.execute_docker_commands(container_id, [
            'vtysh -c "terminal length 0"',
            'vtysh -c "show ip ospf route"'
        ])
        with self.docker_lock:
            self.docker_configurations[docker_id] = initial_output + subsequent_output

        logging.info(f"[Docker:{docker_id}] 成功获取配置.")
        return True

    def connect_and_get_sysnames_and_configs(self):
        """
        连接到每个路由器并根据 image_type 获取 sysname 和配置。
        使用并行执行提高效率。
        """
        nodes = self.telnet_info.get("node", [])
        if not nodes:
            logging.warning("telnet_info 中未找到任何节点.")
            return

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_node = {}

            for node in nodes:
                image_type = node.get("image_type", "").lower()
                if "frrouting" in image_type:
                    docker_id = node.get("dockerid")
                    if docker_id:
                        future = executor.submit(self.connect_via_docker, docker_id)
                        future_to_node[future] = node
                    else:
                        logging.warning("image_type 为 'frrouting' 的节点未提供 Docker ID. 跳过.")
                elif any(sub in image_type for sub in self.commands_map.keys()):
                    host, port = node.get("hostip"), node.get("port")
                    if not host or not port:
                        logging.warning(f"image_type 为 '{image_type}' 的节点缺少 Host IP 或端口. 跳过.")
                        continue
                    try:
                        tn = telnetlib.Telnet(host, port, timeout=10)
                        tn.host, tn.port = host, port
                        future = executor.submit(self.get_configuration_via_telnet, tn, image_type)
                        future_to_node[future] = node
                    except Exception as e:
                        logging.error(f"无法通过 Telnet 连接到 {host}:{port}: {e}")
                else:
                    logging.warning(f"未知的 image_type '{image_type}' 的节点. 跳过.")

            # 处理所有完成的任务
            for future in as_completed(future_to_node):
                node = future_to_node[future]
                image_type = node.get("image_type", "").lower()
                if "frrouting" in image_type:
                    docker_id = node.get("dockerid")
                    success = future.result()
                    msg = "成功" if success else "失败"
                    logging.info(f"[Docker:{docker_id}] 配置获取 {msg}.")
                elif any(sub in image_type for sub in self.commands_map.keys()):
                    host, port = node.get("hostip"), node.get("port")
                    config = future.result()
                    msg = "成功" if config else "失败"
                    logging.info(f"[{host}:{port}] 配置获取 {msg}.")

    def collect_results(self) -> Dict[str, Any]:
        """
        收集并整理从 Telnet 和 Docker 设备获取的 sysname 和配置。

        :return: 包含 Telnet 和 Docker 设备信息的字典。
        """
        return {
            "telnet_devices": {
                host_port: {
                    "sysname": sysname,
                    "configuration": self.telnet_configurations.get(host_port, 'No config')
                }
                for host_port, sysname in self.telnet_sysnames.items()
            },
            "docker_devices": {
                docker_id: {
                    "sysname": sysname,
                    "configuration": self.docker_configurations.get(docker_id, 'No config')
                }
                for docker_id, sysname in self.docker_sysnames.items()
            }
        }

def find_latest_folder(base_path: str) -> str:
    """
    查找指定路径下编号最大的文件夹。

    :param base_path: 基础目录路径。
    :return: 最新文件夹的名称。
    """
    try:
        all_folders = [f for f in os.listdir(base_path) if f.isdigit()]
        if not all_folders:
            raise ValueError("基础路径下未找到任何编号文件夹.")
        latest_folder = max(all_folders, key=int)
        logging.info(f"识别到最新文件夹: {latest_folder}")
        return latest_folder
    except FileNotFoundError:
        logging.error(f"基础路径未找到: {base_path}")
        sys.exit(1)
    except ValueError as ve:
        logging.error(ve)
        sys.exit(1)

def load_telnet_info(input_path: str) -> Dict[str, Any]:
    """
    从 JSON 输入文件加载 Telnet 信息。

    :param input_path: param.json 文件的路径。
    :return: 解析后的 Telnet 信息字典。
    """
    try:
        with open(input_path, 'r') as f:
            telnet_info = json.load(f)
        logging.info(f"成功加载 telnet_info 从 {input_path}")
        return telnet_info
    except FileNotFoundError:
        logging.error(f"param.json 文件未找到: {input_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logging.error(f"解析 param.json 时出错: {e}")
        sys.exit(1)

def write_output(output_path: str, data: Dict[str, Any]):
    """
    将收集到的路由器配置信息写入输出文件。

    :param output_path: 输出 JSON 文件的路径。
    :param data: 包含路由器配置信息的字典。
    """
    try:
        with open(output_path, 'w') as f:
            json.dump(data, f, indent=4)
        logging.info(f"配置信息已写入 {output_path}")
    except IOError as e:
        logging.error(f"写入输出文件时出错: {e}")
        sys.exit(1)

def main(input_path: str, output_path: str):
    """
    主函数，协调路由器配置的获取和结果的输出。

    :param input_path: param.json 的路径，可能包含 {t} 占位符。
    :param output_path: 输出 JSON 文件的路径，可能包含 {t} 占位符。
    """
    base_path = "/uploadPath/reasoning"
    # 处理路径中的 {t} 占位符，替换为最新文件夹编号
    if "{t}" in input_path or "{t}" in output_path:
        latest_folder = find_latest_folder(base_path)
        input_path = input_path.replace("{t}", latest_folder)
        output_path = output_path.replace("{t}", latest_folder)
        logging.debug(f"解析后的 input_path: {input_path}")
        logging.debug(f"解析后的 output_path: {output_path}")

    # 加载 Telnet 信息
    telnet_info = load_telnet_info(input_path)
    # 初始化 RouterManager 并获取配置
    router_manager = RouterManager(telnet_info)
    router_manager.connect_and_get_sysnames_and_configs()
    # 收集结果
    mapping = router_manager.collect_results()

    logging.info("收集到的路由器配置信息:")
    logging.info(json.dumps(mapping, indent=4))
    # 将结果写入输出文件
    write_output(output_path, mapping)

if __name__ == "__main__":
    # 设置命令行参数解析
    parser = argparse.ArgumentParser(description="从 param.json 处理路由器配置.")
    parser.add_argument("-i", "--input", required=True, help="param.json 的路径，使用 {t} 代表最新文件夹编号.")
    parser.add_argument("-o", "--output", required=True, help="处理信息的输出路径，使用 {t} 代表最新文件夹编号.")
    args = parser.parse_args()
    # 执行主函数
    main(args.input, args.output)
