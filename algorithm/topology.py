#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler


class ArgumentParser:
    """解析命令行参数"""

    def __init__(self):
        self.parser = argparse.ArgumentParser(description='Eveng平台拓扑变更检测脚本（标准库版）')
        self.parser.add_argument('-i', '--input', required=True, help='param.json文件的输入路径')
        self.parser.add_argument('-o', '--output', required=True, help='变更结果输出路径')
        self.args = None

    def parse(self):
        self.args = self.parser.parse_args()
        return self.args


class ParamReader:
    """读取和解析param.json文件"""

    def __init__(self, param_path):
        self.param_path = param_path
        self.param = None

    def read_param(self):
        logging.info(f"读取参数文件: {self.param_path}")
        if not os.path.exists(self.param_path):
            logging.error(f"参数文件 {self.param_path} 不存在。")
            raise FileNotFoundError(f"参数文件 {self.param_path} 不存在。")
        with open(self.param_path, 'r', encoding='utf-8') as f:
            try:
                self.param = json.load(f)
                logging.info("参数文件读取成功。")
                return self.param
            except json.JSONDecodeError as e:
                logging.error(f"无法解析JSON文件 {self.param_path}。错误信息: {e}")
                raise ValueError(f"无法解析JSON文件 {self.param_path}。错误信息: {e}")


class UnlParser:
    """查找并解析.unl文件，将其转换为JSON格式"""

    LABS_DIR = '/opt/unetlab/labs'

    def __init__(self, lab_id):
        self.lab_id = lab_id
        self.unl_path = self.find_unl_file()
        self.lab_json = None

    def find_unl_file(self):
        unl_filename = f"{self.lab_id}.unl"
        unl_path = os.path.join(self.LABS_DIR, unl_filename)
        logging.info(f"查找.unl文件: {unl_path}")
        if not os.path.exists(unl_path):
            logging.error(f"未找到对应的.unl文件: {unl_path}")
            raise FileNotFoundError(f"未找到对应的.unl文件: {unl_path}")
        logging.info(f"找到.unl文件: {unl_path}")
        return unl_path

    def parse_unl_to_json(self):
        try:
            logging.info(f"解析.unl文件: {self.unl_path}")
            tree = ET.parse(self.unl_path)
            root = tree.getroot()
            lab_json = {}
            lab_json['lab'] = {
                'name': root.attrib.get('name'),
                'id': root.attrib.get('id'),
                'version': root.attrib.get('version'),
                'scripttimeout': root.attrib.get('scripttimeout'),
                'lock': root.attrib.get('lock')
            }

            # 解析拓扑
            topology = root.find('topology')
            if topology is not None:
                # 解析节点
                nodes = []
                nodes_elem = topology.find('nodes')
                if nodes_elem is not None:
                    for node in nodes_elem.findall('node'):
                        node_dict = node.attrib.copy()
                        interfaces = []
                        for interface in node.findall('interface'):
                            interfaces.append(interface.attrib.copy())
                        node_dict['interfaces'] = interfaces
                        nodes.append(node_dict)
                lab_json['topology'] = {'nodes': nodes}

                # 解析网络
                networks = []
                networks_elem = topology.find('networks')
                if networks_elem is not None:
                    for network in networks_elem.findall('network'):
                        networks.append(network.attrib.copy())
                lab_json['topology']['networks'] = networks

            self.lab_json = lab_json
            logging.info("解析.unl文件成功。")
            return lab_json
        except ET.ParseError as e:
            logging.error(f"无法解析XML文件 {self.unl_path}。错误信息: {e}")
            raise ValueError(f"无法解析XML文件 {self.unl_path}。错误信息: {e}")


