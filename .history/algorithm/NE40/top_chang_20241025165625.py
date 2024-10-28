import json
import telnetlib
import os
import argparse
import sys
import time
import re
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

# Configure logging for better traceability and control
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

class RouterTelnetManager:
    def __init__(self, telnet_info):
        self.telnet_info = telnet_info
        self.sysnames = {}

    def get_sysname(self, tn: telnetlib.Telnet) -> Optional[str]:
        """通过 Telnet 获取节点的 sysname"""
        try:
            tn.write(b'\n')
            time.sleep(1)
            output = tn.read_very_eager().decode('ascii', errors='ignore')
            for line in output.splitlines():
                line = line.strip()
                if line.startswith('<') and line.endswith('>'):
                    sysname = line.strip('<> ').strip()
                    logging.info(f"[{tn.host}:{tn.port}] Detected sysname: {sysname}")
                    return sysname
                elif line.startswith('[') and line.endswith(']'):
                    sysname = line.strip('[] ').strip()
                    logging.info(f"[{tn.host}:{tn.port}] Detected sysname: {sysname}")
                    return sysname
            logging.warning(f"[{tn.host}:{tn.port}] No sysname detected.")
            return None
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] Telnet Error while getting sysname: {e}")
            return None

    def connect_and_get_sysnames(self, tn: telnetlib.Telnet):
        sysname = self.get_sysname(tn)
        if sysname:
            key = f"{tn.host}:{tn.port}"
            self.sysnames[key] = sysname
            logging.info(f"Connected to {tn.host}:{tn.port} - Sysname: {sysname}")
        else:
            logging.warning(f"Failed to retrieve sysname for {tn.host}:{tn.port}")

class OSPFDiagnostic:
    def __init__(self, ospf_outputs: Dict[str, Dict[str, str]]):
        self.ospf_outputs = ospf_outputs
        self.ospf_data = {}
        self.faults = []

    def parse_output(self, output: str) -> List[str]:
        """将 Telnet 输出分割为行"""
        return output.splitlines()

    def collect_ospf_info(self):
        """从输出中收集 OSPF 信息"""
        for host_port, output in self.ospf_outputs.items():
            self.ospf_data[host_port] = {
                'peer': self.parse_output(output.get('display ospf peer', '')),
                'interface': self.parse_output(output.get('display ospf interface', '')),
                'brief': self.parse_output(output.get('display ospf brief', ''))
            }
            logging.info(f"Collected OSPF info from {host_port}")

    def analyze_ospf_status(self):
        """Analyze the collected OSPF information for possible faults."""
        for router, info in self.ospf_data.items():
            logging.info(f"\nAnalyzing OSPF status for router {router}...")

            # Check OSPF peer status
            peer_status = info.get('peer', [])
            logging.info(f"Peer Status for {router}: {peer_status}")
            for line in peer_status:
                line_lower = line.lower()
                if 'init' in line_lower or 'down' in line_lower or 'attempt' in line_lower:
                    fault = f"OSPF邻居问题检测到 {router}: {line}"
                    self.faults.append((router, fault))
                    logging.warning(f"Detected Neighbor Issue: {line}")

            # Check OSPF interface configuration
            interface_status = info.get('interface', [])
            logging.info(f"Interface Status for {router}: {interface_status}")
            for line in interface_status:
                logging.debug(f"Analyzing interface line: {line}")  # Debug
                # 使用正则表达式提取接口状态
                match = re.search(r'state:\s*(\S+)', line, re.IGNORECASE)
                if match:
                    state = match.group(1).lower()
                    if state == 'down':
                        # 提取接口名称和IP地址
                        # 假设接口信息格式为 "Interface: <IP> (<Interface Name>)"
                        interface_match = re.search(r'Interface:\s*([\d\.]+)\s*\(([^)]+)\)', line, re.IGNORECASE)
                        if interface_match:
                            ip_address = interface_match.group(1)
                            interface_name = interface_match.group(2)
                            fault = f"OSPF接口 Down 在 {router}: 接口 {interface_name} ({ip_address})"
                            self.faults.append((router, fault))
                            logging.warning(f"Detected Interface Down: {line}")
                        else:
                            # 如果无法解析接口信息，则记录整个行
                            fault = f"OSPF接口 Down 在 {router}: {line}"
                            self.faults.append((router, fault))
                            logging.warning(f"Detected Interface Down: {line}")

            # Check OSPF brief information for interface states
            brief_status = info.get('brief', [])
            logging.info(f"Brief Status for {router}: {brief_status}")

            for line in brief_status:
                logging.debug(f"Analyzing brief line: {line}")  # Debug
                # 使用正则表达式提取接口状态
                match = re.search(r'Interface:\s*([\d\.]+)\s*\(([^)]+)\).*State:\s*(\S+)', line, re.IGNORECASE)
                if match:
                    ip_address = match.group(1)
                    interface_name = match.group(2)
                    state = match.group(3).lower()
                    if state == 'down':
                        fault = f"OSPF概要信息接口状态 Down 在 {router}: 接口 {interface_name} ({ip_address})"
                        self.faults.append((router, fault))
                        logging.warning(f"Detected Brief Interface Down: {line}")

            logging.info(f"Finished analyzing OSPF status for router {router}.")

    def get_faults(self) -> List[tuple]:
        return self.faults

