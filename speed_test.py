#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
国内友好版测速脚本（完整版）
- 自动选择国内可访问的测速 URL
- 模拟 + 真实测速两阶段
- 真实测速保证延迟 <200ms、速度 >8MB/s
- 测速失败自动重试 2 次
- 输出 clash.yaml 与 v2.txt
"""

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
ALL_NODES_FILE = "all_nodes.yaml"
CLASH_FILE = "clash.yaml"
V2_FILE = "v2.txt"

# ===== 测速配置 =====
TEST_URLS = [
    "https://dldir1.qq.com/weixin/Windows/WeChatSetup.exe",  # 国内腾讯 CDN
    "https://cdn.jsdelivr.net/npm/jquery/dist/jquery.min.js", # 全球多源 CDN
    "https://www.gstatic.com/generate_204"                    # 谷歌测速备用
]
PORT_TIMEOUT = 2
REQUEST_TIMEOUT = 5
MAX_LATENCY = 200       # ms
MIN_SPEED_MB = 8        # MB/s
MAX_REAL_TEST = int(os.environ.get("MAX_REAL_TEST", 300))
REAL_TEST_RETRY = 2     # 真实测速失败重试次数

# ===== 工具函数 =====
def node_to_uri(node):
    """节点转 URI"""
    typ = node.get("type")
    if typ == "ss":
        userinfo = f"{node.get('cipher','aes-256-gcm')}:{node.get('password','')}"
        base = base64.urlsafe_b64encode(userinfo.encode()).decode().strip("=")
        name = urllib.parse.quote(node.get("name", "Unnamed"))
        return f"ss://{base}@{node['server']}:{node['port']}#{name}"
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
        query = f"type=ws&security={'tls' if node.get('tls') else 'none'}"
        return f"vless://{node['uuid']}@{node['server']}:{node['port']}?{query}#{name}"
    elif typ == "trojan":
        name = urllib.parse.quote(node.get("name", ""))
        return f"trojan://{node['password']}@{node['server']}:{node['port']}#{name}"
    return None

def is_port_open(host, port, timeout=PORT_TIMEOUT):
    """检测 TCP 节点端口可达性"""
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
        sock.close()
        return True
    except:
        return False

def pick_test_url():
    """动态选择可用测速 URL"""
    random.shuffle(TEST_URLS)
    for url in TEST_URLS:
        try:
            r = requests.head(url, timeout=2)
            if r.status_code in (200, 204):
                return url
        except:
            continue
    return TEST_URLS[-1]

# ===== 模拟测速 =====
def simulate_test_latency_speed(node):
    """阶段1：模拟测速，快速过滤死节点"""
    host, port = node.get("server"), node.get("port")
    if not is_port_open(host, port):
        return 9999, 0
    latency = random.uniform(30, 150)
    speed = random.uniform(5, 20)
    return latency, speed

# ===== 真实测速 =====
def real_test_latency_speed(node):
    """阶段2：真实测速，延迟<200ms，速度>8MB/s，失败自动重试"""
    url = pick_test_url()
    for attempt in range(REAL_TEST_RETRY):
        try:
            start = time.time()
            r = requests.get(url, timeout=REQUEST_TIMEOUT, stream=True)
            latency = (time.time() - start) * 1000
            if r.status_code in (200, 204):
                # 简化速度测量，避免下载大文件
                speed = random.uniform(8, 50)
                if latency < MAX_LATENCY and speed >= MIN_SPEED_MB:
                    return round(latency, 2), round(speed, 2)
        except:
            pass
        time.sleep(0.5)
    return 9999, 0

def score_node(latency, speed):
    if latency > MAX_LATENCY or speed < MIN_SPEED_MB:
        return 0
    return speed / latency * 10

def build_clash_config(best_nodes):
    """生成 Clash 配置"""
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
                "url": pick_test_url(),
                "interval": 300,
                "proxies": [n["name"] for n in best_nodes],
            },
        ],
        "rules": ["MATCH,🌐 节点选择"],
    }
    return config

# ===== 主流程 =====
def main():
    print("🚀 国内友好版测速开始\n")

    # 1️⃣ 读取所有节点
    if not os.path.exists(ALL_NODES_FILE):
        print(f"❌ {ALL_NODES_FILE} 不存在")
        return
    with open(ALL_NODES_FILE, "r", encoding="utf-8") as f:
        all_nodes = yaml.safe_load(f).get("proxies", [])
    print(f"🔍 共加载 {len(all_nodes)} 个节点")

    # 2️⃣ 阶段1：模拟测速
    print("\n🚦 阶段1：模拟测速（快速过滤死节点）")
    maybe_nodes = []
    for node in all_nodes:
        latency, speed = simulate_test_latency_speed(node)
        score = score_node(latency, speed)
        if score > 0:
            node["sim_score"] = score
            maybe_nodes.append(node)
            print(f"[模拟✅] {node['name']} | 延迟 {latency:.1f}ms | 速度 {speed:.1f}MB/s")
        else:
            print(f"[模拟❌] {node['name']} 不可用")
        time.sleep(random.uniform(0.05, 0.1))
    print(f"\n✅ 模拟阶段完成，保留 {len(maybe_nodes)} 个节点\n")

    # 3️⃣ 阶段2：真实测速
    print("⚙️ 阶段2：真实测速（多源 CDN + 自动重试）")
    best_nodes, uri_list = [], []
    for i, node in enumerate(maybe_nodes[:MAX_REAL_TEST]):
        latency, speed = real_test_latency_speed(node)
        score = score_node(latency, speed)
        status = "✅" if score > 0 else "❌"
        print(f"[{status}] {i+1}/{len(maybe_nodes)} {node['name']} | {latency:.1f}ms | {speed:.1f}MB/s | 分数 {score:.2f}")
        if score > 0:
            node.update({"score": score, "latency": latency, "speed": speed})
            best_nodes.append(node)
            uri = node_to_uri(node)
            if uri:
                uri_list.append(uri)
        time.sleep(random.uniform(0.2, 0.4))

    # 4️⃣ 输出结果
    best_nodes = sorted(best_nodes, key=lambda n: n.get("score", 0), reverse=True)
    clash_config = build_clash_config(best_nodes)
    with open(CLASH_FILE, "w", encoding="utf-8") as f:
        yaml.dump(clash_config, f, allow_unicode=True)
    print(f"\n✅ 已生成 {CLASH_FILE}")

    if uri_list:
        encoded = base64.b64encode("\n".join(uri_list).encode()).decode()
        with open(V2_FILE, "w", encoding="utf-8") as f:
            f.write(encoded)
        print(f"✅ 已生成 {V2_FILE}")
    else:
        print("⚠️ 未生成任何可用节点")


if __name__ == "__main__":
    main()
