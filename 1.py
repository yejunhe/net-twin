import networkx as nx
import random
import copy
import math
import numpy as np
from scipy.optimize import minimize

# 随机拓扑生成函数
def Random_Topo(host_num, switch_num, link_num):
    net = nx.DiGraph()  # 创建一个有向图
    bw_dic = {}  # 带宽字典，记录每条链路的带宽
    hosts = []  # 主机列表
    switches = []  # 交换机列表

    # 添加主机节点
    for i in range(host_num):
        net.add_node('h' + str(i + 1))
        hosts.append('h' + str(i + 1))

    # 添加交换机节点
    for i in range(switch_num):
        net.add_node('s' + str(i + 1))
        switches.append('s' + str(i + 1))

    # 添加主机到交换机的链路
    for i in range(host_num):
        random_switch = switches[random.randint(0, switch_num - 1)]
        net.add_edge(hosts[i], random_switch)
        net.add_edge(random_switch, hosts[i])
        link_bw = random.randint(5000, 10000)  # 随机生成链路带宽，增加最小带宽
        bw_dic[(hosts[i], random_switch)] = link_bw
        bw_dic[(random_switch, hosts[i])] = link_bw

    # 添加交换机之间的基础链路
    for i in range(switch_num - 1):
        random_switch = switches[random.randint(0, switch_num - 1)]
        net.add_edge(switches[i], random_switch)
        net.add_edge(random_switch, switches[i])
        link_bw = random.randint(5000, 10000)  # 随机生成链路带宽，增加最小带宽
        bw_dic[(switches[i], random_switch)] = link_bw
        bw_dic[(random_switch, switches[i])] = link_bw

    # 添加交换机之间的冗余链路
    for i in range(link_num - switch_num):
        random_switch1 = switches[random.randint(0, switch_num - 1)]
        random_switch2 = switches[random.randint(0, switch_num - 1)]
        net.add_edge(random_switch1, random_switch2)
        net.add_edge(random_switch2, random_switch1)
        link_bw = random.randint(5000, 10000)  # 随机生成链路带宽，增加最小带宽
        bw_dic[(random_switch1, random_switch2)] = link_bw
        bw_dic[(random_switch2, random_switch1)] = link_bw

    return net, bw_dic, hosts

# 生成流量函数
def Produce_Flow(hosts, flow_num):
    flows = []  # 流量列表
    copy_flows = []  # 流量副本列表，用于备份
    max_priority = 0  # 最大优先级
    for _ in range(flow_num):
        src = random.choice(hosts)  # 随机选择源主机
        dst = random.choice([h for h in hosts if h != src])  # 随机选择目标主机，但不与源主机相同
        req_bw = random.randint(1, 500)  # 随机生成请求带宽，减少最大带宽请求
        priority = random.randint(1, 10)  # 随机生成优先级
        flows.append((src, dst, req_bw, priority))
        copy_flows.append((src, dst, req_bw, priority))
        if priority > max_priority:
            max_priority = priority
    return flows, copy_flows, max_priority

# 计算路径权重和成本的函数
def caculate_weight_graph_path_cost(src, dst, net, req_bw, bw_dic, max_priority, bw_cap):
    # 使用Dijkstra算法计算最短路径
    try:
        path = nx.dijkstra_path(net, src, dst)
        cost = sum([1 / bw_dic[(path[i], path[i + 1])] for i in range(len(path) - 1)])  # 使用1/带宽作为成本
        return path, cost
    except nx.NetworkXNoPath:
        return None, float('inf')

# 更新带宽的函数
def updata_bandwidth(path, req_bw, bw_dic):
    # 减少路径上每条链路的带宽
    for i in range(len(path) - 1):
        bw_dic[(path[i], path[i + 1])] -= req_bw
        bw_dic[(path[i + 1], path[i])] -= req_bw
    return bw_dic