class IsisFaultDetector:
    def __init__(self, isis_outputs: Dict[str, Dict[str, str]]):
        self.isis_outputs = isis_outputs
        self.fault_info = {}
        self.faults = []

    def send_command(self, tn: telnetlib.Telnet, command: str) -> str:
        try:
            tn.write(command.encode('ascii') + b'\n')
            time.sleep(1)
            output = tn.read_very_eager().decode('ascii', errors='ignore')
            logging.debug(f"[{tn.host}:{tn.port}] Command '{command}' output:\n{output}")
            return output
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] Error sending command '{command}': {e}")
            return ""

    def parse_display_isis_peer(self, output: str, host_port: str):
        total_peers = 0
        for line in output.splitlines():
            if line.strip().startswith("Total Peer(s):"):
                try:
                    total_peers = int(line.strip().split(":")[-1])
                    self.fault_info[host_port]["total_peers"] = total_peers
                    logging.info(f"[{host_port}] Total ISIS Peers: {total_peers}")
                except ValueError:
                    logging.error(f"[{host_port}] Unable to parse total peers from line: {line}")
        if total_peers == 0:
            logging.warning(f"[{host_port}] No ISIS peers detected.")

    def parse_display_isis_brief(self, output: str, host_port: str):
        for line in output.splitlines():
            if "L1 Lsp Over Flow:" in line:
                self.fault_info[host_port]["l1_lsp_overflow"] = "true" in line.lower()
            elif "L2 Lsp Over Flow:" in line:
                self.fault_info[host_port]["l2_lsp_overflow"] = "true" in line.lower()
            elif "Level-1 Avoid Redistribute Loop Capability:" in line:
                self.fault_info[host_port]["level1_avoid_redistribute_loop"] = "true" in line.lower()
            elif "Level-2 Avoid Redistribute Loop Capability:" in line:
                self.fault_info[host_port]["level2_avoid_redistribute_loop"] = "true" in line.lower()
        logging.info(f"[{host_port}] Parsed ISIS brief information.")

    def parse_display_isis_interface(self, output: str, host_port: str):
        ipv4_state_issues = []
        lines = output.splitlines()
        header_found = False
        for line in lines:
            if "Interface information for ISIS" in line:
                header_found = False  # Reset for new table
            if "Interface" in line and "IPV4.State" in line:
                header_found = True
                continue
            if header_found:
                if line.strip() == "":
                    break
                parts = line.split()
                if len(parts) >= 4:
                    interface = parts[0]
                    ipv4_state = parts[2]
                    if "DN" in ipv4_state.upper() or "DOWN" in ipv4_state.upper():
                        ipv4_state_issues.append({
                            "interface": interface,
                            "ipv4_state": ipv4_state
                        })
        self.fault_info[host_port]["ipv4_state_issues"] = ipv4_state_issues
        if ipv4_state_issues:
            logging.warning(f"[{host_port}] IPV4 State issues detected: {ipv4_state_issues}")
        else:
            logging.info(f"[{host_port}] No IPV4 State issues detected.")

    def detect_faults(self, tn: telnetlib.Telnet, host_port: str):
        output_present = any(self.isis_outputs.get(host_port, {}).values())
        if not output_present:
            logging.info(f"[{host_port}] 无ISIS相关输出，跳过ISIS故障检测。")
            return

        self.fault_info[host_port] = {
            "total_peers": 0,
            "l1_lsp_overflow": False,
            "l2_lsp_overflow": False,
            "level1_avoid_redistribute_loop": False,
            "level2_avoid_redistribute_loop": False,
            "ipv4_state_issues": []
        }

        # 1. display isis peer
        peer_output = self.send_command(tn, "display isis peer")
        self.parse_display_isis_peer(peer_output, host_port)

        # 2. display isis brief
        brief_output = self.send_command(tn, "display isis brief")
        self.parse_display_isis_brief(brief_output, host_port)

        # 3. display isis interface
        interface_output = self.send_command(tn, "display isis interface")
        self.parse_display_isis_interface(interface_output, host_port)

        # Analyze faults
        info = self.fault_info[host_port]
        if info["total_peers"] == 0:
            fault = f"ISIS邻居问题检测到 {host_port}: 无邻居"
            self.faults.append((host_port, fault))
            logging.warning(fault)
        if info["l1_lsp_overflow"]:
            fault = f"ISIS L1 LSP溢出在 {host_port}"
            self.faults.append((host_port, fault))
            logging.warning(fault)
        if info["l2_lsp_overflow"]:
            fault = f"ISIS L2 LSP溢出在 {host_port}"
            self.faults.append((host_port, fault))
            logging.warning(fault)
        if info["level1_avoid_redistribute_loop"]:
            fault = f"ISIS Level-1 避免重分发循环能力启用在 {host_port}"
            self.faults.append((host_port, fault))
            logging.warning(fault)
        if info["level2_avoid_redistribute_loop"]:
            fault = f"ISIS Level-2 避免重分发循环能力启用在 {host_port}"
            self.faults.append((host_port, fault))
            logging.warning(fault)
        for issue in info["ipv4_state_issues"]:
            fault = f"ISIS IPV4 State 异常在 {host_port}: 接口 {issue['interface']} 状态 {issue['ipv4_state']}"
            self.faults.append((host_port, fault))
            logging.warning(fault)

    def get_faults(self) -> List[tuple]:
        """Return a list of tuples containing (host_port, fault)."""
        return self.faults

class BgpErrorDetector:
    def __init__(self, bgp_outputs: Dict[str, str]):
        self.bgp_outputs = bgp_outputs
        self.bgp_errors = {}
        self.error_translation_map = {
            "Routes received with cluster ID loop": "收到的路由具有集群ID循环",
            "Routes received with as path count over limit": "收到的路由AS路径计数超过限制",
            "Routes advertised with as path count over limit": "通告的路由AS路径计数超过限制",
            "Routes received with As loop": "收到的路由存在AS环路",
            "Routes received with Zero RD(0:0)": "收到的路由具有零RD(0:0)",
            "Routes received with no prefix": "收到的路由没有前缀",
            "Routes received with error path-attribute": "收到的路由路径属性错误",
            "Routes received with originator ID loop": "收到的路由具有发起者ID循环",
            "Routes received with total number over limit": "收到的路由总数超过限制",
            "Routes received with error router id": "收到的路由具有错误的路由器ID"
        }

    def parse_bgp_error_statistics(self, output: str) -> Dict[str, int]:
        """Parses the BGP error statistics from the Telnet output using regular expressions."""
        pattern = re.compile(
            r'Routes received with cluster ID loop\s*:\s*(\d+)|'
            r'Routes received with as path count over limit\s*:\s*(\d+)|'
            r'Routes advertised with as path count over limit\s*:\s*(\d+)|'
            r'Routes received with As loop\s*:\s*(\d+)|'
            r'Routes received with Zero RD\(0:0\)\s*:\s*(\d+)|'
            r'Routes received with no prefix\s*:\s*(\d+)|'
            r'Routes received with error path-attribute\s*:\s*(\d+)|'
            r'Routes received with originator ID loop\s*:\s*(\d+)|'
            r'Routes received with total number over limit\s*:\s*(\d+)|'
            r'Routes received with error router id\s*:\s*(\d+)'
        )

        error_counts = {
            "Routes received with cluster ID loop": 0,
            "Routes received with as path count over limit": 0,
            "Routes advertised with as path count over limit": 0,
            "Routes received with As loop": 0,
            "Routes received with Zero RD(0:0)": 0,
            "Routes received with no prefix": 0,
            "Routes received with error path-attribute": 0,
            "Routes received with originator ID loop": 0,
            "Routes received with total number over limit": 0,
            "Routes received with error router id": 0,
        }

        for match in pattern.findall(output):
            for idx, value in enumerate(match):
                if value:
                    error_type = list(error_counts.keys())[idx]
                    error_counts[error_type] = int(value)

        return error_counts

    def detect_faults(self):
        for host_port, output in self.bgp_outputs.items():
            error_counts = self.parse_bgp_error_statistics(output)
            self.bgp_errors[host_port] = error_counts
            logging.info(f"[{host_port}] BGP error statistics: {error_counts}")

    def get_faults(self) -> Dict[str, Dict[str, int]]:
        return self.bgp_errors

