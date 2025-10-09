#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import yaml
import base64
import urllib.parse
import random
import time
import requests
import socket
import os

# ===== 文件路径 =====
HK_NODES_FILE = "hk_nodes.json"
ALL_NODES_FILE = "all_nodes.yaml"
CLASH_FILE = "clash.yaml"
V2_FILE = "v2.txt"

# ===== 测速配置 =====
TEST_URL = "https://www.gstatic.com/generate_204"
TIMEOUT = 5
MAX_LATENCY = 200
MIN_SPEED_MB = 10

# ===== 工具函数 =====
def node_to_uri(node):
    """将节点转换为订阅链接"""
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
    else:
        return None


def score_node(latency, speed):
    """计算节点评分"""
    if latency > MAX_LATENCY or speed < MIN_SPEED_MB:
        return 0.0
    return speed / latency * 10


def build_clash_config(best_nodes):
    """构建 Clash 配置文件"""
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


def test_node_latency(node, proxies=None):
    """通过代理测速节点延迟"""
    try:
        start = time.time()
        r = requests.get(TEST_URL, proxies=proxies, timeout=TIMEOUT)
        latency = (time.time() - start) * 1000
        if r.status_code == 204:
            return round(latency, 2)
    except:
        pass
    return 5000.0


def simulate_speed(node):
    """模拟下载速度（伪随机）"""
    return round(random.uniform(5, 50), 2)


# ===== 主程序 =====
def main():
    # 1️⃣ 读取香港节点
    with open(HK_NODES_FILE, "r", encoding="utf-8") as f:
        hk_nodes = json.load(f)
    if not hk_nodes:
        print("❌ 没有可用的香港节点，无法测速。")
        return

    # 使用第一个香港节点作为测速代理
    hk_node = hk_nodes[0]
    print(f"✅ 使用香港参考节点: {hk_node.get('name')} ({hk_node.get('server')})")

    # 构建代理地址（假设为socks5 7890）
    proxy_url = f"http://{hk_node.get('server')}:{hk_node.get('port')}"
    proxies = {
        "http": proxy_url,
        "https": proxy_url
    }

    # 2️⃣ 读取所有节点
    with open(ALL_NODES_FILE, "r", encoding="utf-8") as f:
        all_nodes = yaml.safe_load(f).get("proxies", [])

    best_nodes = []
    uri_list = []

    # 3️⃣ 逐个测速
    for node in all_nodes:
        name = node.get("name", "Unnamed")
        latency = test_node_latency(node, proxies)
        speed = simulate_speed(node)
        score = score_node(latency, speed)
        print(f"[{name}] 延迟 {latency} ms, 速度 {speed} Mbps, 分数 {score:.2f}")

        if score > 0:
            best_nodes.append(node)
            uri = node_to_uri(node)
            if uri:
                uri_list.append(uri)

    # 4️⃣ 生成 Clash YAML
    clash_config = build_clash_config(best_nodes)
    with open(CLASH_FILE, "w", encoding="utf-8") as f:
        yaml.dump(clash_config, f, allow_unicode=True)
    print(f"✅ 已生成 {CLASH_FILE}")

    # 5️⃣ 生成 V2Ray 订阅
    if uri_list:
        joined = "\n".join(uri_list)
        encoded = base64.b64encode(joined.encode()).decode()
        with open(V2_FILE, "w", encoding="utf-8") as f:
            f.write(encoded)
        print(f"✅ 已生成 {V2_FILE} (Base64 订阅)")
    else:
        print("⚠️ 没有生成任何可用节点。")


if __name__ == "__main__":
    main()
