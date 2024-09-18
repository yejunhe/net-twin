#!/usr/bin/env python3



import ipaddress  
#from get_device_data import create_connection_to_mysql, execute_sql
from get_NE40_telnet import get_ip_tables

from get_device_param import get_json_content


host = "172.16.40.157"
ports = [(32899, 'R1'), (32901, 'R2'), (32900, 'R3'), (32902, 'R4'), (32898, 'R5'), (32897, 'R6')]

'''
def get_ip_list_backup(host, password):
    devices = {}
    connection, cursor = create_connection_to_mysql(host, password)
    sql_statement = "SELECT p.device_id, ip_a.ipv4_address, ip_a.ipv4_prefixlen FROM ipv4_addresses ip_a left join  ports p on ip_a.port_id = p.port_id where p.ifType = 'ethernetCsmacd';"
    sql_result = execute_sql(connection, cursor, sql_statement, False)
    for device in sql_result:
        device_id = device.get("device_id")
        if devices.get(device_id):
            devices[device_id].append(device.get("ipv4_address") + '/' + str(device.get("ipv4_prefixlen")))
        else:
            devices[device_id] = [device.get("ipv4_address") + '/' + str(device.get("ipv4_prefixlen"))]
    #print("devices = ", devices)
    return devices
'''
def get_ip_list(device_info_list):
    sql_result = get_ip_tables(device_info_list)
    #print("sql_result = ", sql_result) 
    devices = {}
    for device in sql_result:
        device_name = device.get("device_name")
        if devices.get(device_name):
            devices[device_name].add(device.get("ipv4_address") + '/' + str(device.get("ipv4_prefixlen")))
        else:
            devices[device_name] = set()
            devices[device_name].add(device.get("ipv4_address") + '/' + str(device.get("ipv4_prefixlen")))
    print("########devices = ", devices)
    return devices

def check_ip_conflict_data_process(devices):
    subnet_to_devices = {}
    #print("devices = ", devices)
    for device_id, ip_list in devices.items():
        for ip_ in ip_list:
            #print("ip_ = ", ip_)
            #print(f"device_id = {device_id} ip_list = {ip_list}")
            ip_str, prefixlen = ip_.split('/')
            
            ip = ipaddress.ip_address(ip_str)
            network = ipaddress.ip_network(ip_, strict = False)
            
            if network in subnet_to_devices:
                if ip in subnet_to_devices[network]:
                    #print(f"Conflict found for IP {ip_str} between devices {device_id} and {subnet_to_devices[network][ip]}")
                    subnet_to_devices[network][ip].append(device_id)
                else:
                    subnet_to_devices[network].setdefault(ip, []).append(device_id)
            else:
                subnet_to_devices[network] = {ip: [device_id]}
    
    #print("subnet_to_devices = ", subnet_to_devices)
    return subnet_to_devices

def check_ip_conflict(subnet_to_devices):
    check_result = {}
    for subnet, ip_dict in subnet_to_devices.items():
        for ip, device_list in ip_dict.items():
            if len(device_list) > 1:
                check_result[subnet] = {}
                check_result[subnet][ip] = device_list
            else:
                continue

    print("check_result = ", check_result)
    return check_result

def result_to_txt(result):
    result_str = str(result)
    with open('./ip_conflict.txt', 'w') as f:
        f.write(result_str + '\n')


if __name__ == "__main__":
    device_info_list = get_json_content('./param.json')
    #print("device_info_list = ", device_info_list)
    devices = get_ip_list(device_info_list)
    #print("devices = ", devices)
    #print("devices = ", devices)
    subnet_to_devices = check_ip_conflict_data_process(devices)
    result = check_ip_conflict(subnet_to_devices)
    result_to_txt(result)
    

