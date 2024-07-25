import telnetlib
import re
import requests
import json
from concurrent.futures import ThreadPoolExecutor

class TelnetClient:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.telnet = None
        self.connect()

    def connect(self):
        try:
            self.telnet = telnetlib.Telnet(self.host, self.port)
            print(f"Connected to {self.host}:{self.port}")
            self.write('screen-length 0 temporary')  # Disable pagination
            self.read_until('<')  # Wait for the command to complete
        except Exception as e:
            print(f"Failed to connect to {self.host}:{self.port} - {e}")

    def read_until(self, expected, timeout=5):
        return self.telnet.read_until(expected.encode('ascii'), timeout)

    def write(self, data):
        self.telnet.write(data.encode('ascii') + b"\n")

    def execute_command(self, command):
        try:
            self.write(command)
            result = self.read_until('<', timeout=5)
            output = result.decode('ascii')
            print(f"Command: {command}\nOutput:\n{output}")
            return output
        except Exception as e:
            print(f"Failed to execute command {command} - {e}")
            return ""

    def close(self):
        if self.telnet:
            self.telnet.close()
            print(f"Closed connection to {self.host}:{self.port}")

def fetch_info_from_router(host, port):
    print(f"Connecting to Host: {host} on Port: {port}")

    telnet_client = TelnetClient(host, port)

    try:
        routing_table_output = telnet_client.execute_command('display ip routing-table')
        ospf_neighbors_output = telnet_client.execute_command('display ospf peer')
        bgp_neighbors_output = telnet_client.execute_command('display bgp peer')
    finally:
        telnet_client.close()
        print(f"Disconnected from Host: {host} on Port: {port}")

    return {
        "Routing Table": routing_table_output,
        "OSPF Neighbors": ospf_neighbors_output,
        "BGP Peers": bgp_neighbors_output
    }

def fetch_ports_from_eve_ng():
    eve_ng_server = "http://192.168.3.100"
    lab_path = "/api/labs/test.unl/nodes"
    url = f"{eve_ng_server}{lab_path}"

    headers = {
        'Content-type': 'application/json'
    }

    cookies = {
        'unetlab_session': '388fc6f1-a36e-40d3-a816-2775103761f2'
    }

    response = requests.get(url, headers=headers, cookies=cookies)
    ports = []

    if response.status_code == 200:
        data = response.json()['data']
        for node_info in data.values():
            url = node_info.get('url', '')
            port_match = re.search(r":(\d+)", url)
            if port_match:
                ports.append(int(port_match.group(1)))
    else:
        print(f"Failed to retrieve data from EVE-NG. Status code: {response.status_code}")
        print("Response:", response.text)

    return ports

if __name__ == "__main__":
    host = "192.168.3.100"
    ports = fetch_ports_from_eve_ng()

    routers = [{"port": port} for port in ports]

    routing_table_results = {}
    ospf_results = {}
    bgp_results = {}

    with ThreadPoolExecutor(max_workers=len(routers)) as executor:
        futures = {executor.submit(fetch_info_from_router, host, router["port"]): f"Router_{i+1}" for i, router in enumerate(routers)}

    for future in futures:
        try:
            result = future.result()
            router_id = futures[future]
            routing_table_results[router_id] = result["Routing Table"]
            ospf_results[router_id] = result["OSPF Neighbors"]
            bgp_results[router_id] = result["BGP Peers"]
        except Exception as e:
            print(f"Error fetching info for {futures[future]}: {e}")

    with open("routing_table_info.json", 'w') as rt_file:
        json.dump(routing_table_results, rt_file, indent=4)

    with open("ospf_info.json", 'w') as ospf_file:
        json.dump(ospf_results, ospf_file, indent=4)

    with open("bgp_info.json", 'w') as bgp_file:
        json.dump(bgp_results, bgp_file, indent=4)

    print("Routing table information has been saved to routing_table_info.json.")
    print("OSPF information has been saved to ospf_info.json.")
    print("BGP information has been saved to bgp_info.json.")
