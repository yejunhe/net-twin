import os
import json


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

    def process(self):
        # 查找最新的输入和输出文件夹
        input_folder_path, folder_name = self.find_latest_folder(self.input_base_dir)
        output_folder_path = os.path.join(self.output_base_dir, folder_name)  # 输出文件夹与输入文件夹编号相同

        # 自动设置输入和输出路径
        input_file = os.path.join(input_folder_path, 'params', 'param.json')
        output_folder = os.path.join(output_folder_path, 'res')

        # 确保输出文件夹存在
        if not os.path.exists(output_folder):
            os.makedirs(output_folder)

        # 检查输入文件是否存在
        if not os.path.exists(input_file):
            raise FileNotFoundError(f"Input file not found: {input_file}")

        # 处理文件并生成输出
        with open(input_file, 'r') as infile:
            params = json.load(infile)

        result = f"Processed data: {json.dumps(params, indent=4)}"

        output_file_path = os.path.join(output_folder, 'data.txt')
        with open(output_file_path, 'w') as outfile:
            outfile.write(result)

        print(f"输入文件路径: {input_file}")
        print(f"输出结果路径: {output_file_path}")
        return output_file_path  # 如果你需要返回结果路径


# 使用示例
# if __name__ == "__main__":
#     processor = ExperimentProcessor(input_base_dir="/uploadPath/reasoning", output_base_dir="/uploadPath/reasoning")
#     processor.process()
# ```

### 类功能说明
#
# - ** `__init__(self, input_base_dir, output_base_dir)` **：构造函数，初始化输入和输出的基础路径。
# - ** `find_latest_folder(self, base_dir)` **：辅助方法，用于找到指定目录下最新（编号最大）的文件夹。
# - ** `process(self)` **：主方法，用于处理实验文件。它会找到最新的输入文件夹，读取
# `param.json`，然后将处理结果保存到
# `data.txt`。

### 使用类的方法

# 你可以通过以下方式使用这个类：
#
# 1. ** 创建类的实例 **：
# ```python
# processor = ExperimentProcessor(input_base_dir="/uploadPath/reasoning", output_base_dir="/uploadPath/reasoning")
# ```
#
# 2. ** 调用
# `process()`
# 方法 **：
# ```python
# processor.process()
# ```
#
# 这样，你就可以将功能封装成一个类，并在不同的地方轻松调用该类来处理实验文件。