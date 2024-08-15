import random
import os
import argparse

class ExperimentProcessor:
    def __init__(self, input_base_dir, output_base_dir):
        self.input_base_dir = input_base_dir
        self.output_base_dir = output_base_dir

    def find_latest_folder(self, base_dir):
        folders = [f for f in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, f)) and f.isdigit()]

        if not folders:
            raise Exception(f"No folders found in {base_dir}")

        latest_folder = max(folders, key=int)
        latest_folder_path = os.path.join(base_dir, latest_folder)

        return latest_folder_path, latest_folder

    def process_paths(self):
        input_folder_path, folder_name = self.find_latest_folder(self.input_base_dir)
        output_folder_path = os.path.join(self.output_base_dir, folder_name)

        input_file = os.path.join(input_folder_path, 'params', 'param.json')
        output_folder = os.path.join(output_folder_path, 'res')

        if not os.path.exists(output_folder):
            os.makedirs(output_folder)

        output_file = os.path.join(output_folder, 'data.txt')

        return input_file, output_file

def generate_network_parameters():
    return {
        "网络连接状态": random.choice(["稳定", "不稳定", "断开"]),
        "路由状态": random.choice(["正确", "不正确", "失败"]),
        "ACL状态": random.choice(["有效", "丢失", "配置错误"]),
        "QoS状态": random.choice(["最优", "次优", "配置错误"]),
        "CPU使用率": round(random.uniform(0, 100), 3),
        "内存使用率": round(random.uniform(0, 100), 3),
    }

def check_connection_status(connection_status):
    if connection_status != "稳定":
        return f"错误：网络连接{connection_status}."
    return "网络连接是稳定的."

def check_route_status(route_status):
    if route_status != "正确":
        return f"错误：路由配置为{route_status}."
    return "路由配置是正确的."

def check_acl_status(acl_status):
    if acl_status != "有效":
        return f"错误：ACL是{acl_status}."
    return "ACL配置是有效的."

def check_qos_status(qos_status):
    if qos_status != "最优":
        return f"错误：QoS配置是{qos_status}."
    return "QoS配置是最优的."

def check_device_performance(cpu_usage, memory_usage):
    messages = []
    if cpu_usage > 85:
        messages.append(f"警告：检测到CPU使用率高,为{cpu_usage:.3f}%.")
    if memory_usage > 85:
        messages.append(f"警告：检测到内存使用率高,为{memory_usage:.3f}%.")
    return messages if messages else ["设备性能在可接受的范围内."]

def main(input_file, output_file):
    network_params = generate_network_parameters()

    output = []
    output.append("网络参数: " + str(network_params))
    output.append(check_connection_status(network_params["网络连接状态"]))
    output.append(check_route_status(network_params["路由状态"]))
    output.append(check_acl_status(network_params["ACL状态"]))
    output.append(check_qos_status(network_params["QoS状态"]))
    performance_messages = check_device_performance(network_params["CPU使用率"], network_params["内存使用率"])
    output.extend(performance_messages)

    with open(output_file, 'w') as file:
        for line in output:
            file.write(line + '\n')

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="网络参数检测工具")
    parser.add_argument('-i', '--input', help="param.json文件的输入路径")
    parser.add_argument('-o', '--output', help="输出文件的输出路径")

    args = parser.parse_args()

    # 使用默认的输入输出路径，除非提供了 -i 和 -o 参数
    processor = ExperimentProcessor(input_base_dir="/uploadPath/reasoning", output_base_dir="/uploadPath/reasoning")
    if args.input and args.output:
        input_file = args.input
        output_file = args.output
    else:
        input_file, output_file = processor.process_paths()

    main(input_file, output_file)
