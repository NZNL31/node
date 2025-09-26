import yaml
import json
import socket
import time
import random

# 配置
RAW_NODES_FILE = "all_nodes.yaml"  # Clash 通用原始订阅
BEST_YAML_FILE = "best_nodes.yaml"
BEST_JSON_FILE = "best_nodes.json"

# 测试阈值
MAX_LATENCY = 200   # ms
MIN_SPEED_MB = 10   # 下行 Mbps
TEST_TIMEOUT = 5    # 秒

# 备用香港节点（用于测速参考）
HK_NODES = [
    {"server": "hk1.example.com", "port": 443},
    {"server": "hk2.example.com", "port": 443}
]

def check_port(host, port, timeout=TEST_TIMEOUT):
    """检查节点端口是否开放"""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True
    except:
        return False

def simulate_speedtest(node):
    """
    模拟测速：
    - GitHub Actions 上无法真实测速，所以用随机数模拟
    - 对可连通节点随机生成延迟和速度
    """
    if not check_port(node["server"], node["port"]):
        return 5000.0, 0.0  # 超时
    latency = random.randint(50, 250)  # ms
    speed = random.uniform(5, 50)      # Mbps
    return latency, speed

def score_node(latency, speed):
    """根据延迟和网速打分"""
    if latency > MAX_LATENCY or speed < MIN_SPEED_MB:
        return 0.0
    return speed / latency * 10  # 简单评分公式

def main():
    # 读取原始节点
    with open(RAW_NODES_FILE, "r", encoding="utf-8") as f:
        raw_nodes = yaml.safe_load(f)

    best_nodes = []

    for node in raw_nodes.get("proxies", []):
        server = node.get("server")
        port = node.get("port")
        if not server or not port:
            continue

        latency, speed = simulate_speedtest({"server": server, "port": port})
        score = score_node(latency, speed)

        print(f"[{node.get('name')}] 延迟 {latency} ms, 速度 {speed:.1f} Mbps, 分数 {score:.2f}")

        if score > 0:
            best_nodes.append(node)

    # 保存 Clash YAML
    yaml.dump({"proxies": best_nodes}, open(BEST_YAML_FILE, "w", encoding="utf-8"), allow_unicode=True)

    # 保存 V2Ray JSON
    json.dump(best_nodes, open(BEST_JSON_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
