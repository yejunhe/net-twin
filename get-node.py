import xml.etree.ElementTree as ET
import os

class NodeReader:
    def __init__(self, input_file_name, output_file_name):
        self.input_file_path = os.path.join('/opt/unetlab/labs', input_file_name)
        self.output_file_path = os.path.join(os.getcwd(), output_file_name)
        self.tree = None
        self.root = None
        self.nodes = None
        self.networks = None

    def parse_file(self):
        if not os.path.exists(self.input_file_path):
            print(f"File {self.input_file_path} does not exist.")
            return False
        
        self.tree = ET.parse(self.input_file_path)
        self.root = self.tree.getroot()

        self.nodes = self.root.find('topology').find('nodes')
        self.networks = self.root.find('topology').find('networks')
        return True

    def write_nodes(self, file):
        file.write("Nodes:\n")
        for node in self.nodes.findall('node'):
            node_id = node.attrib.get('id')
            node_name = node.attrib.get('name')
            file.write(f"  Node ID: {node_id}, Name: {node_name}\n")

    def write_links(self, file):
        file.write("\nLinks:\n")
        for network in self.networks.findall('network'):
            network_id = network.attrib.get('id')
            connected_nodes = self.get_connected_nodes(network_id)
            if len(connected_nodes) > 1:
                self.write_network_links(file, connected_nodes)

    def get_connected_nodes(self, network_id):
        connected_nodes = []
        for node in self.nodes.findall('node'):
            for interface in node.findall('interface'):
                if interface.attrib.get('network_id') == network_id:
                    connected_nodes.append((node.attrib.get('id'), node.attrib.get('name'), interface.attrib.get('name')))
        return connected_nodes

    def write_network_links(self, file, connected_nodes):
        for i in range(len(connected_nodes)):
            for j in range(i + 1, len(connected_nodes)):
                node1 = connected_nodes[i]
                node2 = connected_nodes[j]
                file.write(f"  {node1[1]} (Interface {node1[2]}) <--> {node2[1]} (Interface {node2[2]})\n")

    def write_output(self):
        with open(self.output_file_path, 'w') as file:
            self.write_nodes(file)
            self.write_links(file)
        print(f"Output written to {self.output_file_path}")

def parse_unl_file():
    reader = NodeReader('test.unl', 'node_link.txt') #第一个为实验文件名，第二个为输出结果文件名
    if reader.parse_file():
        reader.write_output()

if __name__ == "__main__":
    parse_unl_file()