class HistoryManager:
    """管理历史记录，包括加载和保存历史JSON文件"""

    HISTORY_DIR = '/opt/unetlab/labs_history'

    def __init__(self, lab_id):
        self.lab_id = lab_id
        self.history_path = self.get_history_path()

    def get_history_path(self):
        try:
            if not os.path.exists(self.HISTORY_DIR):
                logging.info(f"历史记录目录不存在，正在创建: {self.HISTORY_DIR}")
                os.makedirs(self.HISTORY_DIR, exist_ok=True)
                logging.info(f"历史记录目录创建成功: {self.HISTORY_DIR}")
            else:
                logging.info(f"历史记录目录已存在: {self.HISTORY_DIR}")
        except Exception as e:
            logging.error(f"无法创建历史记录目录 {self.HISTORY_DIR}。错误信息: {e}")
            raise IOError(f"无法创建历史记录目录 {self.HISTORY_DIR}。错误信息: {e}")
        return os.path.join(self.HISTORY_DIR, f"{self.lab_id}.json")

    def load_history(self):
        if not os.path.exists(self.history_path):
            logging.info(f"历史记录文件不存在: {self.history_path}")
            return None
        logging.info(f"加载历史记录文件: {self.history_path}")
        with open(self.history_path, 'r', encoding='utf-8') as f:
            try:
                history = json.load(f)
                logging.info("历史记录文件加载成功。")
                return history
            except json.JSONDecodeError as e:
                logging.warning(f"无法解析历史JSON文件 {self.history_path}。错误信息: {e}")
                return None

    def save_history(self, data):
        try:
            with open(self.history_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            logging.info(f"历史记录已保存到 {self.history_path}")
        except Exception as e:
            logging.error(f"无法保存历史记录到 {self.history_path}。错误信息: {e}")
            raise IOError(f"无法保存历史记录到 {self.history_path}。错误信息: {e}")


class TopologyComparator:
    """比较当前JSON与历史JSON，检测拓扑变化"""

    IGNORED_FIELDS = {'left', 'top', 'uuid'}

    def __init__(self, current, history):
        self.current = current
        self.history = history
        self.differences = {
            'added_nodes': [],
            'removed_nodes': [],
            'modified_nodes': [],
            'added_networks': [],
            'removed_networks': [],
            'modified_networks': []
        }

    def compare(self):
        if self.history is None:
            logging.info("无历史记录可供比较。")
            return None  # 无历史记录

        # 比较节点
        current_nodes = {node['id']: node for node in self.current['topology'].get('nodes', [])}
        history_nodes = {node['id']: node for node in self.history['topology'].get('nodes', [])}

        # 检测新增节点
        for node_id in current_nodes:
            if node_id not in history_nodes:
                self.differences['added_nodes'].append(current_nodes[node_id])

        # 检测删除节点
        for node_id in history_nodes:
            if node_id not in current_nodes:
                self.differences['removed_nodes'].append(history_nodes[node_id])

        # 检测修改节点
        for node_id in current_nodes:
            if node_id in history_nodes:
                filtered_current = self.filter_ignored_fields(current_nodes[node_id])
                filtered_history = self.filter_ignored_fields(history_nodes[node_id])
                if filtered_current != filtered_history:
                    changes = self.find_changes(filtered_history, filtered_current)
                    self.differences['modified_nodes'].append({
                        'id': node_id,
                        'changes': changes
                    })

        # 比较网络
        current_networks = {network['id']: network for network in self.current['topology'].get('networks', [])}
        history_networks = {network['id']: network for network in self.history['topology'].get('networks', [])}

        # 检测新增网络
        for network_id in current_networks:
            if network_id not in history_networks:
                self.differences['added_networks'].append(current_networks[network_id])

        # 检测删除网络
        for network_id in history_networks:
            if network_id not in current_networks:
                self.differences['removed_networks'].append(history_networks[network_id])

        # 检测修改网络
        for network_id in current_networks:
            if network_id in history_networks:
                filtered_current = self.filter_ignored_fields(current_networks[network_id])
                filtered_history = self.filter_ignored_fields(history_networks[network_id])
                if filtered_current != filtered_history:
                    changes = self.find_changes(filtered_history, filtered_current)
                    self.differences['modified_networks'].append({
                        'id': network_id,
                        'changes': changes
                    })

        # 移除空的类别
        self.differences = {k: v for k, v in self.differences.items() if v}
        return self.differences

    def filter_ignored_fields(self, item):
        """返回一个新的字典，移除被忽略的字段"""
        return {k: v for k, v in item.items() if k not in self.IGNORED_FIELDS}

    def find_changes(self, old, new):
        """找到两个字典之间的不同，返回变化的字段和其旧值、新值"""
        changes = {}
        for key in new:
            if key not in old:
                changes[key] = {'old': None, 'new': new[key]}
            elif new[key] != old[key]:
                changes[key] = {'old': old[key], 'new': new[key]}
        for key in old:
            if key not in new:
                changes[key] = {'old': old[key], 'new': None}
        return changes


class OutputWriter:
    """将检测到的变化内容输出到指定路径"""

    def __init__(self, output_path):
        self.output_path = output_path

    def format_diff(self, differences, execution_time, execution_result):
        report_lines = []
        report_lines.append("=" * 50)
        report_lines.append("拓扑变更检测报告")
        report_lines.append(f"执行时间：{execution_time.strftime('%Y-%m-%d %H:%M:%S')}")
        report_lines.append(f"执行结果：{execution_result}")
        report_lines.append("变更内容：")
        report_lines.append("-" * 50)

        if differences is None:
            report_lines.append("首次运行，无历史记录进行比较。")
        elif not differences:
            report_lines.append("无拓扑变化。")
        else:
            # 新增节点
            if 'added_nodes' in differences:
                report_lines.append("新增节点:")
                for node in differences['added_nodes']:
                    report_lines.append(f" - ID: {node['id']}, Name: {node.get('name', 'N/A')}, Image: {node.get('image', 'N/A')}")
            # 删除节点
            if 'removed_nodes' in differences:
                report_lines.append("删除节点:")
                for node in differences['removed_nodes']:
                    report_lines.append(f" - ID: {node['id']}, Name: {node.get('name', 'N/A')}, Image: {node.get('image', 'N/A')}")
            # 修改节点
            if 'modified_nodes' in differences:
                report_lines.append("修改节点:")
                for node_change in differences['modified_nodes']:
                    node_id = node_change['id']
                    changes = node_change['changes']
                    report_lines.append(f" - ID: {node_id}")
                    for key, change in changes.items():
                        if key == 'interfaces':
                            added_interfaces = self.get_added_interfaces(change['old'], change['new'])
                            removed_interfaces = self.get_removed_interfaces(change['old'], change['new'])
                            modified_interfaces = self.get_modified_interfaces(change['old'], change['new'])
                            if added_interfaces:
                                report_lines.append(f"   * 接口新增:")
                                for iface in added_interfaces:
                                    report_lines.append(f"     - ID: {iface['id']}, Name: {iface.get('name', 'N/A')}, Type: {iface.get('type', 'N/A')}, Network ID: {iface.get('network_id', 'N/A')}")
                            if removed_interfaces:
                                report_lines.append(f"   * 接口删除:")
                                for iface in removed_interfaces:
                                    report_lines.append(f"     - ID: {iface['id']}, Name: {iface.get('name', 'N/A')}, Type: {iface.get('type', 'N/A')}, Network ID: {iface.get('network_id', 'N/A')}")
                            if modified_interfaces:
                                report_lines.append(f"   * 接口修改:")
                                for iface_change in modified_interfaces:
                                    iface_id = iface_change['id']
                                    iface_changes = iface_change['changes']
                                    report_lines.append(f"     - ID: {iface_id}")
                                    for iface_key, iface_change_detail in iface_changes.items():
                                        report_lines.append(f"       * {iface_key}: {iface_change_detail['old']} → {iface_change_detail['new']}")
                        else:
                            report_lines.append(f"   * {key}: {change['old']} → {change['new']}")

            # 新增网络
            if 'added_networks' in differences:
                report_lines.append("新增链路:")
                for network in differences['added_networks']:
                    report_lines.append(f" - ID: {network['id']}, Name: {network.get('name', 'N/A')}, Type: {network.get('type', 'N/A')}")
            # 删除网络
            if 'removed_networks' in differences:
                report_lines.append("删除链路:")
                for network in differences['removed_networks']:
                    report_lines.append(f" - ID: {network['id']}, Name: {network.get('name', 'N/A')}, Type: {network.get('type', 'N/A')}")
            # 修改网络
            if 'modified_networks' in differences:
                report_lines.append("修改链路:")
                for network_change in differences['modified_networks']:
                    network_id = network_change['id']
                    changes = network_change['changes']
                    report_lines.append(f" - ID: {network_id}")
                    for key, change in changes.items():
                        report_lines.append(f"   * {key}: {change['old']} → {change['new']}")

        report_lines.append("=" * 50)
        return "\n".join(report_lines)

    def get_added_interfaces(self, old_interfaces, new_interfaces):
        """识别新增的接口"""
        old_ids = {iface['id'] for iface in old_interfaces}
        added = [iface for iface in new_interfaces if iface['id'] not in old_ids]
        return added

    def get_removed_interfaces(self, old_interfaces, new_interfaces):
        """识别删除的接口"""
        new_ids = {iface['id'] for iface in new_interfaces}
        removed = [iface for iface in old_interfaces if iface['id'] not in new_ids]
        return removed

    def get_modified_interfaces(self, old_interfaces, new_interfaces):
        """识别修改的接口"""
        old_dict = {iface['id']: iface for iface in old_interfaces}
        new_dict = {iface['id']: iface for iface in new_interfaces}
        modified = []
        for iface_id in new_dict:
            if iface_id in old_dict:
                filtered_old = self.filter_ignored_fields(old_dict[iface_id])
                filtered_new = self.filter_ignored_fields(new_dict[iface_id])
                if filtered_old != filtered_new:
                    changes = self.find_changes(filtered_old, filtered_new)
                    modified.append({
                        'id': iface_id,
                        'changes': changes
                    })
        return modified

    def filter_ignored_fields(self, item):
        """返回一个新的字典，移除被忽略的字段"""
        IGNORED_FIELDS = {'left', 'top', 'uuid'}
        return {k: v for k, v in item.items() if k not in IGNORED_FIELDS}

    def find_changes(self, old, new):
        """找到两个字典之间的不同"""
        changes = {}
        for key in new:
            if key not in old:
                changes[key] = {'old': None, 'new': new[key]}
            elif new[key] != old[key]:
                changes[key] = {'old': old[key], 'new': new[key]}
        for key in old:
            if key not in new:
                changes[key] = {'old': old[key], 'new': None}
        return changes

    def write_output(self, content):
        """将内容写入输出文件"""
        try:
            with open(self.output_path, 'w', encoding='utf-8') as f:
                f.write(content)
            logging.info(f"变更内容已输出到 {self.output_path}")
            print(f"变更内容已输出到 {self.output_path}")
        except Exception as e:
            logging.error(f"无法写入输出文件 {self.output_path}。错误信息: {e}")
            raise IOError(f"无法写入输出文件 {self.output_path}。错误信息: {e}")


class TopologyChangeDetector:
    """主控制类，协调各个组件完成拓扑变更检测"""

    def __init__(self, input_path, output_path):
        self.input_path = input_path
        self.output_path = output_path

    def run(self):
        start_time = datetime.now()
        execution_result = "成功"

        try:
            # 读取param.json
            param_reader = ParamReader(self.input_path)
            param = param_reader.read_param()

            lab_id = param.get('labId')
            if lab_id is None:
                raise ValueError("param.json 中缺少 'labId' 字段。")

            # 解析.unl文件
            unl_parser = UnlParser(lab_id)
            current_json = unl_parser.parse_unl_to_json()

            # 加载历史记录
            history_manager = HistoryManager(lab_id)
            history_json = history_manager.load_history()

            # 比较当前JSON与历史JSON
            comparator = TopologyComparator(current_json, history_json)
            differences = comparator.compare()

            # 格式化比较结果
            output_writer = OutputWriter(self.output_path)
            formatted_diff = output_writer.format_diff(differences, start_time, execution_result)

            # 写入输出文件
            output_writer.write_output(formatted_diff)

            # 更新历史记录
            history_manager.save_history(current_json)

        except Exception as e:
            execution_result = "失败"
            end_time = datetime.now()
            formatted_diff = self.format_error_report(start_time, end_time, str(e))
            try:
                OutputWriter(self.output_path).write_output(formatted_diff)
            except Exception as write_error:
                logging.error(f"无法写入错误报告到 {self.output_path}。错误信息: {write_error}")
            logging.error(f"脚本运行失败。错误信息: {e}")
            print(f"错误: {e}")
            sys.exit(1)

    def format_error_report(self, start_time, end_time, error_message):
        report_lines = []
        report_lines.append("=" * 50)
        report_lines.append("拓扑变更检测报告")
        report_lines.append(f"执行时间：{start_time.strftime('%Y-%m-%d %H:%M:%S')} 至 {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        report_lines.append("执行结果：失败")
        report_lines.append("变更内容：")
        report_lines.append("-" * 50)
        report_lines.append(f"错误信息: {error_message}")
        report_lines.append("=" * 50)
        return "\n".join(report_lines)


def setup_logging():
    """配置日志记录"""
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

    # 文件处理器，限制日志文件大小为5MB，保留5个备份
    try:
        file_handler = RotatingFileHandler("topology_change_detector.log", maxBytes=5*1024*1024, backupCount=5)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except Exception as e:
        print(f"无法创建日志文件处理器。错误信息: {e}")
        sys.exit(1)

    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)


def main():
    # 配置日志
    setup_logging()

    # 解析命令行参数
    arg_parser = ArgumentParser()
    args = arg_parser.parse()

    # 运行拓扑变更检测
    detector = TopologyChangeDetector(args.input, args.output)
    detector.run()


if __name__ == "__main__":
    main()
