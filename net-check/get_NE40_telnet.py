#!/usr/bin/env python3


import telnetlib

import time
import pdb
import re

host = "172.16.40.157"
ports = [(32899, 'R1'), (32901, 'R2'), (32900, 'R3'), (32902, 'R4'), (32898, 'R5'), (32897, 'R6')]
username = "humeng"
password = "Songshan@123"
TAG = "<HUAWEI>"
SYS_TAG = "\~HUAWEI"
AFTER_CONFIG_TAG = "\*HUAWEI"


def node_login_tmp(host, port, username, password, sysname):

    sysname_tag_usr = '<' + sysname + '>'
    sysname_tag_sys = '~' + sysname
    sysname_tag_configed = '*' + sysname 
    tn = telnetlib.Telnet(host, port)

    tn.read_until(b"Username:")
    tn.write(username.encode() + b"\n")
    tn.read_until(b"Password:")
    tn.write(password.encode() + b"\n")
    print(tn.read_until(b"The password needs to be changed. Change now? [Y/N]"))
    tn.write(b"N\r\n")

    ret = tn.expect([TAG.encode(), SYS_TAG.encode(), AFTER_CONFIG_TAG.encode(), sysname_tag_usr.encode()])
    index, m_object, content = ret

    print("ret = ", ret)
    if m_object.group(0).decode() == sysname_tag_usr:
        tn.write(b"system-view\r\n")
        print(tn.read_until(sysname_tag_sys.encode()).decode())
        return tn
    tn.write(b"system-view\r\n")
    config_sysname = "sysname " + sysname + "\r\n" 
    tn.write(config_sysname.encode())
    print("config_sysname = ", config_sysname)
    print("aaaaaaaa")
    tn.read_until("HUAWEI".encode())
    print("bbbbbbbb")
    tn.write(b"commit\r\n")
    print("ccccc") 
    tn.read_until("R1".encode())
    print("ddddd")
    
    return tn

def node_login(host, port):
    tn = telnetlib.Telnet(host, port)
    tn.read_some()
    a = tn.read_very_eager()
    ##print("a = ", a)
    tn.write(b'\r\n')
    a = tn.read_very_eager()
    #print("a = ", a)
    tn.write(b"system-view\r\n")
    tn.write(b"screen-length 0\r\n")
    tn.write(b"commit\r\n")
    return tn

def ip_config_str(ip_result):
    match_ip = r'\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b' 
    if type(ip_result) == list:
        
        result = ''.join(ip_result)
        result = result.split("\r\n")
        result = [' '.join(r.replace("\x1b[16D", '').split()).replace("---- More ----", ' ') for r in result]
        for str_ in result:
            if re.search(match_ip, str_):
                print(str_.strip().split())
        #print(result)


def exec_cmd_tmp(tn, cmd_str, end_tag, is_set):
    tmp_tag = ''
    result = []
    tag = [b'More', end_tag.encode()] if end_tag != None else [b'More']
    tn.write(cmd_str.encode() + b"\n")
    if is_set:
        tmp_tag = AFTER_CONFIG_TAG
    else:
        tmp_tag = SYS_TAG
   # print("tag = ", tag)
    while True:
        tmp = tn.expect(tag)
        index, m_object, content = tmp
        #print("tmp = ", tmp)
        if m_object.group(0).decode() != end_tag:
            tn.write(b"\r\n")
            result.append(content.decode())
        else:
            result.append(content.decode())
            #print("result = ", ''.join(result))
            tn.write(b'\r\n')
            return result

def exec_cmd(tn, cmd_str, is_set):
    cmd_encode = cmd_str + '\r\n'
    cmd_encode = cmd_encode.encode()
   # pdb.set_trace()
    tn.write(cmd_encode)
    #tn.write(b'\r\n')
    time.sleep(0.1)
    exec_result = tn.read_very_eager()
    return exec_result.decode()
    #print("exec_result = ", exec_result)

