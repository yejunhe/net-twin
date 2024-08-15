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
        "网络连接状态": random.choice(["稳定", "不稳定", "断开"]),  # 网络连接状态
        "路由状态": random.choice(["正确", "不正确", "失败"]),  # 路由状态
        "ACL状态": random.choice(["有效", "丢失", "配置错误"]),  # ACL状态
        "QoS状态": random.choice(["最优", "次优", "配置错误"]),  # QoS状态
        "CPU使用率": round(random.uniform(0, 100), 3),  # CPU使用率，保留三位小数
        "内存使用率": round(random.uniform(0, 100), 3),  # 内存使用率，保留三位小数
    }

# 检测网络连接中断
def check_connection_status(connection_status):
    if connection_status != "稳定":
        return f"错误：网络连接{connection_status}."
    return "网络连接是稳定的."

# 检测路由错误
def check_route_status(route_status):
    if route_status != "正确":
        return f"错误：路由配置为{route_status}."
    return "路由配置是正确的."

# 检测ACL错误
def check_acl_status(acl_status):
    if acl_status != "有效":
        return f"错误：ACL是{acl_status}."
    return "ACL配置是有效的."

# 检测QoS策略错误
def check_qos_status(qos_status):
    if qos_status != "optimal":
        return f"错误：QoS配置是{qos_status}."
    return "QoS配置是最优的."

# 检测设备性能下降
def check_device_performance(cpu_usage, memory_usage):
    messages = []
    if cpu_usage > 85:  # 设定CPU使用率的阈值
        messages.append(f"警告：检测到CPU使用率高,为{cpu_usage:.3f}%.")
    if memory_usage > 85:  # 设定内存使用率的阈值
        messages.append(f"警告：在检测到内存使用率高,为{memory_usage:.3f}%.")
    return messages if messages else ["设备性能在可接受的范围内."]

# 主检测函数
def main():
    network_params = generate_network_parameters()

    output = []
    output.append("网络参数: " + str(network_params))
    output.append(check_connection_status(network_params["网络连接状态"]))
    output.append(check_route_status(network_params["路由状态"]))
    output.append(check_acl_status(network_params["ACL状态"]))
    output.append(check_qos_status(network_params["QoS状态"]))
    performance_messages = check_device_performance(network_params["CPU使用率"], network_params["内存使用率"])
    output.extend(performance_messages)

    # 将结果保存到指定路径
    with open(output_file, 'w') as file:
        for line in output:
            file.write(line + '\n')

if __name__ == "__main__":
    # 使用 ExperimentProcessor 类来处理输入和输出路径
    processor = ExperimentProcessor(input_base_dir="/uploadPath/reasoning", output_base_dir="/uploadPath/reasoning")
    input_file, output_file = processor.process_paths()
    main()