class AclErrorDetector:
    """
    A class to detect ACL errors such as redundant ACLs, ACLs with only deny rules,
    and ACLs with incorrect rule ordering (deny before permit).
    """
    def __init__(self, acl_outputs: Dict[str, str]):
        self.acl_outputs = acl_outputs
        self.acl_errors = {}
        self.redundant_acls = []
        self.only_deny_acls = []
        self.sequence_errors = []

    def parse_acl_output(self, output: str) -> Dict[str, Dict[str, Any]]:
        """Parses the ACL output and structures it into a dictionary."""
        acl_data = {}
        for host_port, acl_output in self.acl_outputs.items():
            acl_data[host_port] = {}
            current_acl = None
            lines = acl_output.splitlines()
            for line in lines:
                line = line.strip()
                if line.startswith("Basic ACL") and not line.startswith("Basic Name ACL"):
                    # Example: Basic ACL 2003, 1 rule
                    parts = line.split(',')
                    acl_name_part = parts[0].strip()
                    try:
                        acl_id = acl_name_part.split()[2]
                    except IndexError:
                        logging.warning(f"Unable to parse ACL ID from line: '{line}'")
                        continue
                    rule_count_part = parts[1].strip()
                    try:
                        rule_count = int(rule_count_part.split()[0])
                    except (IndexError, ValueError):
                        rule_count = 0
                    acl_data[host_port][acl_id] = {
                        "rules": [],
                        "steps": 0
                    }
                    current_acl = acl_id
                elif line.startswith("Basic Name ACL"):
                    # Example: Basic Name ACL ACL_BASIC_TEST 2000, 2 rules
                    parts = line.split(',')
                    acl_name_part = parts[0].strip()
                    try:
                        acl_id = acl_name_part.split()[3]
                    except IndexError:
                        logging.warning(f"Unable to parse ACL ID from line: '{line}'")
                        continue
                    rule_count_part = parts[1].strip()
                    try:
                        rule_count = int(rule_count_part.split()[0])
                    except (IndexError, ValueError):
                        rule_count = 0
                    acl_data[host_port][acl_id] = {
                        "rules": [],
                        "steps": 0
                    }
                    current_acl = acl_id
                elif line.startswith("ACL's step is"):
                    # Example: ACL's step is 5
                    if current_acl:
                        try:
                            step = int(line.split()[3])
                            acl_data[host_port][current_acl]["steps"] = step
                        except (IndexError, ValueError):
                            logging.warning(f"Unable to parse ACL step from line: '{line}'")
                elif line.startswith("rule"):
                    # Example: rule 10 deny source 10.0.45.0 0.0.0.255 (0 times matched)
                    if current_acl:
                        parts = line.split(' ', 2)
                        if len(parts) >= 3:
                            rule_number = parts[1]
                            rule_details = parts[2]
                            acl_data[host_port][current_acl]["rules"].append(rule_details)
                        else:
                            logging.warning(f"Unable to parse rule details from line: '{line}'")
                elif "Basic ACL" in line and not line.startswith("Basic ACL"):
                    # Fallback for any other ACL lines
                    parts = line.split(',')
                    acl_name_part = parts[0].strip()
                    try:
                        acl_id = acl_name_part.split()[2]
                    except IndexError:
                        logging.warning(f"Unable to parse ACL ID from line: '{line}'")
                        continue
                    acl_data[host_port][acl_id] = {
                        "rules": [],
                        "steps": 0
                    }
                    current_acl = acl_id
            logging.info(f"Parsed ACL data for {host_port}: {acl_data[host_port]}")
        return acl_data

    def detect_errors(self, acl_data: Dict[str, Dict[str, Any]]):
        """Detects redundant ACLs, ACLs with only deny rules, and rule sequence errors."""
        for host_port, acls in acl_data.items():
            self.acl_errors[host_port] = {
                "redundant_rules_detection": [],
                "configuration_integrity_detection": [],
                "configuration_sequence_detection": []
            }
            acl_rules_map = {}
            for acl_id, data in acls.items():
                rules_tuple = tuple(data["rules"])
                if rules_tuple in acl_rules_map:
                    redundant_acl = {
                        "acl1": acl_rules_map[rules_tuple],
                        "acl2": acl_id
                    }
                    self.acl_errors[host_port]["redundant_rules_detection"].append(redundant_acl)
                else:
                    acl_rules_map[rules_tuple] = acl_id

            # Detect ACLs with only deny rules
            for acl_id, data in acls.items():
                has_permit = any("permit" in rule.lower() for rule in data["rules"])
                if not has_permit:
                    self.acl_errors[host_port]["configuration_integrity_detection"].append(acl_id)

            # Detect rule sequence errors (deny should precede permit)
            for acl_id, data in acls.items():
                permit_found = False
                for rule in data["rules"]:
                    if "permit" in rule.lower():
                        permit_found = True
                    elif "deny" in rule.lower() and permit_found:
                        error = {
                            "acl_id": acl_id,
                            "issue": "deny rule found after permit rule"
                        }
                        self.acl_errors[host_port]["configuration_sequence_detection"].append(error)
                        break  # No need to check further in this ACL

    def get_errors(self) -> Dict[str, Any]:
        return self.acl_errors

