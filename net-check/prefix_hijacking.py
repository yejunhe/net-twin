#!/usr/bin/env python3


import ipaddress
from get_NE40_telnet import get_bgp_routing_tables
from get_device_param import get_json_content


class Prefix(object):
    def __init__(self, ip_str, as_num):
        self.ip_str = ip_str
        self.as_num = as_num
        
    def __hash__(self):
        return hash(self.ip_str)
    
    def __eq__(self, other):
        if isinstance(other, Prefix):
            return self.ip_str == other.ip_str
        return False

    def get_ip(self):
        return self.ip_str
    
    def get_as_num(self):
        return self.as_num

    def __str__(self):
        return f'({self.ip_str} : {self.as_num})'
   
    def __repr__(self):
        return self.__str__()

host = "172.16.40.157"
ports = [(32899, 'R1'), (32901, 'R2'), (32900, 'R3'), (32902, 'R4'), (32898, 'R5'), (32897, 'R6')]


def ip_to_binary(ip_str):
    ip = ipaddress.IPv4Address(ip_str)

    ip_int = int(ip)
    
    ip_bytes = ip_int.to_bytes(4, byteorder = 'big')
    
    binary_str = bin(int.from_bytes(ip_bytes, byteorder = 'big'))[2:].zfill(32)
    return binary_str


def raw_data_process(bgp_routing_data):
    ip_dic = {}
    for item in bgp_routing_data:
        ip_str = item[2].split('/')[0]
        ip_len = int(item[2].split('/')[1])
        as_num = item[-1]
        if as_num == 'i':
            continue
        ip_bina = ip_to_binary(ip_str)
        if ip_dic.get(ip_bina, None):
            ip_dic[ip_bina].add((ip_str, ip_len, as_num))
        else:
            ip_dic[ip_bina] = set()
            ip_dic[ip_bina].add((ip_str, ip_len, as_num))

    return ip_dic

def subnet_prefix_hijacking(ip_dic):
    ip_tuple = ip_dic.items()
    prefix_dic = {}
    ip_dic_sorted = sorted(ip_tuple, key = lambda i: tuple(i[1])[0][1])
    for index, ip_info in enumerate(ip_dic_sorted):
        ip_, len_as = ip_info
        len_as = tuple(len_as)[0]
        ip_str, prefix_len, as_ = len_as
        parant_prefix = Prefix(ip_str, as_)
        for ip_2, prefix_len_as in ip_dic_sorted[index + 1 : ]:
            ip_str_1, prefix_len_2, as_2 = tuple(prefix_len_as)[0]
            prefix = Prefix(ip_str_1, as_2)
            if ip_[: prefix_len] == ip_2[: prefix_len]:
                if prefix_dic.get(parant_prefix, None):
                    prefix_dic[parant_prefix].append(prefix)
                else:
                    prefix_dic[parant_prefix] = []
                    prefix_dic[parant_prefix].append(prefix)

    return prefix_dic
        
def result_to_txt(result):
    result_str = str(result)
    with open('./prefix_hijacking.txt', 'w') as f:
        f.write(result_str + '\n')

if __name__ == "__main__":
    device_info_list = get_json_content('./param.json')
    ip_raw_data = get_bgp_routing_tables(device_info_list)
    result = raw_data_process(ip_raw_data)
    
    result = subnet_prefix_hijacking(result)
    print("check result = ", result)
    result_to_txt(result)
    






