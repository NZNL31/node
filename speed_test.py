import yaml
import json
import socket
import time
import random
import base64
import urllib.parse

# ===== 基本配置 =====
RAW_NODES_FILE = "all_nodes.yaml"  # 输入文件
CLASH_FILE = "clash.yaml"          # Clash 配置输出
V2_FILE = "v2.txt"                 # V2Ray / Shadowrocket 订阅输出

# 测试阈值
MAX_LATENCY = 200   # ms
MIN_SPEED_MB = 10   # Mbps
TEST_TIMEOUT = 5    # 秒


# ===== 功能函数 =====
def check_port(host, port, timeout=TEST_TIMEOUT):
    """检查端口连通性"""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True
    except Exception:
        return False


def simulate_speedtest(node):
    """模拟测速（随机生成）"""
    if not check_port(node["server"], node["port"]):
        return 5000.0, 0.0  # 不可达
    latency = random.randint(50, 250)
    speed = random.uniform(5, 50)
    return latency, speed


def score_node(latency, speed):
    """节点评分"""
    if latency > MAX_LATENCY or speed < MIN_SPEED_MB:
        return 0.0
    return speed / latency * 10


def node_to_uri(node):
    """将节点转换为订阅链接格式"""
    typ = node.get("type")

    # SS
    if typ == "ss":
        userinfo = f"{node.get('cipher','aes-256-gcm')}:{node.get('password','')}"
        server_part = f"{node.get('server')}:{node.get('port')}"
        base = base64.urlsafe_b64encode(userinfo.encode()).decode().strip("=")
        name = urllib.parse.quote(node.get("name", "Unnamed"))
        return f"ss://{base}@{server_part}#{name}"

    # VMess
    elif typ == "vmess":
        obj = {
            "v": "2",
            "ps": node.get("name", ""),
            "add": node.get("server"),
            "port": str(node.get("port")),
            "id": node.get("uuid"),
            "aid": str(node.get("alterId", 0)),
            "net": node.get("network", "tcp"),
            "type": "none",
            "host": node.get("ws-opts", {}).get("headers", {}).get("Host", ""),
            "path": node.get("ws-opts", {}).get("path", ""),
            "tls": "tls" if node.get("tls") else "",
        }
        return "vmess://" + base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().strip("=")

    # VLESS
    elif typ == "vless":
        name = urllib.parse.quote(node.get("name", ""))
        server = node.get("server")
        port = node.get("port")
        uuid = node.get("uuid")
        tls = "tls" if node.get("tls") else "none"
        path = node.get("ws-opts", {}).get("path", "")
        host = node.get("ws-opts", {}).get("headers", {}).get("Host", "")
        query = f"type=ws&security={tls}&path={urllib.parse.quote(path)}&host={host}"
        return f"vless://{uuid}@{server}:{port}?{query}#{name}"

    else:
        return None


def build_clash_config(best_nodes):
    """构建 Clash YAML 完整配置"""
    config = {
        "allow-lan": True,
        "external-controller": ":9090",
        "log-level": "info",
        "mode": "Rule",
        "port": 7890,
        "proxies": best_nodes,
        "proxy-groups": [
            {
                "name": "🌐 节点选择",
                "type": "select",
                "proxies": ["🔄 自动选择"] + [n["name"] for n in best_nodes],
            },
            {
                "name": "🔄 自动选择",
                "type": "url-test",
                "url": "https://www.gstatic.com/generate_204",
                "interval": 300,
                "proxies": [n["name"] for n in best_nodes],
            },
        ],
        "rules": [
            "MATCH,🌐 节点选择"
        ],
    }
    return config


# ===== 主程序 =====
def main():
    with open(RAW_NODES_FILE, "r", encoding="utf-8") as f:
        raw_nodes = yaml.safe_load(f)

    best_nodes = []
    uri_list = []

    for node in raw_nodes.get("proxies", []):
        if not node.get("server") or not node.get("port"):
            continue

        latency, speed = simulate_speedtest(node)
        score = score_node(latency, speed)

        print(f"[{node.get('name')}] 延迟 {latency} ms, 速度 {speed:.1f} Mbps, 分数 {score:.2f}")

        if score > 0:
            best_nodes.append(node)
            uri = node_to_uri(node)
            if uri:
                uri_list.append(uri)

    # 输出 Clash YAML
    clash_config = build_clash_config(best_nodes)
    with open(CLASH_FILE, "w", encoding="utf-8") as f:
        yaml.dump(clash_config, f, allow_unicode=True)

    # 输出 Base64 订阅
    if uri_list:
        joined = "\n".join(uri_list)
        encoded = base64.b64encode(joined.encode()).decode()
        with open(V2_FILE, "w", encoding="utf-8") as f:
            f.write(encoded)
        print(f"\n✅ 已生成 {V2_FILE} （Base64 订阅格式）")
    else:
        print("⚠️ 没有生成任何可用节点。")


if __name__ == "__main__":
    main()