class FaultImpactAnalyzer:
    def __init__(self, sysname_map: Dict[str, str], ospf_faults: List[tuple], isis_faults: List[tuple],
                 bgp_faults: Dict[str, Dict[str, int]], acl_errors: Dict[str, Any]):
        """
        Initialize with faults and a mapping from host_port to sysname.
        """
        self.sysname_map = sysname_map  # host_port -> sysname
        self.faults = {
            "OSPF": ospf_faults,  # List of tuples (host_port, fault)
            "ISIS": isis_faults,  # List of tuples (host_port, fault)
            "BGP": bgp_faults,    # Dict of host_port -> {error_type: count}
            "ACL": acl_errors     # Dict of host_port -> {...}
        }
        self.fault_function_map = self.load_fault_function_map()
        self.impacts = {}  # sysname -> List of impacts

    def load_fault_function_map(self) -> Dict[str, List[str]]:
        # 定义故障与网络功能影响的映射关系
        return {
            "OSPF_NEIGHBOR_DOWN": ["路由收敛延迟", "路径不可用"],
            "OSPF_INTERFACE_DOWN": ["特定网络段不可达"],
            "OSPF_INTERFACE_NOT_FULL": ["路由不稳定"],
            "ISIS_NO_PEERS": ["路由失效", "流量中断"],
            "ISIS_L1_LSP_OVERFLOW": ["网络性能下降", "路由不稳定"],
            "ISIS_L2_LSP_OVERFLOW": ["网络性能下降", "路由不稳定"],
            "ISIS_LEVEL1_AVOID_REDIS_LOOP": ["路由收敛问题"],
            "ISIS_LEVEL2_AVOID_REDIS_LOOP": ["路由收敛问题"],
            "ISIS_IPV4_STATE_ISSUE": ["IPV4路由不可达"],
            "BGP_AS_PATH_LOOP": ["外部路由不可达", "路由泄漏风险"],
            "BGP_ROUTES_OVER_LIMIT": ["路由表膨胀", "性能下降"],
            "BGP_ORIGINATOR_ID_LOOP": ["外部路由不可达"],
            "ACL_REDUNDANT": ["ACL配置冗余，可能导致配置复杂化"],
            "ACL_ONLY_DENY": ["ACL仅包含deny规则，可能阻断合法流量"],
            "ACL_SEQUENCE_ERROR": ["ACL规则顺序错误，可能导致不必要的流量被拒绝或允许"]
            # 更多故障类型及其影响
        }

    def analyze_impacts(self):
        """
        Analyze faults and map them to network impacts per sysname.
        """
        # Initialize impacts dictionary
        for host_port in self.sysname_map:
            sysname = self.sysname_map[host_port]
            self.impacts[sysname] = []

        # Analyze OSPF faults
        for host_port, fault in self.faults["OSPF"]:
            sysname = self.sysname_map.get(host_port, host_port)
            if "邻居问题" in fault or "OSPF邻居Down" in fault:
                impacts = self.fault_function_map.get("OSPF_NEIGHBOR_DOWN", [])
                self.impacts[sysname].extend(impacts)
            elif "OSPF接口异常" in fault or "OSPF接口Down" in fault:
                impacts = self.fault_function_map.get("OSPF_INTERFACE_DOWN", [])
                self.impacts[sysname].extend(impacts)
            elif "OSPF接口状态非Full" in fault:
                impacts = self.fault_function_map.get("OSPF_INTERFACE_NOT_FULL", [])
                self.impacts[sysname].extend(impacts)
            # 其他OSPF故障类型...

        # Analyze ISIS faults
        for host_port, fault in self.faults["ISIS"]:
            sysname = self.sysname_map.get(host_port, host_port)
            if "无ISIS邻居" in fault or "ISIS邻居问题" in fault:
                impacts = self.fault_function_map.get("ISIS_NO_PEERS", [])
                self.impacts[sysname].extend(impacts)
            if "ISIS L1 LSP溢出" in fault:
                impacts = self.fault_function_map.get("ISIS_L1_LSP_OVERFLOW", [])
                self.impacts[sysname].extend(impacts)
            if "ISIS L2 LSP溢出" in fault:
                impacts = self.fault_function_map.get("ISIS_L2_LSP_OVERFLOW", [])
                self.impacts[sysname].extend(impacts)
            if "ISIS Level-1 避免重分发循环能力启用" in fault:
                impacts = self.fault_function_map.get("ISIS_LEVEL1_AVOID_REDIS_LOOP", [])
                self.impacts[sysname].extend(impacts)
            if "ISIS Level-2 避免重分发循环能力启用" in fault:
                impacts = self.fault_function_map.get("ISIS_LEVEL2_AVOID_REDIS_LOOP", [])
                self.impacts[sysname].extend(impacts)
            if "ISIS IPV4 State 异常" in fault:
                impacts = self.fault_function_map.get("ISIS_IPV4_STATE_ISSUE", [])
                self.impacts[sysname].extend(impacts)
            # 其他ISIS故障类型...

        # Analyze BGP faults
        for host_port, errors in self.faults["BGP"].items():
            sysname = self.sysname_map.get(host_port, host_port)
            for error_type, count in errors.items():
                if count > 0:
                    impacts = self.fault_function_map.get(error_type, [])
                    self.impacts[sysname].extend(impacts)

        # Analyze ACL faults
        acl_errors = self.faults["ACL"]
        for host_port, errors in acl_errors.items():
            sysname = self.sysname_map.get(host_port, host_port)
            # 冗余规则
            for redundant in errors.get("redundant_rules_detection", []):
                impacts = self.fault_function_map.get("ACL_REDUNDANT", ["ACL配置冗余"])
                self.impacts[sysname].extend(impacts)
            # 仅包含deny规则
            for acl in errors.get("configuration_integrity_detection", []):
                impacts = self.fault_function_map.get("ACL_ONLY_DENY", ["ACL仅包含deny规则"])
                self.impacts[sysname].extend(impacts)
            # 规则顺序错误
            for error in errors.get("configuration_sequence_detection", []):
                impacts = self.fault_function_map.get("ACL_SEQUENCE_ERROR", ["ACL规则顺序错误"])
                self.impacts[sysname].extend(impacts)

        # Remove duplicates
        for sysname in self.impacts:
            self.impacts[sysname] = list(set(self.impacts[sysname]))

    def get_impacts(self) -> Dict[str, List[str]]:
        return self.impacts

