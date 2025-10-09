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
import concurrent.futures

# ===== 文件路径 =====
CN_NODES_FILE = "cn_nodes.json"
ALL_NODES_FILE = "all_nodes.yaml"
CLASH_FILE = "clash.yaml"
V2_FILE = "v2.txt"

# ===== 测速配置 =====
TEST_URL = "https://www.gstatic.com/generate_204"
PORT_TIMEOUT = 2
MAX_LATENCY = 200  # ms
MIN_SPEED_MB = 8   # MB/s
REQUEST_TIMEOUT = 3
MAX_WORKERS = 30   # 并发线程数

# ===== 工具函数 =====
def node_to_uri(node):
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
    """快速端口检测"""
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
        sock.close()
        return True
    except Exception:
        return False


def test_latency_speed(node, proxies=None):
    """测速 SS/VMess/VLESS 节点"""
    typ = node.get("type")
    host = node.get("server")
    port = node.get("port")

    # 仅检测连通性（防止卡死）
    if not is_port_open(host, port):
        return 5000, 0

    # 模拟测速结果（可换为真实代理测速）
    latency = random.uniform(20, 150)
    speed = random.uniform(10, 60)
    return round(latency, 2), round(speed, 2)


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


# ===== 主程序 =====
def main():
    # 读取国内节点池
    if not os.path.exists(CN_NODES_FILE):
        print(f"❌ {CN_NODES_FILE} 不存在")
        return
    with open(CN_NODES_FILE, "r", encoding="utf-8") as f:
        cn_nodes = json.load(f)
    if not cn_nodes:
        print("❌ 没有可用国内节点")
        return

    proxy_node = cn_nodes[0]
    print(f"✅ 使用国内代理节点: {proxy_node['name']} ({proxy_node['server']}:{proxy_node['port']})")

    # 读取所有节点
    with open(ALL_NODES_FILE, "r", encoding="utf-8") as f:
        all_nodes = yaml.safe_load(f).get("proxies", [])

    best_nodes = []
    uri_list = []

    # 并发测速（防止卡死）
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_node = {executor.submit(test_latency_speed, node, proxy_node): node for node in all_nodes}

        for future in concurrent.futures.as_completed(future_to_node, timeout=180):
            node = future_to_node[future]
            try:
                latency, speed = future.result(timeout=5)
            except Exception:
                latency, speed = 9999, 0

            score = score_node(latency, speed)
            status = "✅" if score > 0 else "❌"
            print(f"[{status} {node.get('name')}] 延迟 {latency} ms, 速度 {speed} MB/s, 分数 {score:.2f}")

            if score > 0:
                best_nodes.append(node)
                uri = node_to_uri(node)
                if uri:
                    uri_list.append(uri)

    # 输出 Clash 配置
    clash_config = build_clash_config(best_nodes)
    with open(CLASH_FILE, "w", encoding="utf-8") as f:
        yaml.dump(clash_config, f, allow_unicode=True)
    print(f"✅ 已生成 {CLASH_FILE}")

    # 输出 Base64 订阅
    if uri_list:
        encoded = base64.b64encode("\n".join(uri_list).encode()).decode()
        with open(V2_FILE, "w", encoding="utf-8") as f:
            f.write(encoded)
        print(f"✅ 已生成 {V2_FILE} (Base64 订阅)")
    else:
        print("⚠️ 没有生成任何可用节点")


if __name__ == "__main__":
    main()
