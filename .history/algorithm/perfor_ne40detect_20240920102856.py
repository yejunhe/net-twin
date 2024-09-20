import json
import telnetlib
import os
import argparse


class RouterTelnetManager:
    def __init__(self, telnet_info):
        self.telnet_info = telnet_info
        self.sysnames = {}
        self.ospf_routes = {}

    def get_sysname_and_routing_table(self, host, port):
        """Connect via Telnet to retrieve sysname and OSPF routing table."""
        try:
            tn = telnetlib.Telnet(host, port, timeout=10)

            # Send an empty command to ensure prompt is received
            tn.write(b'\n')

            sysname = None
            ospf_ip = None
            nqa_result = None
            while True:
                output = tn.read_until(b'>', timeout=5)
                lines = output.decode('ascii', errors='ignore').splitlines()

                # Check for sysname
                for line in lines:
                    line = line.strip()
                    if line.startswith('[') and line.endswith(']'):
                        tn.write(b'q\n')
                    elif line.startswith('<') and line.endswith('>'):
                        sysname = line.strip('<> ')
                        break

                if sysname:
                    print(f"Sysname for {host}:{port} is {sysname}")

                    # Send command to ensure proper routing table output
                    tn.write(b'scr 0 t\n')
                    tn.read_until(b'>', timeout=3)

                    # Send command to get routing table
                    tn.write(b'display ip routing-table\n')
                    routing_output = tn.read_until(b'>', timeout=5).decode('ascii', errors='ignore')
                    ospf_ip = self.parse_routing_table(routing_output)

                    if ospf_ip:
                        # Configure and execute NQA test
                        nqa_result = self.perform_nqa_test(tn, ospf_ip)
                        tn.close()
                        return sysname, ospf_ip, nqa_result
                    else:
                        tn.close()
                        return sysname, None, None

                else:
                    print(f"Waiting for correct prompt from {host}:{port}...")

        except Exception as e:
            print(f"Error connecting to {host}:{port} - {e}")
            return None, None, None

    def parse_routing_table(self, routing_table):
        """Parse the routing table to find the first OSPF destination IP."""
        for line in routing_table.splitlines():
            if 'OSPF' in line:
                parts = line.split()
                if parts:
                    # Extract destination IP and remove subnet mask if present
                    dest_ip = parts[0].split('/')[0]
                    print(f"Found OSPF route: {dest_ip}")
                    return dest_ip
        print("No OSPF route found")
        return None

    def perform_nqa_test(self, tn, dest_ip, max_attempts=5):
        """Configure and execute NQA test, attempt to retrieve results up to max_attempts."""
        try:
            # Enter system-view mode
            tn.write(b'system-view\n')
            tn.read_until(b']', timeout=3)

            # Configure NQA test instance
            nqa_commands = [
                b'nqa test-instance admin perfor_test\n',
                b'test-type icmpjitter\n',
                f'destination-address ipv4 {dest_ip}\n'.encode('ascii'),
                b'probe-count 2\n',
                b'interval milliseconds 100\n',
                b'timeout 1\n'
            ]
            for cmd in nqa_commands:
                tn.write(cmd)
                tn.read_until(b']', timeout=3)

            # Start the NQA test
            tn.write(b'start now\n')
            tn.read_until(b']', timeout=3)

            # Commit the configuration
            tn.write(b'commit\n')
            tn.read_until(b']', timeout=3)

            # Attempt to retrieve NQA test results
            attempt_count = 0
            result = ""
            while attempt_count < max_attempts:
                tn.write(b'display nqa results test-instance admin perfor_test\n')
                partial_output = tn.read_until(b'>', timeout=5).decode('ascii')
                result += partial_output

                if "The test is finished" in partial_output:
                    print(f"NQA test finished for {dest_ip} on attempt {attempt_count + 1}")
                    break

                attempt_count += 1
                print(f"Attempt {attempt_count}/{max_attempts} for NQA result on {dest_ip}...")

                if attempt_count >= max_attempts:
                    print(f"Max attempts reached for {dest_ip}. Test result may be incomplete.")
                    break

            # Log the raw NQA result
            print(f"NQA Test Result for {dest_ip}:")
            print(result)

            # Execute cleanup commands
            tn.write(b'stop\n')
            tn.read_until(b'>', timeout=3)

            tn.write(b'q\n')
            tn.read_until(b'>', timeout=3)

            tn.write(b'undo nqa test-instance admin perfor_test\n')
            tn.read_until(b']', timeout=3)

            tn.write(b'commit\n')
            tn.read_until(b']', timeout=3)

            # Parse and return performance metrics
            metrics = self.parse_nqa_result(result)
            if metrics is None:
                print(f"Failed to parse NQA results for {dest_ip}.")
            return metrics

        except Exception as e:
            print(f"Error during NQA test - {e}")
            return None

    def parse_nqa_result(self, nqa_result):
        """Parse NQA results to extract performance metrics."""
        metrics = {
            "latency": None,
            "jitter": None,
            "packet_loss": None
        }

        for line in nqa_result.splitlines():
            line = line.strip()
            # Parse RTT (Latency)
            if "Min/Max/Average RTT" in line:
                try:
                    # Example line: Min/Max/Average RTT:27/61/39/1571
                    rtt_values = line.split(":")[1].split("/")
                    if len(rtt_values) >= 3:
                        latency_avg = float(rtt_values[2])
                        metrics["latency"] = latency_avg
                        print(f"Parsed Latency (Average RTT): {latency_avg} ms")
                except (IndexError, ValueError) as e:
                    print(f"Failed to parse latency from line: '{line}'. Error: {e}")
                    metrics["latency"] = None

            # Parse Average of Jitter
            elif "Average of Jitter:" in line:
                try:
                    # Example line: Average of Jitter:5
                    jitter_value = float(line.split(":")[1].strip())
                    metrics["jitter"] = jitter_value
                    print(f"Parsed Jitter (Average of Jitter): {jitter_value} ms")
                except (IndexError, ValueError) as e:
                    print(f"Failed to parse jitter from line: '{line}'. Error: {e}")
                    metrics["jitter"] = None

            # Parse Packet Loss Ratio
            elif "Packet Loss Ratio:" in line:
                try:
                    # Example line: Packet Loss Ratio:0 %
                    packet_loss_str = line.split(":")[1].strip().replace('%', '')
                    packet_loss = float(packet_loss_str)
                    metrics["packet_loss"] = packet_loss
                    print(f"Parsed Packet Loss Ratio: {packet_loss} %")
                except (IndexError, ValueError) as e:
                    print(f"Failed to parse packet loss from line: '{line}'. Error: {e}")
                    metrics["packet_loss"] = None

        # Log the raw NQA result if all metrics are None for debugging
        if all(value is None for value in metrics.values()):
            print("All metrics are None. Raw NQA result:")
            print(nqa_result)

        return metrics

    def evaluate_network_performance(self, metrics):
        """Evaluate network performance based on extracted metrics."""
        evaluation = {
            "latency": "unknown",
            "jitter": "unknown",
            "packet_loss": "unknown",
            "overall_performance": "unknown"
        }

        # Define performance evaluation thresholds
        latency_thresholds = {"good": 50, "average": 100}
        jitter_thresholds = {"good": 20, "average": 50}
        packet_loss_thresholds = {"good": 1, "average": 5}

        # Evaluate Latency
        if metrics["latency"] is not None:
            if metrics["latency"] <= latency_thresholds["good"]:
                evaluation["latency"] = "good"
            elif metrics["latency"] <= latency_thresholds["average"]:
                evaluation["latency"] = "average"
            else:
                evaluation["latency"] = "poor"

        # Evaluate Jitter
        if metrics["jitter"] is not None:
            if metrics["jitter"] <= jitter_thresholds["good"]:
                evaluation["jitter"] = "good"
            elif metrics["jitter"] <= jitter_thresholds["average"]:
                evaluation["jitter"] = "average"
            else:
                evaluation["jitter"] = "poor"

        # Evaluate Packet Loss
        if metrics["packet_loss"] is not None:
            if metrics["packet_loss"] <= packet_loss_thresholds["good"]:
                evaluation["packet_loss"] = "good"
            elif metrics["packet_loss"] <= packet_loss_thresholds["average"]:
                evaluation["packet_loss"] = "average"
            else:
                evaluation["packet_loss"] = "poor"

        # Comprehensive Performance Evaluation
        if all(v == "good" for v in [evaluation["latency"], evaluation["jitter"], evaluation["packet_loss"]]):
            evaluation["overall_performance"] = "good"
        elif any(v == "poor" for v in [evaluation["latency"], evaluation["jitter"], evaluation["packet_loss"]]):
            evaluation["overall_performance"] = "poor"
        elif any(v == "average" for v in [evaluation["latency"], evaluation["jitter"], evaluation["packet_loss"]]):
            evaluation["overall_performance"] = "average"

        return evaluation

    def connect_and_get_sysnames_routes_and_nqa(self):
        """Connect to each router via Telnet, retrieve sysname, OSPF route, perform NQA test, and evaluate network performance."""
        results = []
        for node in self.telnet_info.get("node", []):
            host = node.get("hostip")
            port = node.get("port")
            if not host or not port:
                print(f"Invalid node configuration: {node}")
                continue

            sysname, ospf_ip, nqa_result = self.get_sysname_and_routing_table(host, port)
            if sysname:
                self.sysnames[f"{host}:{port}"] = sysname
                print(f"Connected to {host}:{port} - Sysname: {sysname}")
                result_data = {"host": host, "port": port, "sysname": sysname}

                if ospf_ip and nqa_result:
                    self.ospf_routes[f"{host}:{port}"] = {"ospf_ip": ospf_ip, "nqa_result": nqa_result}
                    print(f"OSPF route for {host}:{port}: {ospf_ip}")

                    # Parse NQA test results and evaluate network performance
                    performance_metrics = nqa_result
                    evaluation = self.evaluate_network_performance(performance_metrics)

                    result_data["ospf_ip"] = ospf_ip
                    result_data["nqa_result"] = performance_metrics
                    result_data["performance_evaluation"] = evaluation
                else:
                    result_data["ospf_ip"] = ospf_ip if ospf_ip else None
                    result_data["nqa_result"] = nqa_result if nqa_result else None
                    result_data["performance_evaluation"] = None
                    if not ospf_ip:
                        print(f"No OSPF route found for {host}:{port}")
                    if not nqa_result:
                        print(f"No NQA result available for {host}:{port}")

                # Append each node's results to the results list
                results.append(result_data)

            else:
                print(f"Failed to retrieve sysname for {host}:{port}")

        return results