class OptimizationSuggester:
    def __init__(self):
        self.suggestion_map = self.load_suggestion_map()
        self.suggestions = {}  # sysname -> List of suggestions

    def load_suggestion_map(self) -> Dict[str, List[str]]:
        # 定义故障与优化建议的映射关系
        return {
            "路由收敛延迟": ["优化OSPF路由器的Hello和Dead间隔以加快收敛速度。"],
            "路径不可用": ["检查并恢复OSPF邻居关系，确保所有必要的接口处于UP状态。"],
            "特定网络段不可达": ["确认OSPF接口配置，确保接口处于正确的状态并正确加入OSPF区域。"],
            "路由不稳定": ["检查OSPF链路质量，调整OSPF成本以优化路由选择。"],
            "路由失效": ["恢复ISIS邻居关系，确保所有接口正确配置并处于UP状态。"],
            "流量中断": ["检查ISIS链路状态，确保网络连接的冗余性以防止流量中断。"],
            "网络性能下降": ["评估并优化ISIS LSP生成频率，确保网络不会因LSP溢出而影响性能。"],
            "路由收敛问题": ["调整ISIS的重分发配置，避免潜在的路由环路问题。"],
            "IPV4路由不可达": ["检查ISIS的IPV4配置，确保IP路由的正确性和接口状态。"],
            "外部路由不可达": ["审查BGP邻居关系，确保BGP会话正常建立并正确传播路由。"],
            "路由泄漏风险": ["实施BGP路由策略，防止不必要的路由泄漏，确保AS路径的正确性。"],
            "路由表膨胀": ["优化BGP过滤策略，限制不必要的路由广告，减少路由表大小。"],
            "性能下降": ["评估BGP路由策略，优化路由处理以提升设备性能。"],
            "ACL配置冗余": ["简化ACL配置，合并重复的ACL规则，减少配置复杂性。"],
            "ACL仅包含deny规则": ["在ACL中添加必要的permit规则，确保合法流量不会被阻断。"],
            "ACL规则顺序错误": ["重新排列ACL规则，确保deny规则位于permit规则之前，以正确处理流量。"]
            # 更多故障类型及其优化建议
        }

    def analyze_suggestions(self, impacts: Dict[str, List[str]]):
        """
        Analyze impacts and map them to optimization suggestions per sysname.
        """
        for sysname, impact_list in impacts.items():
            self.suggestions[sysname] = []
            for impact in impact_list:
                suggestions = self.suggestion_map.get(impact, ["请进一步分析以确定优化措施。"])
                self.suggestions[sysname].extend(suggestions)
            # Remove duplicates
            self.suggestions[sysname] = list(set(self.suggestions[sysname]))

    def get_suggestions(self) -> Dict[str, List[str]]:
        return self.suggestions

class ReportGenerator:
    def __init__(self, sysname_map: Dict[str, str],
                 ospf_faults: List[tuple],
                 isis_faults: List[tuple],
                 bgp_faults: Dict[str, Dict[str, int]],
                 acl_errors: Dict[str, Any],
                 network_impacts: Dict[str, List[str]],
                 optimization_suggestions: Dict[str, List[str]],
                 topology_errors: List[str],
                 topology_image_path: Optional[str] = None):
        self.sysname_map = sysname_map  # host_port -> sysname
        self.ospf_faults = ospf_faults
        self.isis_faults = isis_faults
        self.bgp_faults = bgp_faults
        self.acl_errors = acl_errors
        self.network_impacts = network_impacts
        self.optimization_suggestions = optimization_suggestions
        self.topology_errors = topology_errors
        self.topology_image_path = topology_image_path

    def generate_report(self, report_path: str):
        # Organize faults per sysname
        sysname_faults = {sysname: [] for sysname in self.sysname_map.values()}
        for protocol, faults in [("OSPF", self.ospf_faults), ("ISIS", self.isis_faults)]:
            for host_port, fault in faults:
                sysname = self.sysname_map.get(host_port, host_port)
                sysname_faults[sysname].append(f"{protocol}: {fault}")

        # BGP faults
        for host_port, errors in self.bgp_faults.items():
            sysname = self.sysname_map.get(host_port, host_port)
            for error_type, count in errors.items():
                if count > 0:
                    error_desc = BgpErrorDetector({}).error_translation_map.get(error_type, error_type)
                    fault = f"BGP: {error_desc} ({count} 次)"
                    sysname_faults[sysname].append(fault)

        # ACL faults
        for host_port, errors in self.acl_errors.items():
            sysname = self.sysname_map.get(host_port, host_port)
            # 冗余规则
            for redundant in errors.get("redundant_rules_detection", []):
                fault = f"ACL: 发现冗余ACL - ACL{redundant.get('acl1')} 与 ACL{redundant.get('acl2')}"
                sysname_faults[sysname].append(fault)
            # 仅包含deny规则
            for acl in errors.get("configuration_integrity_detection", []):
                fault = f"ACL: 发现仅包含deny规则的ACL - ACL{acl}"
                sysname_faults[sysname].append(fault)
            # 规则顺序错误
            for error in errors.get("configuration_sequence_detection", []):
                fault = f"ACL: ACL{error.get('acl_id')} 存在规则顺序错误 - {error.get('issue')}"
                sysname_faults[sysname].append(fault)

        try:
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write("="*50 + "\n")
                f.write("网络设备故障检测综合报告\n")
                f.write("="*50 + "\n")
                execution_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"报告时间：{execution_time}\n\n")

                # 拓扑验证结果
                f.write("===== 网络拓扑验证结果 =====\n")
                if self.topology_errors:
                    f.write("**拓扑错误:**\n")
                    for error in self.topology_errors:
                        f.write(f"- {error}\n")
                else:
                    f.write("未检测到拓扑错误。\n")
                f.write("\n")

                # 如果有拓扑图图片路径，则在报告中引用
                if self.topology_image_path and os.path.exists(self.topology_image_path):
                    f.write("**网络拓扑图:**\n")
                    f.write(f"![拓扑图]({self.topology_image_path})\n\n")

                for sysname in sysname_faults:
                    f.write(f"===== 节点: {sysname} =====\n")

                    # 故障列表
                    f.write("**故障列表:**\n")
                    if sysname_faults[sysname]:
                        for fault in sysname_faults[sysname]:
                            f.write(f"- {fault}\n")
                    else:
                        f.write("未检测到故障。\n")
                    f.write("\n")

                    # 影响的网络功能
                    f.write("**影响的网络功能:**\n")
                    impacts = self.network_impacts.get(sysname, [])
                    if impacts:
                        for impact in impacts:
                            f.write(f"- {impact}\n")
                    else:
                        f.write("未检测到对网络功能的影响。\n")
                    f.write("\n")

                    # 优化建议
                    f.write("**优化建议:**\n")
                    suggestions = self.optimization_suggestions.get(sysname, [])
                    if suggestions:
                        for suggestion in suggestions:
                            f.write(f"- {suggestion}\n")
                    else:
                        f.write("暂无优化建议。\n")
                    f.write("\n")

                f.write("="*50 + "\n")
            logging.info(f"综合报告已成功写入到 {report_path}")
        except IOError as e:
            logging.error(f"写入报告文件时出错: {e}")
            sys.exit(1)

