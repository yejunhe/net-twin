import docker
import socket
import paramiko
import telnetlib

class RemoteLogin:
    def __init__(self, host=None):
        self.host = host
        self.port = None
        self.container_id = self.get_docker_container_id()

    def get_docker_container_id(self):
        """
        自动检索本地运行的 Docker 容器 ID。
        """
        try:
            client = docker.from_env()
            containers = client.containers.list()
            if containers:
                return containers[0].id  # 返回第一个运行中的容器 ID
            return None
        except Exception as e:
            print(f"Error retrieving Docker container ID: {e}")
            return None

    def detect_port(self):
        """
        自动检测常用端口（22, 23）是否开放。
        """
        ports_to_check = [22, 23]
        for port in ports_to_check:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                result = s.connect_ex((self.host, port))
                if result == 0:
                    return port
        return None

    def determine_login_method(self):
        """
        自动判断登录方式。
        """
        # 先检测端口，优先考虑 SSH 或 Telnet
        self.port = self.detect_port()
        if self.port == 22:
            return "SSH"
        elif self.port == 23:
            return "Telnet"
        elif self.container_id:
            return "Docker"
        else:
            return "Unsupported login type"

    def extract_data(self, login_method):
        """
        根据登录方式提取相关数据。
        """
        if login_method == "SSH":
            return self.extract_data_via_ssh()
        elif login_method == "Telnet":
            return self.extract_data_via_telnet()
        elif login_method == "Docker":
            return self.extract_data_via_docker()
        else:
            return "Unsupported login type"

    def extract_data_via_ssh(self):
        """
        使用 SSH 提取数据。
        """
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(self.host, port=self.port, username="your_username", password="your_password")
            stdin, stdout, stderr = ssh.exec_command("ls -la")  # 替换为你要执行的命令
            output = stdout.read().decode()
            ssh.close()
            return output
        except Exception as e:
            return f"SSH data extraction failed: {e}"

    def extract_data_via_telnet(self):
        """
        使用 Telnet 提取数据。
        """
        try:
            telnet = telnetlib.Telnet(self.host, self.port)
            telnet.read_until(b"login: ")
            telnet.write(b"your_username\n")
            telnet.read_until(b"Password: ")
            telnet.write(b"your_password\n")
            telnet.write(b"ls -la\n")  # 替换为你要执行的命令
            output = telnet.read_all().decode('ascii')
            telnet.close()
            return output
        except Exception as e:
            return f"Telnet data extraction failed: {e}"

    def extract_data_via_docker(self):
        """
        使用 Docker 提取数据。
        """
        try:
            client = docker.from_env()
            container = client.containers.get(self.container_id)
            result = container.exec_run("ls -la")  # 替换为你要执行的命令
            output = result.output.decode()
            return output
        except Exception as e:
            return f"Docker data extraction failed: {e}"

# 模拟用户输入
host = input("Enter host IP: ").strip()

# 创建 RemoteLogin 对象并判断登录方式
login = RemoteLogin(host=host)
login_method = login.determine_login_method()

# 输出结果
print(f"Determined login method: {login_method}")
if login_method == "Docker":
    print(f"Detected Docker container ID: {login.container_id}")
elif login_method in ["SSH", "Telnet"]:
    print(f"Detected port: {login.port}")

# 根据登录方式提取数据
data = login.extract_data(login_method)
print(f"Extracted data:\n{data}")