def find_latest_folder(base_path):
    """Find the latest folder by number under the given base path."""
    all_folders = [f for f in os.listdir(base_path) if f.isdigit()]
    if not all_folders:
        raise ValueError("No numbered folders found in the base path.")
    latest_folder = max(all_folders, key=int)
    return latest_folder


def main(input_path, output_path):
    # Resolve the latest folder number if {t} is used
    base_path = "/uploadPath/reasoning"
    if "{t}" in input_path or "{t}" in output_path:
        latest_folder = find_latest_folder(base_path)
        input_path = input_path.replace("{t}", latest_folder)
        output_path = output_path.replace("{t}", latest_folder)

    # Load param.json
    try:
        with open(input_path, 'r') as f:
            telnet_info = json.load(f)
    except Exception as e:
        print(f"Failed to load input JSON file '{input_path}': {e}")
        return

    # Manage Telnet connections
    telnet_manager = RouterTelnetManager(telnet_info)
    results = telnet_manager.connect_and_get_sysnames_routes_and_nqa()

    # Output results to file
    try:
        with open(output_path, 'w') as f:
            f.write(json.dumps(results, indent=4))
        print(f"Results and performance evaluations written to {output_path}")
    except Exception as e:
        print(f"Failed to write output JSON file '{output_path}': {e}")


if __name__ == "__main__":
    # Set up argument parsing
    parser = argparse.ArgumentParser(description="Retrieve sysnames, OSPF routes, and perform NQA test via Telnet from routers.")
    parser.add_argument("-i", "--input", required=True, help="Path to param.json, use {t} for latest folder number.")
    parser.add_argument("-o", "--output", required=True, help="Output path for process information, use {t} for latest folder number.")
    args = parser.parse_args()

    main(args.input, args.output)
