import telnetlib
import re
import requests
import json
from concurrent.futures import ThreadPoolExecutor

class RouterInfoFetcher:
    def __init__(self, host, eve_ng_server, lab_path, session_id):
        self.host = host
        self.eve_ng_server = eve_ng_server
        self.lab_path = lab_path
        self.session_id = session_id

    def fetch_ports_from_eve_ng(self):
        url = f"{self.eve_ng_server}{self.lab_path}"
        headers = {
            'Content-type': 'application/json'
        }
        cookies = {
            'unetlab_session': self.session_id
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

    def fetch_info_from_router(self, port):
        telnet_client = self.TelnetClient(self.host, port)

        try:
            routing_table_output = telnet_client.execute_command('display ip routing-table')
            ospf_neighbors_output = telnet_client.execute_command('display ospf peer')
            bgp_neighbors_output = telnet_client.execute_command('display bgp peer')
        finally:
            telnet_client.close()

        return {
            "Routing Table": routing_table_output,
            "OSPF Neighbors": ospf_neighbors_output,
            "BGP Peers": bgp_neighbors_output
        }

    def parse_bgp_output(self, output):
        bgp_info = {}
        router_id_match = re.search(r"BGP local router ID\s+:\s+([\d\.]+)", output)
        local_as_match = re.search(r"Local AS number\s+:\s+(\d+)", output)
        
        if router_id_match and local_as_match:
            bgp_info['Router ID'] = router_id_match.group(1)
            bgp_info['Local AS'] = local_as_match.group(1)
        
        peers = []
        peer_matches = re.finditer(r"(\d+\.\d+\.\d+\.\d+)\s+\d+\s+(\d+)", output)
        for match in peer_matches:
            peer_info = {
                'Peer': match.group(1),
                'AS': match.group(2)
            }
            peers.append(peer_info)
        
        bgp_info['Peers'] = peers
        return bgp_info

    def parse_ospf_output(self, output):
        ospf_info = {}
        router_id_match = re.search(r"OSPF Process \d+ with Router ID\s+([\d\.]+)", output)
        
        if router_id_match:
            ospf_info['Router ID'] = router_id_match.group(1)
        
        neighbors = []
        neighbor_matches = re.finditer(r"Router ID:\s+([\d\.]+)\s+Address:\s+([\d\.]+)", output)
        for match in neighbor_matches:
            neighbor_info = {
                'Neighbor Router ID': match.group(1),
                'Neighbor Address': match.group(2)
            }
            neighbors.append(neighbor_info)
        
        ospf_info['Neighbors'] = neighbors
        return ospf_info

    def parse_routing_table(self, routing_table_output):
        routes = []
        lines = routing_table_output.split('\r\n')
        for line in lines:
            match = re.match(r'\s*(\d+\.\d+\.\d+\.\d+/\d+)\s+(\S+)\s+\d+\s+\d+\s+\w*\s+(\d+\.\d+\.\d+\.\d+)\s+(\S+)', line)
            if match:
                route = {
                    "Destination": match.group(1),
                    "Protocol": match.group(2),
                    "NextHop": match.group(3),
                    "Interface": match.group(4)
                }
                routes.append(route)
        return routes

    def filter_protocol(self, routes, protocol):
        return [route for route in routes if protocol in route["Protocol"]]

    def save_to_file(self, data, filename):
        with open(filename, "w") as file:
            json.dump(data, file, indent=4)

    def run(self):
        ports = self.fetch_ports_from_eve_ng()
        routers = [{"port": port} for port in ports]

        routing_table_results = {}
        ospf_results = {}
        bgp_results = {}

        with ThreadPoolExecutor(max_workers=len(routers)) as executor:
            futures = {executor.submit(self.fetch_info_from_router, router["port"]): f"Router_{i+1}" for i, router in enumerate(routers)}

        for future in futures:
            try:
                result = future.result()
                router_id = futures[future]
                routing_table_results[router_id] = result["Routing Table"]
                ospf_results[router_id] = result["OSPF Neighbors"]
                bgp_results[router_id] = result["BGP Peers"]
            except Exception as e:
                print(f"Error fetching info for {futures[future]}: {e}")

        parsed_bgp_data = {router: self.parse_bgp_output(output) for router, output in bgp_results.items()}
        parsed_ospf_data = {router: self.parse_ospf_output(output) for router, output in ospf_results.items()}

        all_routes = []
        for routing_table in routing_table_results.values():
            all_routes.extend(self.parse_routing_table(routing_table))

        ospf_routes = self.filter_protocol(all_routes, "OSPF")
        bgp_routes = self.filter_protocol(all_routes, "BGP")

        self.save_to_file(parsed_bgp_data, "parsed_bgp_data.json")
        self.save_to_file(parsed_ospf_data, "parsed_ospf_data.json")
        self.save_to_file(ospf_routes, "ospf_routes.json")
        self.save_to_file(bgp_routes, "bgp_routes.json")

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

        def read_until(self, expected, timeout=2):
            return self.telnet.read_until(expected.encode('ascii'), timeout)

        def write(self, data):
            self.telnet.write(data.encode('ascii') + b"\n")

        def execute_command(self, command):
            try:
                self.write(command)
                result = self.read_until('<', timeout=2)
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


if __name__ == "__main__":
    host = "192.168.3.100"
    eve_ng_server = "http://192.168.3.100"
    lab_path = "/api/labs/test.unl/nodes"
    session_id = "388fc6f1-a36e-40d3-a816-2775103761f2"
    
    fetcher = RouterInfoFetcher(host, eve_ng_server, lab_path, session_id)
    fetcher.run()
