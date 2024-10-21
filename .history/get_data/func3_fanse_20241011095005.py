import xml.etree.ElementTree as ET
import json
import telnetlib
import os
import argparse
import time
from concurrent.futures import ThreadPoolExecutor, as_completed


class UNLParser:
    def __init__(self, unl_file):
        self.unl_file = unl_file
        self.nodes = {}
        self.networks = {}

    def parse(self):
        """解析.unl文件，提取节点和网络信息。"""
        tree = ET.parse(self.unl_file)
        root = tree.getroot()

        # 提取节点信息
        for node in root.findall(".//node"):
            node_id = node.get('id')
            node_name = node.get('name')
            interfaces = []

            for interface in node.findall("interface"):
                interfaces.append({
                    'id': interface.get('id'),
                    'name': interface.get('name'),
                    'network_id': interface.get('network_id')
                })

            self.nodes[node_id] = {
                'name': node_name,
                'interfaces': interfaces
            }

        # 提取网络信息
        for network in root.findall(".//network"):
            network_id = network.get('id')
            network_name = network.get('name')
            self.networks[network_id] = network_name

        print("Parsed UNL File:")
        print("Nodes:", self.nodes)
        print("Networks:", self.networks)


class RouterTelnetConnection:
    def __init__(self, host, port, timeout=10):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.tn = None
        self.prompt = b'>'

    def connect(self):
        """建立Telnet连接并进行初始设置。"""
        try:
            self.tn = telnetlib.Telnet(self.host, self.port, timeout=self.timeout)
            initial_output = self._read_until_prompt()
            # 提取提示符
            if initial_output:
                lines = initial_output.splitlines()
                for line in reversed(lines):
                    line = line.strip()
                    if line.startswith('<') and line.endswith('>'):
                        self.prompt = line.encode('ascii') + b' '
                        break
            self.tn.write(b'\n')  # 确保处于提示符
            self._read_until_prompt()
            print(f"Connected to {self.host}:{self.port} with prompt {self.prompt.decode('ascii')}")
        except Exception as e:
            print(f"Error connecting to {self.host}:{self.port} - {e}")
            self.tn = None

    def send_command(self, command, wait_time=1):
        """发送命令到Telnet会话并返回输出。"""
        if not self.tn:
            print(f"No active Telnet connection to {self.host}:{self.port}")
            return None
        try:
            print(f"Sending command to {self.host}:{self.port}: {command}")
            self.tn.write(command.encode('ascii') + b'\n')
            time.sleep(wait_time)  # 等待命令执行
            output = self._read_until_prompt()
            print(f"Received output from {self.host}:{self.port} for command '{command}':\n{output}")
            return output
        except Exception as e:
            print(f"Error sending command to {self.host}:{self.port} - {e}")
            return None

    def _read_until_prompt(self):
        """读取数据直到提示符被发现。"""
        try:
            output = self.tn.read_until(self.prompt, timeout=self.timeout)
            return output.decode('ascii')
        except Exception as e:
            print(f"Error reading from {self.host}:{self.port} - {e}")
            return ""

    def close(self):
        """关闭Telnet连接。"""
        if self.tn:
            self.tn.close()
            print(f"Closed connection to {self.host}:{self.port}")
            self.tn = None


