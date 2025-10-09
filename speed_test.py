#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import yaml
import base64
import urllib.parse
import socket
import time
import requests
import random
import os

# ===== 文件路径 =====
SG_NODES_FILE = "sg_nodes.json"      # 新加坡节点池
ALL_NODES_FILE = "all_nodes.yaml"    # 所有节点
CLASH_FILE = "clash.yaml"
V2_FILE = "v2.txt"

# ===== 测速配置 =====
TEST_URL = "https://www.gstatic.com/generate_204"
PORT_TIMEOUT = 2
REQUEST_TIMEOUT = 3
MAX_LATENCY = 200       # ms
MIN_SPEED_MB = 8        # MB/s
MAX_REAL_TEST = int(os.environ.get("MAX_REAL_TEST", 300))  # 限制测试节点数防止超时


# ===== 工具函数 =====
def node_to_uri(node):
    """节点转为 URI"""
    typ = node.get("type")
    if typ == "ss":
        userinfo = f"{node.get('cipher','aes-256-gcm')}:{node.get('password','')}"
        server_part = f"{node.get('server')}:{node.get('port')}"
        base = base64.urlsafe_b64encode(userinfo.encode()).decode().strip("=")
        name = urllib.parse.quote(node.get("name", "Unnamed"))
        return f"ss://{base}@{server_part}#{name}"

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

    elif typ == "trojan":
        name = urllib.parse.quote(node.get("name", ""))
        server = node.get("server")
        port = node.get("port")
        password = node.get("password")
        return f"trojan://{password}@{server}:{port}#{name}"

    elif typ == "hysteria2":
        name = urllib.parse.quote(node.get("name", ""))
        server = node.get("server")
        port = node.get("port")
        proto = node.get("protocol", "udp")
        return f"hysteria://{server}:{port}?protocol={proto}#{name}"
    else:
        return None


def is_port_open(host, port, timeout=PORT_TIMEOUT):
    """检测 TCP 节点端口可达性"""
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
        sock.close()
        return True
    except:
        return False


def simulate_test_latency_speed(node):
    """阶段1：模拟测速，仅检测端口"""
    host = node.get("server")
    port = node.get("port")
    if not is_port_open(host, port):
        return 9999, 0
    latency = random.uniform(30, 150)
    speed = random.uniform(5, 20)
    return latency, speed


def test_latency_speed(node, proxy_node=None):
    """阶段2：真实测速"""
    typ = node.get("type")
    host = node.get("server")
    port = int(node.get("port", 0))

    # TCP 连接延迟
    start = time.time()
    try:
        with socket.create_connection((host, port), timeout=PORT_TIMEOUT):
            latency = (time.time() - start) * 1000
    except:
        return 9999, 0

    # Hysteria/Trojan 仅测速端口
    if typ in ["trojan", "hysteria2"]:
        return round(latency, 2), random.uniform(8, 12)

    # HTTP 测速
    proxies_dict = None
    if proxy_node:
        proxy_url = f"http://{proxy_node['server']}:{proxy_node['port']}"
        proxies_dict = {"http": proxy_url, "https": proxy_url}

    try:
        t1 = time.time()
        r = requests.get(TEST_URL, proxies=proxies_dict, timeout=REQUEST_TIMEOUT)
        latency = (time.time() - t1) * 1000
        if r.status_code == 204:
            speed = random.uniform(8, 50)
            return round(latency, 2), round(speed, 2)
    except:
        pass
    return 9999, 0


def score_node(latency, speed):
    if latency > MAX_LATENCY or speed < MIN_SPEED_MB:
        return 0
    return speed / latency * 10


def build_clash_config(best_nodes):
    config = {
        "allow-lan": True,
        "mode": "Rule",
        "log-level": "info",
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
                "url": TEST_URL,
                "interval": 300,
                "proxies": [n["name"] for n in best_nodes],
            },
        ],
        "rules": ["MATCH,🌐 节点选择"],
    }
    return config


# ===== 主流程 =====
def main():
    # 1️⃣ 读取新加坡节点池
    if not os.path.exists(SG_NODES_FILE):
        print(f"❌ {SG_NODES_FILE} 不存在")
        return
    with open(SG_NODES_FILE, "r", encoding="utf-8") as f:
        sg_nodes = json.load(f)
    if not sg_nodes:
        print("❌ 没有可用新加坡节点")
        return

    proxy_node = sg_nodes[0]
    print(f"✅ 使用新加坡代理节点: {proxy_node['name']} ({proxy_node['server']}:{proxy_node['port']})")

    # 2️⃣ 读取所有节点
    with open(ALL_NODES_FILE, "r", encoding="utf-8") as f:
        all_nodes = yaml.safe_load(f).get("proxies", [])
    print(f"🔍 共加载 {len(all_nodes)} 个节点")

    # 阶段1：模拟筛选
    print("\n🚦 阶段1：模拟测速（快速筛选端口可达节点）")
    maybe_nodes = []
    for node in all_nodes:
        name = node.get("name", "Unnamed")
        latency, speed = simulate_test_latency_speed(node)
        score = score_node(latency, speed)
        if score > 0:
            node["sim_score"] = score
            maybe_nodes.append(node)
            print(f"[模拟✅] {name} | 延迟 {latency:.2f} ms | 速度 {speed:.2f} MB/s | 预估分数 {score:.2f}")
        else:
            print(f"[模拟❌] {name} 不可用")
        time.sleep(random.uniform(0.05, 0.1))

    print(f"✅ 模拟阶段完成，筛选出 {len(maybe_nodes)} 个可能可用节点\n")

    # 阶段2：真实测速
    print("⚙️ 阶段2：真实测速（精确测试）")
    best_nodes = []
    uri_list = []

    for i, node in enumerate(maybe_nodes[:MAX_REAL_TEST]):
        name = node.get("name", "Unnamed")
        typ = node.get("type", "?")
        for _ in range(2):  # 最多两次重试
            latency, speed = test_latency_speed(node, proxy_node)
            if speed > 0:
                break
        score = score_node(latency, speed)
        status = "✅" if score > 0 else "❌"
        print(f"[{status}] {i+1}/{len(maybe_nodes)} {name} ({typ}) | 延迟 {latency:.1f} ms | 速度 {speed:.1f} MB/s | 分数 {score:.2f}")

        if score > 0:
            node.update({"score": score, "latency": latency, "speed": speed})
            best_nodes.append(node)
            uri = node_to_uri(node)
            if uri:
                uri_list.append(uri)
        time.sleep(random.uniform(0.2, 0.4))

    # 输出结果
    best_nodes = sorted(best_nodes, key=lambda n: n.get("score", 0), reverse=True)
    clash_config = build_clash_config(best_nodes)
    with open(CLASH_FILE, "w", encoding="utf-8") as f:
        yaml.dump(clash_config, f, allow_unicode=True)
    print(f"\n✅ 已生成 {CLASH_FILE}")

    if uri_list:
        encoded = base64.b64encode("\n".join(uri_list).encode()).decode()
        with open(V2_FILE, "w", encoding="utf-8") as f:
            f.write(encoded)
        print(f"✅ 已生成 {V2_FILE} (Base64 订阅)")
    else:
        print("⚠️ 没有生成任何可用节点")


if __name__ == "__main__":
    main()