def bgp_routing_table_process(bgp_routing_table, sys_name):
    ip_result = []
    tmp = "        Network            NextHop                       MED        LocPrf    PrefVal Path/Ogn"
    route_list = bgp_routing_table.split('\r\n')
    if tmp not in route_list:
        return False
    index_head = route_list.index("        Network            NextHop                       MED        LocPrf    PrefVal Path/Ogn")
    route_list = route_list[index_head + 1 : ]
    for index, ip_str in enumerate(route_list):
        if '*>' not in ip_str:
            continue
        ip_seg = ip_str.split()
        #print("ip_seg = ", ip_seg)
        if ip_seg[-1].endswith('i') and len(ip_seg) == 5:
            ip_seg.insert(3, '##')
            ip_seg.insert(3, '##')
        elif len(ip_seg) == 6:
            if ip_seg[0] == '*>i': ip_seg.insert(3, '##')
            if ip_seg[0] == '*>': ip_seg.insert(4, '##')
        ip_seg.insert(0, sys_name)
        if len(ip_seg) < 8:
            return False
        ip_result.append(ip_seg)
    if len(ip_result) == 0:
        return False
    return ip_result

def ip_table_process(ip_table, sysname):
    ip_result = []
    tmp = 'Interface                         IP Address/Mask      Physical   Protocol VPN '
    ip_list = ip_table.split('\r\n')
    if tmp not in ip_list:
        return False
    index_head = ip_list.index(tmp)
    ip_list = ip_list[index_head + 1 : ]
    #print("ip_list = ", ip_list)
    for ip_item in ip_list:
        ip_item_list = ip_item.split()
        #print("ip_item_list = ", ip_item_list)
        if len(ip_item_list) < 5 or ip_item_list[1] == 'unassigned':
            continue
        ip_info_dic = {}
       # print("ip_item_list = ", ip_item_list)
        try:
        
            ip_info_dic['device_name'] = sysname
            ip_info_dic['ipv4_address'] = ip_item_list[1].split('/')[0]
            ip_info_dic['ipv4_prefixlen'] = ip_item_list[1].split('/')[1]
        except Exception as e:
            #pdb.set_trace()
            continue

        ip_result.append(ip_info_dic)

    return ip_result
        
    

def get_bgp_routing_table(tn):
    statement = "display bgp routing-table"
    result = exec_cmd(tn, statement, False)
    #print("result = ", result)
    return result

def get_bgp_routing_tables(device_info_list):
    bgp_route_list = []
    for device_info in device_info_list:
        #print("(port, sys_name) = ", (port, sys_name))
        host = device_info.get('hostip')
        port = device_info.get('port')
        sys_name = host + ':' + str(port)
        tn = node_login(host, port)
        while True:
            route_raw = get_bgp_routing_table(tn)
            route_list = bgp_routing_table_process(route_raw, sys_name)
            if route_list == False:
                continue
            else:
                break
        bgp_route_list.extend(route_list)
    bgp_route_result = [tuple(i) for i in bgp_route_list]
    bgp_route_result = set(bgp_route_result)
    return bgp_route_result

def get_ip_interface_table(tn):
    statement = "display ip interface brief"
    result = exec_cmd(tn, statement, False)
    
    return result

def get_ip_tables(device_info_list):
    ip_list = []
    for device_info in device_info_list:
        host = device_info.get('hostip')
        port = device_info.get('port')
        sysname = host + ':' + str(port)
        tn = node_login(host, port)
        while True:
            ip_raw = get_ip_interface_table(tn)
            #print("ip_raw = ", repr(ip_raw))
            processed = ip_table_process(ip_raw, sysname)
            #print("processed = ", processed)
            if processed == False:
                continue
            else:
                break
        ip_list.extend(processed)
    return ip_list

def main():
    tn = node_login(host, port, username, password, "R1")
    #print(tn.read_until(.encode()).decode())
    #exec_cmd(tn, "dis cur", "return", False)
    result = exec_cmd(tn, "display ip interface brief", 'R1', False)
    ip_config_str(result)
    #print(tn.read_until(SYS_TAG.encode()).decode())


#if __name__ == "__main__":
    #main()
#    result = get_bgp_routing_tables(host, ports)
#    print("result = ", result)

#    result = get_ip_tables(host, ports)
#    print("result = ", result)