class RouterTelnetManager:
    def __init__(self, telnet_info, max_workers=10):
        self.telnet_info = telnet_info
        self.sysnames = {}
        self.neighbor_data = {}
        self.routing_tables = {}
        self.bgp_summaries = {}  # 用于存储 BGP Summary 信息
        self.max_workers = max_workers

    def connect_and_collect(self, node, protocols=('isis', 'bgp', 'mpls_ldp')):
        """连接单个路由器并收集sysname、邻居、路由表和BGP Summary信息。"""
        host = node["hostip"]
        port = node["port"]
        connection = RouterTelnetConnection(host, port)
        connection.connect()
        if not connection.tn:
            print(f"Skipping {host}:{port} due to connection issues.")
            return

        try:
            # 获取sysname
            sysname = self.get_sysname(connection)
            if sysname:
                self.sysnames[f"{host}:{port}"] = sysname
                print(f"Retrieved sysname for {host}:{port}: {sysname}")
            else:
                print(f"Failed to retrieve sysname for {host}:{port}")

            # 发送'scr 0 t'命令
            self.send_scr_command(connection)

            # 收集邻居信息
            node_neighbors = {}
            for protocol in protocols:
                output = self.get_neighbors(connection, protocol)
                if output:
                    neighbors = self.parse_neighbors(output, protocol)
                    node_neighbors[protocol] = neighbors
                    print(f"Parsed neighbors for protocol '{protocol}': {neighbors}")
                else:
                    node_neighbors[protocol] = []
            self.neighbor_data[f"{host}:{port}"] = node_neighbors
            print(f"Collected neighbors for {host}:{port}: {node_neighbors}")

            # 收集路由表
            routing_output = self.get_routing_table(connection)
            if routing_output:
                routing_entries = self.parse_routing_table(routing_output)
                self.routing_tables[f"{host}:{port}"] = routing_entries
                print(f"Collected routing table for {host}:{port}: {routing_entries}")
            else:
                print(f"Failed to retrieve routing table for {host}:{port}")

            # 收集 BGP Summary 信息
            bgp_summary_output = self.get_bgp_summary(connection)
            if bgp_summary_output:
                bgp_summary = self.parse_bgp_summary(bgp_summary_output)
                self.bgp_summaries[f"{host}:{port}"] = bgp_summary
                print(f"Collected BGP Summary for {host}:{port}: {bgp_summary}")
            else:
                print(f"Failed to retrieve BGP Summary for {host}:{port}")

        except Exception as e:
            print(f"Error processing {host}:{port} - {e}")
        finally:
            # 确保连接关闭
            connection.close()

    def collect_neighbors_and_routing(self, protocols=('isis', 'bgp', 'mpls_ldp')):
        """并行收集每个路由器的邻居、路由表和BGP Summary信息。"""
        nodes = self.telnet_info.get("node", [])
        if not nodes:
            print("No nodes found in the Telnet information.")
            return

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # 提交所有任务到执行器
            future_to_node = {executor.submit(self.connect_and_collect, node, protocols): node for node in nodes}

            # 处理完成的任务
            for future in as_completed(future_to_node):
                node = future_to_node[future]
                host = node.get("hostip")
                port = node.get("port")
                try:
                    future.result()
                except Exception as e:
                    print(f"Exception occurred while processing {host}:{port} - {e}")

    def get_sysname(self, connection):
        """从路由器获取sysname。"""
        try:
            output = connection.send_command('')
            lines = output.splitlines()
            sysname = None
            for line in lines:
                line = line.strip()
                if line.startswith('<') and line.endswith('>'):
                    sysname = line.strip('<> ')
                    break
            return sysname if sysname else None
        except Exception as e:
            print(f"Error retrieving sysname - {e}")
            return None

    def send_scr_command(self, connection):
        """发送'scr 0 t'命令到路由器。"""
        try:
            output = connection.send_command('scr 0 t', wait_time=2)
            print(f"Sent 'scr 0 t' to {connection.host}:{connection.port}")
            return output
        except Exception as e:
            print(f"Error sending 'scr 0 t' - {e}")
            return None

    def get_neighbors(self, connection, protocol):
        """获取指定协议的邻居信息。"""
        commands = {
            'isis': 'display isis peer',
            'bgp': 'display bgp peer',
            'mpls_ldp': 'display mpls ldp peer'
        }

        if protocol not in commands:
            raise ValueError(f"Unsupported protocol: {protocol}")

        output = connection.send_command(commands[protocol], wait_time=3)
        print(f"Raw output for {protocol} on {connection.host}:{connection.port}:\n{output}")
        return output

    def parse_neighbors(self, output, protocol):
        """根据协议解析邻居信息。"""
        neighbors = []
        if protocol == 'isis':
            in_peer_section = False
            for line in output.splitlines():
                line = line.strip()
                if "System Id" in line:
                    in_peer_section = True
                    continue
                if in_peer_section and len(line) > 0 and not line.startswith("---") and not line.startswith("<"):
                    columns = line.split()
                    if len(columns) > 0:
                        neighbors.append(columns[0])  # 只提取 System Id
                if "Total" in line:
                    break
        elif protocol == 'bgp':
            # 解析 BGP 邻居信息
            in_bgp_section = False
            for line in output.splitlines():
                if "Peer" in line and "AS" in line and "State" in line:
                    in_bgp_section = True
                    continue
                if in_bgp_section and line.strip() and not line.startswith("BGP local"):
                    peer_info = line.split()
                    if len(peer_info) >= 1:
                        neighbors.append(peer_info[0])  # Example: Neighbor IP
        elif protocol == 'mpls_ldp':
            # 解析 MPLS LDP 邻居信息
            in_ldp_section = False
            for line in output.splitlines():
                if "PeerID" in line and "TransportAddress" in line:
                    in_ldp_section = True
                    continue
                if in_ldp_section and line.strip() and ':' in line:
                    ldp_peer = line.split()[0]
                    neighbors.append(ldp_peer.split(':')[0])  # Example: '192.168.2.2:0'
        return neighbors

    def get_routing_table(self, connection):
        """获取路由表。"""
        output = connection.send_command('display ip routing-table', wait_time=5)
        print(f"Raw routing table output on {connection.host}:{connection.port}:\n{output}")
        return output

    def parse_routing_table(self, output):
        """解析路由表输出。"""
        routing_entries = []
        lines = output.splitlines()
        parsing = False
        for line in lines:
            line = line.strip()
            if line.startswith("Destination/Mask"):
                parsing = True
                continue
            if parsing:
                if not line or line.startswith("-"):
                    continue
                # 通过多个空格分割
                fields = line.split()
                if len(fields) < 7:
                    continue
                destination_mask = fields[0]
                proto = fields[1]
                pre = fields[2]
                cost = fields[3]
                flags = fields[4]
                next_hop = fields[5]
                interface = fields[6]
                routing_entries.append({
                    'Destination/Mask': destination_mask,
                    'Proto': proto,
                    'Pre': pre,
                    'Cost': cost,
                    'Flags': flags,
                    'NextHop': next_hop,
                    'Interface': interface
                })
        return routing_entries

    # 新增方法：获取 BGP Summary
    def get_bgp_summary(self, connection):
        """获取 BGP Summary 信息。"""
        output = connection.send_command('display bgp all summary', wait_time=3)
        print(f"Raw BGP Summary output on {connection.host}:{connection.port}:\n{output}")
        return output

    def parse_bgp_summary(self, output):
        """解析 BGP Summary 输出。"""
        bgp_summary = {
            'router_id': None,
            'local_as_number': None,
            'address_family': None,
            'total_peers': 0,
            'peers_established': 0,
            'peers': []
        }
        lines = output.splitlines()
        parsing_peers = False

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # 解析路由器ID
            if line.startswith("BGP local router ID"):
                parts = line.split(":")
                if len(parts) == 2:
                    bgp_summary['router_id'] = parts[1].strip()
                continue

            # 解析 Local AS number
            if line.startswith("Local AS number"):
                parts = line.split(":")
                if len(parts) == 2:
                    bgp_summary['local_as_number'] = parts[1].strip()
                continue

            # 解析 Address Family
            if line.startswith("Address Family"):
                parts = line.split(":")
                if len(parts) == 2:
                    bgp_summary['address_family'] = parts[1].strip()
                continue

            # 解析总对等体数量和已建立状态对等体数量
            if line.startswith("Total number of peers"):
                # Example line:
                # Total number of peers : 1                 Peers in established state : 0
                parts = line.split()
                try:
                    total_index = parts.index("peers") + 4  # 'peers' is part of 'peers : <number>'
                    total_peers = int(parts[5])
                    bgp_summary['total_peers'] = total_peers
                except (ValueError, IndexError):
                    pass

                try:
                    established_index = parts.index("established") + 4  # 'established' is part of 'established state : <number>'
                    peers_established = int(parts[9])
                    bgp_summary['peers_established'] = peers_established
                except (ValueError, IndexError):
                    pass
                continue

            # 解析 Peer 表头，开始解析对等体信息
            if line.startswith("Peer") and "AS" in line and "State" in line:
                parsing_peers = True
                continue

            if parsing_peers:
                # 假设对等体信息位于表头之后，直到遇到空行或其他非数据行
                if line.startswith("-") or line.startswith("Total"):
                    parsing_peers = False
                    continue
                fields = line.split()
                if len(fields) >= 7:
                    peer_ip = fields[0]
                    remote_as = fields[1]
                    msg_rcvd = fields[2]
                    msg_sent = fields[3]
                    out_q = fields[4]
                    up_down = fields[5]
                    state = fields[6]
                    # RtRcv 和 RtAdv 可能存在或缺失
                    rt_rcv = fields[7] if len(fields) > 7 else "0"
                    rt_adv = fields[8] if len(fields) > 8 else "0"

                    bgp_summary['peers'].append({
                        'Peer': peer_ip,
                        'RemoteAS': remote_as,
                        'MsgRcvd': msg_rcvd,
                        'MsgSent': msg_sent,
                        'OutQ': out_q,
                        'Up/Down': up_down,
                        'State': state,
                        'RtRcv': rt_rcv,
                        'RtAdv': rt_adv
                    })

        return bgp_summary


