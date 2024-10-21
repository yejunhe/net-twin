import json
import telnetlib
import re
import os
import argparse
from datetime import datetime
import subprocess

def find_latest_folder(base_path):
    """
    查找给定路径下最新的以数字命名的文件夹。
    """
    try:
        all_folders = [f for f in os.listdir(base_path) if os.path.isdir(os.path.join(base_path, f)) and f.isdigit()]
        if not all_folders:
            raise ValueError("在基路径中未找到以数字命名的文件夹。")
        return max(all_folders, key=lambda x: int(x))
    except Exception as e:
        raise e

def resolve_path(path):
    """
    如果路径中包含 {t}，则替换为最新的文件夹名称。
    否则，返回原路径。
    """
    if "{t}" in path:
        base_path = "/uploadPath/reasoning"
        latest_folder = find_latest_folder(base_path)
        return path.replace("{t}", latest_folder)
    return path

def read_config(file_path):
    """
    读取JSON配置文件并返回数据
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def execute_telnet_command(tn, command, expect_list, error_message, timeout=5):
    """
    执行Telnet命令并检查返回结果。
    """
    tn.write(command.encode('utf-8'))
    idx, match, text = tn.expect(expect_list, timeout=timeout)
    if idx == -1:
        raise Exception(error_message)
    return text.decode('utf-8')

def set_ospf_cost_telnet(tn, interface_name, cost):
    """
    在现有Telnet连接中设置接口的OSPF cost值
    """
    try:
        # 设置 screen-length 0 temporary 防止分页
        execute_telnet_command(tn, 'screen-length 0 temporary\n', [b"]", b">"], "未能设置 screen-length")

        # 进入系统模式
        execute_telnet_command(tn, 'sy\n', [b"]", b">"], "未能进入系统模式")

        # 清理接口名称，去除前缀 'e'
        clean_interface_name = re.sub(r'^e', '', interface_name)

        # 进入接口视图
        execute_telnet_command(tn, f'interface ethernet {clean_interface_name}\n', [b"]"], f"未能进入接口模式: {clean_interface_name}")

        # 设置OSPF cost
        execute_telnet_command(tn, f'ospf cost {cost}\n', [b"]"], f"未能设置OSPF cost: {cost}")

        # 提交配置并退出接口模式
        execute_telnet_command(tn, 'commit\n', [b"]"], "未能提交配置")
        execute_telnet_command(tn, 'q\n', [b"]", b">"], "未能退出接口模式")

        # 直接返回成功信息
        return f"接口 {clean_interface_name} 的OSPF cost已成功设置为 {cost}"

    except Exception as e:
        return f"操作过程中发生错误: {e}"

def perform_telnet_operations(host, port, interface_name, cost, output_log):
    """
    通过Telnet连接路由器，设置接口的OSPF cost值，并记录结果
    """
    try:
        # 连接到Telnet服务器
        tn = telnetlib.Telnet(host, port, timeout=10)
        output_log.append(f"{datetime.now()} - 成功连接到 {host}:{port}\n")

        # 设置OSPF cost
        result = set_ospf_cost_telnet(tn, interface_name, cost)
        output_log.append(f"{datetime.now()} - {result}\n")

        tn.close()

    except Exception as e:
        error_message = f"{datetime.now()} - 连接或操作过程中发生错误: {e}\n"
        output_log.append(error_message)

def transform_interface_name(dockerid, interface_name):
    """
    根据dockerid和interface_name生成特定格式的接口名称。
    格式为：veth{dockerid末尾两部分（用_连接）}_{接口号}
    例如：
    dockerid: 45248690-cb55-4c57-9ca7-20b49a26cf50-1-6
    interface_name: eth0
    生成: veth1_6_0
    """
    try:
        # 提取dockerid的末尾两部分
        parts = dockerid.strip().split('-')
        if len(parts) < 2:
            raise ValueError("dockerid格式不正确，无法提取末尾的两部分。")
        last_two = parts[-2:]  # ['1', '6']
        last_two_joined = '_'.join(last_two)  # '1_6'

        # 提取接口号
        match = re.match(r'eth(\d+)', interface_name)
        if not match:
            raise ValueError(f"接口名称格式不正确: {interface_name}")
        interface_number = match.group(1)  # '0'

        # 构建新的接口名称
        new_interface_name = f'veth{last_two_joined}_{interface_number}'  # 'veth1_6_0'

        return new_interface_name

    except Exception as e:
        raise e

def set_ospf_cost_docker(dockerid, interface_name, cost):
    """
    通过Docker命令设置FRRouting容器中的接口OSPF cost值
    """
    try:
        # 执行docker ps命令并获取输出
        docker_ps_cmd = ['docker', 'ps', '--filter', f'name={dockerid}', '--format', '{{.ID}} {{.Names}}']
        docker_ps_result = subprocess.run(docker_ps_cmd, capture_output=True, text=True, check=True)

        docker_ps_output = docker_ps_result.stdout.strip()
        if not docker_ps_output:
            return f"未找到匹配的Docker容器名称: {dockerid}"

        # 提取容器ID
        container_id = docker_ps_output.split()[0]

        # 生成新的接口名称
        new_interface_name = transform_interface_name(dockerid, interface_name)

        # 构建vtysh命令
        vtysh_commands = (
            f"configure terminal\n"
            f"interface {new_interface_name}\n"
            f"ip ospf cost {cost}\n"
            f"exit\n"
            f"write\n"
        )

        # 执行docker exec命令，将vtysh命令通过管道传输
        exec_cmd = ['docker', 'exec', '-i', container_id, 'vtysh']
        process = subprocess.Popen(exec_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = process.communicate(input=vtysh_commands)

        if process.returncode != 0:
            return f"执行OSPF配置命令时出错: {stderr.strip()}"

        return f"接口 {new_interface_name} 的OSPF cost已成功设置为 {cost} 在容器 {container_id}"

    except subprocess.CalledProcessError as e:
        return f"执行Docker命令时发生错误: {e.stderr.strip()}"
    except Exception as e:
        return f"操作过程中发生错误: {e}"

def perform_docker_operations(node, interface_name, cost, output_log):
    """
    通过Docker命令设置FRRouting容器中的接口OSPF cost值，并记录结果
    """
    try:
        dockerid = node.get('dockerid')
        if not dockerid:
            output_log.append(f"{datetime.now()} - 缺少Docker ID信息。\n")
            return

        result = set_ospf_cost_docker(dockerid, interface_name, cost)
        output_log.append(f"{datetime.now()} - {result}\n")

    except Exception as e:
        error_message = f"{datetime.now()} - Docker操作过程中发生错误: {e}\n"
        output_log.append(error_message)

def main():
    # 设置命令行参数解析
    parser = argparse.ArgumentParser(description="通过Telnet或Docker命令设置路由器接口的OSPF cost值。")
    parser.add_argument("-i", "--input", required=True, help="param.json文件的路径，使用 {t} 作为最新文件夹的占位符。")
    parser.add_argument("-o", "--output", required=True, help="结果输出路径，使用 {t} 作为最新文件夹的占位符。")
    args = parser.parse_args()

    # 解析输入和输出路径
    input_path = resolve_path(args.input)
    output_path = resolve_path(args.output)

    # 确保输出目录存在
    output_dir = os.path.dirname(output_path)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # 读取配置文件
    try:
        config = read_config(input_path)
    except Exception as e:
        print(f"无法读取配置文件: {e}")
        return

    # 准备输出日志
    output_log = [f"{datetime.now()} - 开始处理配置文件: {input_path}\n"]

    # 遍历所有节点
    for node in config.get('node', []):
        image_type = node.get('image_type', '')

        if image_type == "huaweine40-1":
            hostip = node.get('hostip')
            port = node.get('port')
            interface = node.get('interface', {})
            interface_name = interface.get('name')
            cost = interface.get('cost')

            output_log.append(f"\n{datetime.now()} - 正在处理华为路由器节点 {hostip}:{port}\n")
            output_log.append(f"接口名称: {interface_name}, 设置的OSPF cost: {cost}\n")

            perform_telnet_operations(hostip, port, interface_name, cost, output_log)

        elif image_type == "25125/frrouting:10-dev-05221913":
            interface = node.get('interface', {})
            interface_name = interface.get('name')
            cost = interface.get('cost')

            dockerid = node.get('dockerid')
            output_log.append(f"\n{datetime.now()} - 正在处理FRRouting Docker容器节点 {dockerid}\n")
            output_log.append(f"接口名称: {interface_name}, 设置的OSPF cost: {cost}\n")

            perform_docker_operations(node, interface_name, cost, output_log)

        else:
            output_log.append(f"\n{datetime.now()} - 未知的节点类型或不支持的配置: {node}\n")

    # 写入输出日志到文件
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.writelines(output_log)
        print(f"操作完成，结果已写入 {output_path}")
    except Exception as e:
        print(f"无法写入输出文件: {e}")

if __name__ == "__main__":
    main()
