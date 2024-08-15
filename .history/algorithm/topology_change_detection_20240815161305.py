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
        "路由表更新次数": random.randint(0, 100),
        "STP状态": random.choice(["活跃", "非活跃", "错误"]),
        "数据流路径长度": random.randint(1, 20),
        "负载均衡比": round(random.uniform(0, 1), 3),
        "ACL状态": random.choice(["有效", "无效"]),
        "防火墙规则状态": random.choice(["有效", "无效"]),
    }

def check_route_stability(route_updates):
    if route_updates > 50:
        return "警告：由于频繁的路线更新，检测到路线不稳定."
    return "路线稳定性在可接受的范围内."

def check_loop_errors(stp_status):
    if stp_status == "错误":
        return "错误：由于STP配置错误，检测到循环."
    return "STP状态正常."

def check_path_efficiency(path_length):
    if path_length > 10:
        return "警告：路径效率低，检测到可能绕行."
    return "路径长度是有效的."

def check_load_balance(load_balance_ratio):
    if load_balance_ratio < 0.2 or load_balance_ratio > 0.8:
        return f"警告：检测到负载均衡失败，流量分布不均匀.负载均衡比: {load_balance_ratio:.3f}"
    return f"负载均衡功能正常.负载均衡比： {load_balance_ratio:.3f}"

def check_security_policies(acl_status, firewall_rules_status):
    messages = []
    if acl_status == "无效":
        messages.append("错误：ACL配置无效。")
    if firewall_rules_status == "无效":
        messages.append("错误：防火墙规则配置无效。")

    return messages if messages else ["安全策略已正确配置."]

def main(input_file, output_file):
    network_params = generate_network_parameters()

    output = []
    output.append("网络参数: " + str(network_params))
    output.append(check_route_stability(network_params["路由表更新次数"]))
    output.append(check_loop_errors(network_params["STP状态"]))
    output.append(check_path_efficiency(network_params["数据流路径长度"]))
    output.append(check_load_balance(network_params["负载均衡比"]))
    security_messages = check_security_policies(network_params["ACL状态"], network_params["防火墙规则状态"])
    output.extend(security_messages)

    with open(output_file, 'w') as file:
        for line in output:
            file.write(line + '\n')

    # 打印输出文件路径
    print(f"输出文件路径: {output_file}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="网络参数检测工具")
    parser.add_argument('-i', '--input', help="param.json文件的输入路径")
    parser.add_argument('-o', '--output', help="输出文件的输出路径")

    args = parser.parse_args()

    processor = ExperimentProcessor(input_base_dir="/uploadPath/reasoning", output_base_dir="/uploadPath/reasoning")
    if args.input and args.output:
        input_file = args.input
        output_file = args.output
    else:
        input_file, output_file = processor.process_paths()

    main(input_file, output_file)