class TopologyMapper:
    def __init__(self, unl_parser, telnet_manager):
        self.unl_parser = unl_parser
        self.telnet_manager = telnet_manager
        # 如果需要，可以添加节点名称与sysname的映射
        self.name_mapping = {
            # "UNL节点名称": "sysname",
            # 根据实际情况添加更多映射
        }

    def map_topology(self):
        """将从Telnet获取的sysnames、neighbors、routing tables和BGP summaries映射到UNL拓扑中的节点。"""
        mapping = {}
        for node_id, node_info in self.unl_parser.nodes.items():
            node_name = node_info['name']
            # 使用映射字典获取对应的sysname（如果有映射）
            sysname = self.name_mapping.get(node_name, node_name)
            print(f"Mapping node '{node_name}' (ID: {node_id})")
            for host_port, collected_sysname in self.telnet_manager.sysnames.items():
                print(f"  Comparing with sysname '{collected_sysname}' from {host_port}")
                if sysname == collected_sysname:
                    # 获取路由表数据
                    routing_entries = self.telnet_manager.routing_tables.get(host_port, [])
                    print(f"    Found {len(routing_entries)} routing entries")
                    # 按协议组织路由表
                    routing_by_proto = {}
                    for entry in routing_entries:
                        proto = entry['Proto']
                        if proto not in routing_by_proto:
                            routing_by_proto[proto] = []
                        routing_by_proto[proto].append({
                            'Destination/Mask': entry['Destination/Mask'],
                            'Cost': entry['Cost'],
                            'NextHop': entry['NextHop'],
                            'Interface': entry['Interface']
                        })

                    # 获取 BGP Summary 数据
                    bgp_summary = self.telnet_manager.bgp_summaries.get(host_port, {})
                    print(f"    Found BGP Summary for {host_port}: {bgp_summary}")

                    mapping[node_id] = {
                        'node_name': node_name,
                        'host_port': host_port,
                        'sysname': collected_sysname,
                        'neighbors': self.telnet_manager.neighbor_data.get(host_port, {}),
                        'routing_table': routing_by_proto,  # 路由表数据
                        'bgp_summary': bgp_summary  # 新增 BGP Summary 数据
                    }
                    print(f"  Mapped node '{node_name}' to sysname '{collected_sysname}'")
        print("Mapping between UNL topology, Telnet sysnames, neighbors, routing tables, and BGP summaries:")
        print(json.dumps(mapping, indent=4))
        return mapping


