import random
import os

class ExperimentProcessor:
    def __init__(self, input_base_dir, output_base_dir):
        self.input_base_dir = input_base_dir
        self.output_base_dir = output_base_dir

    def find_latest_folder(self, base_dir):
        # 获取reasoning目录下的所有文件夹
        folders = [f for f in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, f)) and f.isdigit()]

        if not folders:
            raise Exception(f"No folders found in {base_dir}")

        # 找到最新（最大编号）的文件夹
        latest_folder = max(folders, key=int)
        latest_folder_path = os.path.join(base_dir, latest_folder)

        return latest_folder_path, latest_folder

    def process_paths(self):
        # 查找最新的输入和输出文件夹
        input_folder_path, folder_name = self.find_latest_folder(self.input_base_dir)
        output_folder_path = os.path.join(self.output_base_dir, folder_name)  # 输出文件夹与输入文件夹编号相同

        # 自动设置输入和输出路径
        input_file = os.path.join(input_folder_path, 'params', 'param.json')
        output_folder = os.path.join(output_folder_path, 'res')

        # 确保输出文件夹存在
        if not os.path.exists(output_folder):
            os.makedirs(output_folder)

        output_file = os.path.join(output_folder, 'data.txt')

        # 返回输入文件路径和输出文件路径
        return input_file, output_file

# 模拟随机生成网络相关参数
def generate_network_parameters():
    return {
        "路由表更新次数": random.randint(0, 100),  # 路由表更新次数
        "STP状态": random.choice(["活跃", "非活跃", "错误"]),  # STP状态
        "数据流路径长度": random.randint(1, 20),  # 数据流路径长度
        "负载均衡比": round(random.uniform(0, 1), 3),  # 负载均衡比例，保留三位小数
        "ACL状态": random.choice(["有效", "无效"]),  # ACL状态
        "防火墙规则状态": random.choice(["有效", "无效"]),  # 防火墙规则状态
    }

# 检测路由不稳定
def check_route_stability(route_updates):
    if route_updates > 50:  # 设定阈值为50
        return "警告：由于频繁的路线更新，检测到路线不稳定."
    return "路线稳定性在可接受的范围内."

# 检测环路错误
def check_loop_errors(stp_status):
    if stp_status == "error":
        return "错误：由于STP配置错误，检测到循环."
    return "STP状态正常."

# 检测路径冗长或绕行
def check_path_efficiency(path_length):
    if path_length > 10:  # 设定阈值为10
        return "警告：路径效率低，检测到可能绕行."
    return "路径长度是有效的."

# 检测负载均衡失效
def check_load_balance(load_balance_ratio):
    if load_balance_ratio < 0.2 or load_balance_ratio > 0.8:  # 设定合理负载均衡比例范围
        return f"警告：检测到负载均衡失败，流量分布不均匀.负载均衡比: {load_balance_ratio:.3f}"
    return f"负载均衡功能正常.负载均衡比： {load_balance_ratio:.3f}"

# 检测ACL和防火墙策略失效
def check_security_policies(acl_status, firewall_rules_status):
    messages = []
    if acl_status == "无效":
        messages.append("错误：ACL配置无效。")
    if firewall_rules_status == "无效":
        messages.append("错误：防火墙规则配置无效。")

    return messages if messages else ["安全策略已正确配置."]

# 主检测函数
def main():

    network_params = generate_network_parameters()

    output = []
    output.append("网络参数: " + str(network_params))
    output.append(check_route_stability(network_params["路由表更新次数"]))
    output.append(check_loop_errors(network_params["STP状态"]))
    output.append(check_path_efficiency(network_params["数据流路径长度"]))
    output.append(check_load_balance(network_params["负载均衡比"]))
    security_messages = check_security_policies(network_params["ACL状态"], network_params["防火墙规则状态"])
    output.extend(security_messages)

    # 将结果保存到指定路径
    with open(output_file, 'w') as file:
        for line in output:
            file.write(line + '\n')

if __name__ == "__main__":
    # 使用 ExperimentProcessor 类来处理输入和输出路径
    processor = ExperimentProcessor(input_base_dir="/uploadPath/reasoning", output_base_dir="/uploadPath/reasoning")
    input_file, output_file = processor.process_paths()
    main()
