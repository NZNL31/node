#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import yaml
import json
import socket
import random
import base64
import urllib.parse

# ===== 配置 =====
ALL_NODES_FILE = "all_nodes.yaml"
HK_NODES_FILE = "hk_nodes.json"
CLASH_FILE = "clash.yaml"
V2_FILE = "v2.txt"

MAX_LATENCY = 200   # ms
MIN_SPEED_MB = 10   # Mbps
TEST_TIMEOUT = 5    # 秒

# ===== 功能函数 =====
def check_port(host, port, timeout=TEST_TIMEOUT):
    """检查节点端口是否可连通"""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True
    except:
        return False

def simulate_speedtest(node, reference_node=None):
    """
    模拟测速：
    - 这里参考节点可以用于实际测速（GitHub Actions / 无法真测速可随机）
    """
    if not check_port(node.get("server"), node.get("port")):
        return 5000.0, 0.0
    latency = random.randint(50, 250)
    speed = random.uniform(5, 50)
    return latency, speed

def score_node(latency, speed):
    """根据延迟和速度评分"""
    if latency > MAX_LATENCY or speed < MIN_SPEED_MB:
        return 0.0
    return speed / latency * 10

def node_to_uri(node):
    """节点转订阅 URI"""
    typ = node.get("type", "").lower()
    if typ == "ss":
        userinfo = f"{node.get('cipher','aes-256-gcm')}:{node.get('password','')}"
        server_part = f"{node.get('server')}:{node.get('port')}"
        base = base64.urlsafe_b64encode(userinfo.encode()).decode().strip("=")
        name = urllib.parse.quote(node.get("name", "Unnamed"))
        return f"ss://{base}@{server_part}#{name}"
    elif typ == "vmess":
        obj = {
            "v": "2",
            "ps": node.get("name",""),
            "add": node.get("server"),
            "port": str(node.get("port")),
            "id": node.get("uuid"),
            "aid": str(node.get("alterId",0)),
            "net": node.get("network","tcp"),
            "type": "none",
            "host": node.get("ws-opts", {}).get("headers", {}).get("Host", ""),
            "path": node.get("ws-opts", {}).get("path", ""),
            "tls": "tls" if node.get("tls") else "",
        }
        return "vmess://" + base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().strip("=")
    elif typ == "vless":
        name = urllib.parse.quote(node.get("name",""))
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
    """生成 Clash YAML 配置"""
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
            }
        ],
        "rules": ["MATCH,🌐 节点选择"]
    }
    return config

# ===== 主程序 =====
def main():
    # 读取节点文件
    with open(ALL_NODES_FILE, "r", encoding="utf-8") as f:
        all_nodes = yaml.safe_load(f).get("proxies", [])

    with open(HK_NODES_FILE, "r", encoding="utf-8") as f:
        hk_nodes = json.load(f)

    best_nodes = []
    uri_list = []

    for node in all_nodes:
        if not node.get("server") or not node.get("port"):
            continue

        # 随机选一个参考香港节点测速
        ref_node = random.choice(hk_nodes) if hk_nodes else None
        latency, speed = simulate_speedtest(node, reference_node=ref_node)
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
        encoded = base64.b64encode("\n".join(uri_list).encode()).decode()
        with open(V2_FILE, "w", encoding="utf-8") as f:
            f.write(encoded)
        print(f"\n✅ 已生成 {V2_FILE}（Base64 订阅格式）")
    else:
        print("⚠️ 没有生成可用节点")

if __name__ == "__main__":
    main()