def find_latest_folder(base_path):
    """在给定的基础路径下找到编号最大的文件夹。"""
    all_folders = [f for f in os.listdir(base_path) if f.isdigit()]
    if not all_folders:
        raise ValueError("No numbered folders found in the base path.")
    latest_folder = max(all_folders, key=int)
    return latest_folder


def main(input_path, output_path):
    base_path = "/uploadPath/reasoning"
    if "{t}" in input_path or "{t}" in output_path:
        latest_folder = find_latest_folder(base_path)
        input_path = input_path.replace("{t}", latest_folder)
        output_path = output_path.replace("{t}", latest_folder)

    with open(input_path, 'r') as f:
        telnet_info = json.load(f)

    lab_id = telnet_info.get("labId")
    if not lab_id:
        print("labId not found in the input JSON.")
        return

    unl_file_path = f"/opt/unetlab/labs/{lab_id}.unl"

    if not os.path.exists(unl_file_path):
        print(f"UNL file not found at {unl_file_path}")
        return

    unl_parser = UNLParser(unl_file_path)
    unl_parser.parse()

    telnet_manager = RouterTelnetManager(telnet_info)
    telnet_manager.collect_neighbors_and_routing(protocols=('isis', 'bgp', 'mpls_ldp'))

    mapper = TopologyMapper(unl_parser, telnet_manager)
    mapping = mapper.map_topology()

    with open(output_path, 'w') as f:
        f.write(json.dumps(mapping, indent=4))
    print(f"Mapping results with neighbors, routing tables, and BGP summaries written to {output_path}")


if __name__ == "__main__":
    # 设置命令行参数解析
    parser = argparse.ArgumentParser(description="Process network topology from UNL and param.json.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json, use {t} for latest folder number.")
    parser.add_argument("-o", "--output", required=True, help="Output path for process information, use {t} for latest folder number.")
    args = parser.parse_args()

    main(args.input, args.output)
