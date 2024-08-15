import json
import networkx as nx
import matplotlib.pyplot as plt

# 读取 JSON 文件
with open('E:/Git/data.json', 'r') as f:
    data = json.load(f)

# 创建一个有向图
G = nx.Graph()

# 添加节点
for node in data['node']:
    G.add_node(node['name'], pos=(node['lon'], node['lat']))

# 添加边
network_id_to_name = {i+1: network['name'] for i, network in enumerate(data['network'])}

for node in data['node']:
    for interface in node.get('interface', []):
        network_id = interface['network_id']
        connected_network_name = network_id_to_name.get(network_id)
        if connected_network_name:
            for connected_node in data['node']:
                if connected_node == node:
                    continue
                for connected_interface in connected_node.get('interface', []):
                    if connected_interface['network_id'] == network_id:
                        G.add_edge(node['name'], connected_node['name'])

# 绘制图形
pos = nx.get_node_attributes(G, 'pos')
nx.draw(G, pos, with_labels=True, node_size=3000, node_color='skyblue', font_size=10, font_weight='bold')
plt.show()