def find_latest_folder(base_path: str) -> str:
    try:
        all_folders = [f for f in os.listdir(base_path) if f.isdigit()]
        if not all_folders:
            raise ValueError("在基础路径中未找到编号文件夹。")
        latest_folder = max(all_folders, key=int)
        logging.info(f"最新文件夹已识别: {latest_folder}")
        return latest_folder
    except FileNotFoundError:
        logging.error(f"基础路径未找到: {base_path}")
        sys.exit(1)
    except ValueError as ve:
        logging.error(ve)
        sys.exit(1)

def load_telnet_info(input_path: str) -> Dict[str, Any]:
    try:
        with open(input_path, 'r', encoding='utf-8') as f:
            telnet_info = json.load(f)
        logging.info(f"成功从 {input_path} 加载 telnet_info")
        return telnet_info
    except FileNotFoundError:
        logging.error(f"param.json 文件未在路径中找到: {input_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logging.error(f"从 param.json 解码 JSON 时出错: {e}")
        sys.exit(1)

class Graph:
    """简单的无向图实现，使用邻接表表示。"""
    def __init__(self):
        self.adj = {}  # sysname -> list of (neighbor_sysname, protocol)

    def add_node(self, node: str):
        if node not in self.adj:
            self.adj[node] = []
            logging.debug(f"Graph: Added node {node}")

    def add_edge(self, node1: str, node2: str, protocol: str):
        self.add_node(node1)
        self.add_node(node2)
        # To avoid duplicate edges with same protocol
        if not any(neighbor == node2 and proto == protocol for neighbor, proto in self.adj[node1]):
            self.adj[node1].append((node2, protocol))
            self.adj[node2].append((node1, protocol))
            logging.debug(f"Graph: Added edge {node1} <--> {node2} with protocol {protocol}")

    def get_nodes(self) -> List[str]:
        return list(self.adj.keys())

    def get_neighbors(self, node: str) -> List[tuple]:
        return self.adj.get(node, [])

class TopologyBuilder:
    def __init__(self, sysname_map: Dict[str, str], neighbor_info: Dict[str, Dict[str, List[str]]]):
        """
        sysname_map: host_port -> sysname
        neighbor_info: host_port -> neighbors dict
        """
        self.sysname_map = sysname_map
        self.neighbor_info = neighbor_info
        self.graph = Graph()

    def build_graph(self):
        """构建网络拓扑图。"""
        # 添加节点
        for host_port, sysname in self.sysname_map.items():
            self.graph.add_node(sysname)
            logging.info(f"添加节点: {sysname} ({host_port})")

        # 添加边
        for host_port, neighbors in self.neighbor_info.items():
            sysname = self.sysname_map.get(host_port, host_port)
            for protocol, neighbor_list in neighbors.items():
                for neighbor in neighbor_list:
                    neighbor_sysname = self.resolve_neighbor_sysname(neighbor)
                    if neighbor_sysname:
                        self.graph.add_edge(sysname, neighbor_sysname, protocol)
                        logging.info(f"添加边: {sysname} <--> {neighbor_sysname} (协议: {protocol})")
                    else:
                        logging.warning(f"无法解析邻居 '{neighbor}' 的sysname。")

    def resolve_neighbor_sysname(self, neighbor: str) -> Optional[str]:
        """将邻居标识转换为sysname。如果无法转换，返回None。"""
        for host_port, sysname in self.sysname_map.items():
            if neighbor in host_port or neighbor == sysname:
                return sysname
        return None

    def visualize_topology(self, output_path: str):
        """可视化拓扑图并保存为简单的文本文件。"""
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write("网络拓扑图\n")
                f.write("===========\n")
                for node in self.graph.get_nodes():
                    neighbors = self.graph.get_neighbors(node)
                    neighbor_str = ', '.join([f"{neighbor}({protocol})" for neighbor, protocol in neighbors])
                    f.write(f"{node}: {neighbor_str}\n")
            logging.info(f"拓扑图已保存到 {output_path}")
        except IOError as e:
            logging.error(f"写入拓扑图文件时出错: {e}")

class TopologyValidator:
    def __init__(self, graph: Graph):
        self.graph = graph
        self.errors = []

    def detect_cycles_util(self, node: str, visited: Dict[str, bool], parent: Optional[str]) -> bool:
        """辅助函数用于检测环路。"""
        visited[node] = True
        for neighbor, _ in self.graph.get_neighbors(node):
            if not visited.get(neighbor, False):
                if self.detect_cycles_util(neighbor, visited, node):
                    return True
            elif neighbor != parent:
                return True
        return False

    def detect_cycles(self):
        """检测拓扑中的环路。"""
        visited = {}
        for node in self.graph.get_nodes():
            if not visited.get(node, False):
                if self.detect_cycles_util(node, visited, None):
                    self.errors.append("检测到网络拓扑中的环路。")
                    logging.warning("网络拓扑中存在环路。")
                    return  # 一旦检测到环路，立即返回
        logging.info("网络拓扑中不存在环路。")

    def detect_isolated_nodes(self):
        """检测孤立节点。"""
        isolated = []
        for node in self.graph.get_nodes():
            if not self.graph.get_neighbors(node):
                isolated.append(node)
        if isolated:
            self.errors.append(f"检测到孤立节点: {isolated}")
            logging.warning(f"孤立节点: {isolated}")
        else:
            logging.info("没有孤立节点。")

    def verify_protocol_consistency(self):
        """验证连接双方使用的协议是否一致。"""
        checked = set()
        for node in self.graph.get_nodes():
            for neighbor, protocol in self.graph.get_neighbors(node):
                # To avoid duplicate checks, ensure node < neighbor
                if (node, neighbor) in checked or (neighbor, node) in checked:
                    continue
                protocols_node_to_neighbor = [proto for nbr, proto in self.graph.get_neighbors(node) if nbr == neighbor]
                protocols_neighbor_to_node = [proto for nbr, proto in self.graph.get_neighbors(neighbor) if nbr == node]
                if set(protocols_node_to_neighbor) != set(protocols_neighbor_to_node):
                    self.errors.append(f"连接 {node} <--> {neighbor} 的协议不一致。")
                    logging.warning(f"连接 {node} <--> {neighbor} 的协议不一致。")
                checked.add((node, neighbor))

    def validate(self):
        """执行所有验证步骤。"""
        self.detect_cycles()
        self.detect_isolated_nodes()
        self.verify_protocol_consistency()

    def get_errors(self) -> List[str]:
        return self.errors

class RouterManager:
    def __init__(self, telnet_info: Dict[str, Any], max_workers: int = 20):
        self.telnet_info = telnet_info
        self.sysnames_manager = RouterTelnetManager(telnet_info)
        self.ospf_outputs: Dict[str, Dict[str, str]] = {}
        self.isis_outputs: Dict[str, Dict[str, str]] = {}
        self.bgp_outputs: Dict[str, str] = {}
        self.acl_outputs: Dict[str, str] = {}
        self.max_workers = max_workers
        self.telnet_lock = Lock()
        # Define command sequences for different device types
        self.commands_map = {
            "huaweine40": {
                "initial": ['scr 0 t', 'display ip routing-table'],
                "ospf": ['display ospf peer', 'display ospf interface', 'display ospf brief'],
                "isis": ['display isis peer', 'display isis brief', 'display isis interface'],
                "bgp": ['display bgp error discard'],
                "acl": ['display acl all']
            }
            # Add other device types and their commands here if needed
        }

    def execute_telnet_commands(self, tn: telnetlib.Telnet, commands: List[str], host_port: str) -> Dict[str, str]:
        """执行Telnet命令并获取输出。"""
        output = {}
        try:
            tn.write(b'\n')
            time.sleep(1)
            initial_output = tn.read_very_eager().decode('ascii', errors='ignore')
            logging.debug(f"[{host_port}] Initial Telnet output:\n{initial_output}")

            for cmd in commands:
                tn.write(cmd.encode('ascii') + b'\n')
                logging.info(f"[{host_port}] 发送命令: {cmd}")
                time.sleep(1)
                cmd_output = tn.read_very_eager().decode('ascii', errors='ignore')
                output[cmd] = cmd_output
                logging.debug(f"[{host_port}] 命令 '{cmd}' 的输出:\n{cmd_output}")

            return output
        except Exception as e:
            logging.error(f"[{host_port}] Telnet Error while executing commands: {e}")
            return {}

    def get_prompt(self, tn: telnetlib.Telnet) -> Optional[str]:
        try:
            time.sleep(1)
            output = tn.read_very_eager().decode('ascii', errors='ignore')
            lines = output.splitlines()
            prompt = lines[-1].strip() if lines else None
            logging.debug(f"[{tn.host}:{tn.port}] Detected prompt: {prompt}")
            return prompt
        except Exception as e:
            logging.error(f"[{tn.host}:{tn.port}] Error getting prompt: {e}")
            return None

    def parse_neighbor_info(self) -> Dict[str, Dict[str, List[str]]]:
        """
        从已收集的 OSPF、ISIS 和 BGP 输出中解析邻居信息。
        返回格式：
        {
            "host:port": {
                "OSPF": ["neighbor1", "neighbor2"],
                "ISIS": ["neighbor3"],
                "BGP": ["neighbor4"]
            },
            ...
        }
        """
        neighbor_info = {}

        for host_port, ospf_info in self.ospf_outputs.items():
            ospf_peers = []
            ospf_peer_output = ospf_info.get('display ospf peer', '')
            for line in ospf_peer_output.splitlines():
                match = re.search(r'Neighbor\s+(\S+)', line)
                if match:
                    neighbor = match.group(1)
                    ospf_peers.append(neighbor)

            isis_peers = []
            isis_info = self.isis_outputs.get(host_port, {})
            isis_peer_output = isis_info.get('display isis peer', '')
            for line in isis_peer_output.splitlines():
                match = re.search(r'Peer\s+(\S+)', line)
                if match:
                    neighbor = match.group(1)
                    isis_peers.append(neighbor)

            bgp_peers = []
            bgp_output = self.bgp_outputs.get(host_port, '')
            for line in bgp_output.splitlines():
                match = re.search(r'BGP Neighbor:\s+(\S+)', line)
                if match:
                    neighbor = match.group(1)
                    bgp_peers.append(neighbor)

            neighbor_info[host_port] = {
                "OSPF": ospf_peers,
                "ISIS": isis_peers,
                "BGP": bgp_peers
            }

            logging.info(f"[{host_port}] 解析到的邻居信息: {neighbor_info[host_port]}")

        return neighbor_info

    def get_configuration_via_telnet(self, tn: telnetlib.Telnet, image_type: str, host_port: str):
        """根据设备的image_type执行命令并收集数据。"""
        # Find matching device type based on partial image_type
        matched_key = next((key for key in self.commands_map if key in image_type), None)
        if not matched_key:
            logging.warning(f"[{host_port}] Unsupported image_type '{image_type}'. Skipping.")
            return

        commands = self.commands_map[matched_key]["initial"]

        # Execute initial commands to determine protocols
        initial_outputs = self.execute_telnet_commands(tn, commands, host_port)
        routing_table_output = initial_outputs.get('display ip routing-table', '')
        protocols = self.parse_routing_table(routing_table_output)

        # Based on protocols, decide which commands to execute
        if protocols["OSPF"]:
            ospf_commands = self.commands_map[matched_key].get("ospf", [])
            ospf_outputs = self.execute_telnet_commands(tn, ospf_commands, host_port)
            self.ospf_outputs[host_port] = {
                'display ospf peer': ospf_outputs.get('display ospf peer', ''),
                'display ospf interface': ospf_outputs.get('display ospf interface', ''),
                'display ospf brief': ospf_outputs.get('display ospf brief', '')
            }
        else:
            logging.info(f"[{host_port}] OSPF 未启用，跳过OSPF相关命令。")

        if any(protocol.startswith("ISIS") for protocol in protocols if protocols[protocol]):
            isis_commands = self.commands_map[matched_key].get("isis", [])
            isis_outputs = self.execute_telnet_commands(tn, isis_commands, host_port)
            self.isis_outputs[host_port] = {
                'display isis peer': isis_outputs.get('display isis peer', ''),
                'display isis brief': isis_outputs.get('display isis brief', ''),
                'display isis interface': isis_outputs.get('display isis interface', '')
            }
        else:
            logging.info(f"[{host_port}] ISIS 未启用，跳过ISIS相关命令。")

        # Execute BGP and ACL commands regardless of routing protocols
        bgp_commands = self.commands_map[matched_key].get("bgp", [])
        if bgp_commands:
            bgp_outputs = self.execute_telnet_commands(tn, bgp_commands, host_port)
            self.bgp_outputs[host_port] = bgp_outputs.get('display bgp error discard', '')

        acl_commands = self.commands_map[matched_key].get("acl", [])
        if acl_commands:
            acl_outputs = self.execute_telnet_commands(tn, acl_commands, host_port)
            self.acl_outputs[host_port] = acl_outputs.get('display acl all', '')

        # Get sysname
        self.sysnames_manager.connect_and_get_sysnames(tn)

    def parse_routing_table(self, routing_table_output: str) -> Dict[str, bool]:
        """解析路由表输出，确定启用了哪些路由协议"""
        protocols = {
            "OSPF": False,
            "ISIS-L1": False,
            "ISIS-L2": False,
            "ISIS-L1-L2": False
        }
        for line in routing_table_output.splitlines():
            line = line.strip()
            if "OSPF" in line:
                protocols["OSPF"] = True
            if "ISIS-L1" in line:
                protocols["ISIS-L1"] = True
            if "ISIS-L2" in line:
                protocols["ISIS-L2"] = True
            if "ISIS-L1-L2" in line:
                protocols["ISIS-L1-L2"] = True
        logging.info(f"解析路由表结果: {protocols}")
        return protocols

    def connect_and_collect(self):
        """Connects to all routers and collects necessary data."""
        nodes = self.telnet_info.get("node", [])
        if not nodes:
            logging.warning("No nodes found in telnet_info.")
            return

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_node = {}
            for node in nodes:
                image_type = node.get("image_type", "").lower()
                if not any(k in image_type for k in self.commands_map.keys()):
                    logging.warning(f"Unsupported image_type '{image_type}' for node. Skipping.")
                    continue
                host, port = node.get("hostip"), node.get("port")
                if not host or not port:
                    logging.warning(f"Host IP or port missing for node with image_type '{image_type}'. Skipping.")
                    continue
                try:
                    tn = telnetlib.Telnet(host, port, timeout=10)
                    tn.host, tn.port = host, port
                    future = executor.submit(self.get_configuration_via_telnet, tn, image_type, f"{host}:{port}")
                    future_to_node[future] = f"{host}:{port}"
                except Exception as e:
                    logging.error(f"Failed to connect to {host}:{port} via Telnet: {e}")

            for future in as_completed(future_to_node):
                host_port = future_to_node[future]
                try:
                    future.result()
                    logging.info(f"[{host_port}] Configuration retrieval successful.")
                except Exception as e:
                    logging.error(f"[{host_port}] Error processing node: {e}")
                    logging.info(f"[{host_port}] Configuration retrieval failed.")

def main(input_path: str, output_path: str):
    base_path = "/uploadPath/reasoning"
    if "{t}" in input_path or "{t}" in output_path:
        latest_folder = find_latest_folder(base_path)
        input_path = input_path.replace("{t}", latest_folder)
        output_path = output_path.replace("{t}", latest_folder)
        logging.debug(f"已解析 input_path: {input_path}")
        logging.debug(f"已解析 output_path: {output_path}")

    telnet_info = load_telnet_info(input_path)
    router_manager = RouterManager(telnet_info)
    router_manager.connect_and_collect()

    # 获取 sysname 映射
    sysname_map = router_manager.sysnames_manager.sysnames  # host_port -> sysname

    # OSPF Diagnostics
    ospf_diagnostic = OSPFDiagnostic(router_manager.ospf_outputs)
    ospf_diagnostic.collect_ospf_info()
    ospf_diagnostic.analyze_ospf_status()
    ospf_faults = ospf_diagnostic.get_faults()

    # ISIS Diagnostics
    isis_detector = IsisFaultDetector(router_manager.isis_outputs)
    for host_port, output in router_manager.isis_outputs.items():
        if output:  # 仅对有ISIS输出的设备进行检测
            try:
                tn = telnetlib.Telnet(host_port.split(':')[0], int(host_port.split(':')[1]), timeout=10)
                tn.host, tn.port = host_port.split(':')[0], int(host_port.split(':')[1])
                isis_detector.detect_faults(tn, host_port)
                tn.close()
            except Exception as e:
                logging.error(f"无法连接到 {host_port} 进行ISIS检测: {e}")
        else:
            logging.info(f"[{host_port}] 未启用ISIS，跳过ISIS故障检测。")
    isis_faults = isis_detector.get_faults()

    # BGP Diagnostics
    bgp_detector = BgpErrorDetector(router_manager.bgp_outputs)
    bgp_detector.detect_faults()
    bgp_faults = bgp_detector.get_faults()

    # ACL Diagnostics
    acl_detector = AclErrorDetector(router_manager.acl_outputs)
    acl_data = acl_detector.parse_acl_output(router_manager.acl_outputs)
    acl_detector.detect_errors(acl_data)
    acl_errors = acl_detector.get_errors()

    # Fault Impact Analysis
    fault_impact_analyzer = FaultImpactAnalyzer(sysname_map, ospf_faults, isis_faults, bgp_faults, acl_errors)
    fault_impact_analyzer.analyze_impacts()
    network_impacts = fault_impact_analyzer.get_impacts()

    # Optimization Suggestions
    optimization_suggester = OptimizationSuggester()
    optimization_suggester.analyze_suggestions(network_impacts)
    optimization_suggestions = optimization_suggester.get_suggestions()

    # 拓扑构建与验证
    neighbor_info = router_manager.parse_neighbor_info()
    logging.info(f"最终邻居信息: {json.dumps(neighbor_info, ensure_ascii=False, indent=2)}")  # 打印邻居信息

    topology_builder = TopologyBuilder(sysname_map, neighbor_info)
    topology_builder.build_graph()
    topology_image_path = os.path.join(os.path.dirname(output_path), 'topology.txt')  # 保存为文本文件
    topology_builder.visualize_topology(topology_image_path)

    topology_validator = TopologyValidator(topology_builder.graph)
    topology_validator.validate()
    topology_errors = topology_validator.get_errors()

    # 生成并写入报告
    report_generator = ReportGenerator(
        sysname_map,
        ospf_faults,
        isis_faults,
        bgp_faults,
        acl_errors,
        network_impacts,
        optimization_suggestions,
        topology_errors,
        topology_image_path
    )
    report_path = os.path.join(os.path.dirname(output_path), 'data.txt')
    report_generator.generate_report(report_path)
    
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="综合处理路由器配置并检测OSPF、ISIS、BGP和ACL故障。")
    parser.add_argument("-i", "--input", required=True, help="param.json 文件的路径，使用 {t} 代表最新文件夹编号。")
    parser.add_argument("-o", "--output", required=True, help="报告输出文件的路径，使用 {t} 代表最新文件夹编号。")
    args = parser.parse_args()
    main(args.input, args.output)