# 生成假设的节点资源使用情况数据
def generate_node_usage_data(nodes):
    usage_data = {}
    for node in nodes:
        usage_data[node] = {
            'cpu_usage': random.uniform(0, 100),
            'bandwidth_utilization': random.uniform(0, 100),
            'interface_usage': random.uniform(0, 100),
            'traffic_rate': random.uniform(0, 1000),
            'memory_usage': random.uniform(0, 100),
            'disk_usage': random.uniform(0, 100)
        }
    return usage_data

# 调度流量函数
def schedule_flows(flows, net, bw_dic, bw_cap, max_priority, node_usage_data):
    accumulate_bw = 0  # 累积带宽
    total_bw = 0  # 总带宽，包括所有流的带宽请求
    suspend_flows = []  # 暂停的流量列表
    scheduled_paths = []  # 存储每个流量调度的路径信息

    for (src, dst, req_bw, priority) in flows:
        total_bw += req_bw
        # 检查网络中最小带宽是否能满足请求带宽
        if (min(bw_dic.values()) / math.log(2 * len(net.nodes()) * max_priority + 2, 2) >= req_bw):
            path, cost = caculate_weight_graph_path_cost(src, dst, net, req_bw, bw_dic, max_priority, bw_cap)
            N = len(net.nodes())
            benefit = N * priority
            if path is not None and cost <= benefit:
                bw_dic = updata_bandwidth(path, req_bw, bw_dic)
                accumulate_bw += req_bw
                path_usage = {node: node_usage_data[node] for node in path}
                scheduled_paths.append((src, dst, path, path_usage))
                print(f"Flow from {src} to {dst} scheduled successfully on path {path} with requested bandwidth {req_bw}")
            else:
                suspend_flows.append((src, dst))
                print(f"Flow from {src} to {dst} suspended due to high cost or no path")
        else:
            suspend_flows.append((src, dst))
            print(f"Flow from {src} to {dst} suspended due to insufficient bandwidth")

    # 计算最大链路负载
    max_load = 0.0
    for lsrc, ldst in bw_dic.keys():
        load = float((bw_cap[lsrc, ldst] - bw_dic[lsrc, ldst])) / bw_cap[lsrc, ldst]
        if load > max_load:
            max_load = load

    return total_bw, accumulate_bw, suspend_flows, max_load, scheduled_paths

# 主程序入口
if __name__ == "__main__":
    # 定义拓扑参数
    host_num = 42
    switch_num = 128
    link_num = int((host_num + switch_num) * (host_num + switch_num) / 4)

    # 生成随机拓扑
    net, bw_dic, hosts = Random_Topo(host_num, switch_num, link_num)
    bw_cap = copy.copy(bw_dic)

    # 生成流量
    flow_num = 2000
    flows, copy_flows, max_priority = Produce_Flow(hosts, flow_num)

    # 生成节点资源使用情况数据
    node_usage_data = generate_node_usage_data(net.nodes)

    # 调度流量
    total_bw, accumulate_bw, suspend_flows, max_load, scheduled_paths = schedule_flows(
        flows, net, bw_dic, bw_cap, max_priority, node_usage_data)

    # 输出结果
    print('累积带宽：', accumulate_bw)
    print('总带宽:', total_bw)
    print('丢掉比:', float('%.4f' % (1 - float(accumulate_bw) / total_bw)))
    print('最大链路负载：', float('%.4f' % max_load))
    print('网络节点数:', len(net.nodes()))

    # 输出调度路径和节点使用情况
    for src, dst, path, usage in scheduled_paths:
        print(f"Flow from {src} to {dst} scheduled on path: {path}")
        for node in path:
            print(f"Node {node} usage:")
            print(f"  CPU使用率: {usage[node]['cpu_usage']:.2f}%")
            print(f"  带宽利用率: {usage[node]['bandwidth_utilization']:.2f}%")
            print(f"  网络接口使用率: {usage[node]['interface_usage']:.2f}%")
            print(f"  流量速率: {usage[node]['traffic_rate']:.2f} Mbps")
            print(f"  内存使用率: {usage[node]['memory_usage']:.2f}%")
            print(f"  磁盘使用率: {usage[node]['disk_usage']:.2f}%")
