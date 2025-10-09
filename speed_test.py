#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import yaml
import base64
import urllib.parse
import socket
import time
import requests
import os

# ===== 文件路径 =====
CN_NODES_FILE = "cn_nodes.json"
ALL_NODES_FILE = "all_nodes.yaml"
CLASH_FILE = "clash.yaml"
V2_FILE = "v2.txt"

# ===== 测速配置 =====
PORT_TIMEOUT = 3
TEST_URL = "https://speed.hetzner.de/1MB.bin"  # 小文件测速
MAX_LATENCY = 200
MIN_SPEED_MB = 10  # MB/s

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
    else:
        return None

def is_port_open(host, port, timeout=PORT_TIMEOUT):
    """检测端口是否可达"""
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
        sock.close()
        return True
    except:
        return False

def test_node(node):
    """真实测速：端口+延迟+下载速度"""
    host = node.get("server")
    port = node.get("port")
    if not host or not port:
        return 5000, 0

    if not is_port_open(host, port):
        return 5000, 0

    # 测延迟
    try:
        start = time.time()
        r = requests.get(TEST_URL, timeout=MAX_LATENCY/1000)
        latency = (time.time() - start) * 1000
        if r.status_code != 200:
            return 5000, 0
    except:
        return 5000, 0

    # 测下载速度
    try:
        start = time.time()
        r = requests.get(TEST_URL, timeout=10, stream=True)
        total = 0
        for chunk in r.iter_content(1024):
            total += len(chunk)
        elapsed = time.time() - start
        speed_mb = total / (1024*1024) / elapsed  # MB/s
    except:
        return latency, 0

    return latency, speed_mb

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
                "url": "https://www.gstatic.com/generate_204",
                "interval": 300,
                "proxies": [n["name"] for n in best_nodes],
            },
        ],
        "rules": ["MATCH,🌐 节点选择"],
    }
    return config

def main():
    if not os.path.exists(CN_NODES_FILE):
        print(f"❌ {CN_NODES_FILE} 不存在")
        return
    with open(CN_NODES_FILE, "r", encoding="utf-8") as f:
        cn_nodes = json.load(f)
    if not cn_nodes:
        print("❌ 没有国内节点可用")
        return
    print(f"✅ 国内节点池加载完成 ({len(cn_nodes)} 个节点)")

    with open(ALL_NODES_FILE, "r", encoding="utf-8") as f:
        all_nodes = yaml.safe_load(f).get("proxies", [])

    best_nodes = []
    uri_list = []

    for node in all_nodes:
        latency, speed = test_node(node)
        name = node.get("name", "Unnamed")
        if latency <= MAX_LATENCY and speed >= MIN_SPEED_MB:
            best_nodes.append(node)
            uri = node_to_uri(node)
            if uri:
                uri_list.append(uri)
            print(f"[✅ {name}] 延迟 {latency:.1f} ms, 速度 {speed:.1f} MB/s")
        else:
            print(f"[❌ {name}] 延迟 {latency:.1f} ms, 速度 {speed:.1f} MB/s")

    # 生成 Clash
    clash_config = build_clash_config(best_nodes)
    with open(CLASH_FILE, "w", encoding="utf-8") as f:
        yaml.dump(clash_config, f, allow_unicode=True)
    print(f"✅ 已生成 {CLASH_FILE}")

    # 生成 V2Ray Base64 订阅
    if uri_list:
        joined = "\n".join(uri_list)
        encoded = base64.b64encode(joined.encode()).decode()
        with open(V2_FILE, "w", encoding="utf-8") as f:
            f.write(encoded)
        print(f"✅ 已生成 {V2_FILE}")
    else:
        print("⚠️ 没有生成任何可用节点")

if __name__ == "__main__":
    main()
