import telnetlib
import re
import requests
from concurrent.futures import ThreadPoolExecutor


class TelnetClient:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.telnet = telnetlib.Telnet(self.host, self.port)

    def read_until(self, expected, timeout=10):
        return self.telnet.read_until(expected.encode('ascii'), timeout)

    def write(self, data):
        self.telnet.write(data.encode('ascii') + b"\n")

    def execute_command(self, command, callback):
        print(f"Executing command: {command}")  # Debug statement to trace command execution

        self.write(command)
        result = self.telnet.read_until(b"> ", timeout=10)
        output = result.decode('ascii')
        callback(output)

    def close(self):
        self.telnet.close()


class DeviceInfoFetcher:
    def __init__(self, client, output_file):
        self.client = client
        self.output_file = output_file
        self.output = ""
        self.target_ip = None

    def fetch_info(self):
        self.client.execute_command(' display ip routing-table', self.handle_routing_table)

    def handle_routing_table(self, output):
        self.output += "Routing Table:\n" + output + "\n\n"
        # Extract NextHop IP address with flags=RD from the routing table
        match = re.search(r'\S+\s+RD\s+(\d+\.\d+\.\d+\.\d+)', output)
        if match:
            self.target_ip = match.group(1)
            print(f"Extracted target IP with flags=RD: {self.target_ip}")
        else:
            print("No valid IP address with flags=RD found in the routing table.")
        self.client.execute_command(' display interface description', self.handle_interface_info)

    def handle_interface_info(self, output):
        self.output += "Interface Description:\n" + output
        if self.target_ip:
            self.test_ping()
        else:
            self.finalize_output()

    def test_ping(self):
        command = f' ping {self.target_ip}'
        self.client.execute_command(command, self.handle_ping_result)

    def handle_ping_result(self, output):
        self.output += f"\nPing result to {self.target_ip}:\n" + output
        self.finalize_output()

    def finalize_output(self):
        print(f"Finalizing output and writing to file: {self.output_file}")
        with open(self.output_file, 'w') as file:
            file.write(self.output)
        print(f"All information has been saved to {self.output_file}.")


class ProtocolNeighborFetcher:
    def __init__(self, client, output_file):
        self.client = client
        self.output_file = output_file
        self.output = ""

    def fetch_neighbors(self):
        # Fetch neighbors for each protocol
        self.client.execute_command(' display isis peer', self.handle_isis_neighbors)
        self.client.execute_command(' display bgp peer', self.handle_bgp_neighbors)
        self.client.execute_command(' display mpls ldp peer', self.handle_ldp_neighbors)

    def handle_isis_neighbors(self, output):
        self.output += "ISIS Neighbors:\n" + output + "\n\n"

    def handle_bgp_neighbors(self, output):
        self.output += "BGP Peers:\n" + output + "\n\n"

    def handle_ldp_neighbors(self, output):
        self.output += "MPLS LDP Peers:\n" + output + "\n\n"
        self.finalize_output()

    def finalize_output(self):
        print(f"Finalizing output and writing to file: {self.output_file}")
        with open(self.output_file, 'w') as file:
            file.write(self.output)
        print(f"All neighbor information has been saved to {self.output_file}.")


def fetch_info_from_router(host, port, output_file, neighbor_output_file):
    print(f"Connecting to Host: {host} on Port: {port}")

    telnet_client = TelnetClient(host, port)

    fetcher = DeviceInfoFetcher(telnet_client, output_file)
    fetcher.fetch_info()

    neighbor_fetcher = ProtocolNeighborFetcher(telnet_client, neighbor_output_file)
    neighbor_fetcher.fetch_neighbors()

    telnet_client.close()
    print(f"Disconnected from Host: {host} on Port: {port}")


def fetch_ports_from_eve_ng():
    # 设置 EVE-NG 服务器和 API 的详细信息
    eve_ng_server = "http://192.168.3.86"
    lab_path = "/api/labs/test.unl/nodes"
    url = f"{eve_ng_server}{lab_path}"

    # 定义请求的 headers
    headers = {
        'Content-type': 'application/json'
    }

    # 定义 cookie
    cookies = {
        'unetlab_session': '06876762-8177-4a12-a4c9-bd38d83afb08'
    }
    # 发送请求
    response = requests.get(url, headers=headers, cookies=cookies)

    ports = []
    if response.status_code == 200:
        data = response.json()['data']
        # 提取所有端口号
        for node_id, node_info in data.items():
            url = node_info.get('url', '')
            port_match = re.search(r":(\d+)", url)
            if port_match:
                ports.append(int(port_match.group(1)))
    else:
        print(f"Failed to retrieve data. Status code: {response.status_code}")
        print("Response:", response.text)

    return ports


if __name__ == "__main__":
    host = "192.168.3.86"
    ports = fetch_ports_from_eve_ng()  # 使用API从EVE-NG获取端口# Add more ports as needed

    routers = [
        {"port": port, "output_file": f"network_info_{i+1}.txt", "neighbor_output_file": f"neighbor_info_{i+1}.txt"}
        for i, port in enumerate(ports)
    ]

    with ThreadPoolExecutor(max_workers=len(routers)) as executor:
        futures = [
            executor.submit(fetch_info_from_router, host, router["port"], router["output_file"], router["neighbor_output_file"])
            for router in routers
        ]

    for future in futures:
        future.result()  # Wait for all futures to complete
