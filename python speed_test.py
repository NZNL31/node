#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import yaml
import time
import random
import requests
from requests.exceptions import RequestException

ALL_NODES_FILE = "all_nodes.json"
HK_NODE_POOL_FILE = "hk_nodes.json"
OUTPUT_YAML_FILE = "best_nodes.yaml"
OUTPUT_JSON_FILE = "best_nodes.json"
TOP_N = 20  # 最终优选节点数量

# -----------------------------
# 网速和延迟要求
# -----------------------------
MIN_SPEED_KB = 1250      # 下行 ≥ 10 Mbps ≈ 1250 KB/s
MAX_LATENCY_MS = 200     # 延迟 ≤ 200 ms

TIMEOUT = 5  # 测速超时（秒）

# -----------------------------
# 读取节点
# -----------------------------
with open(ALL_NODES_FILE, "r", encoding="utf-8") as f:
    all_nodes = json.load(f)

with open(HK_NODE_POOL_FILE, "r", encoding="utf-8") as f:
    hk_nodes = json.load(f)

# -----------------------------
# 测速函数：延迟 + 下载速度
# -----------------------------
def measure_node(node, proxy_node=None):
    server = node.get("server")
    port = node.get("port", 443)

    proxies = None
    if proxy_node:
        proxy_ip = proxy_node.get("server")
        proxy_port = proxy_node.get("port", 1080)
        proxies = {
            "http": f"socks5h://{proxy_ip}:{proxy_port}",
            "https": f"socks5h://{proxy_ip}:{proxy_port}"
        }

    latency = TIMEOUT * 1000
    speed = 0
    try:
        url = f"https://{server}:{port}/"
        start = time.time()
        r = requests.get(url, timeout=TIMEOUT, proxies=proxies)
        end = time.time()
        latency = (end - start) * 1000
        size_kb = len(r.content) / 1024
        speed = size_kb / (end - start)
    except RequestException:
        pass

    # 过滤条件：下行 >= MIN_SPEED_KB, 延迟 <= MAX_LATENCY_MS
    if latency > MAX_LATENCY_MS or speed < MIN_SPEED_KB:
        score = 0
    else:
        score = 1000 / latency + speed

    return {"latency": latency, "speed": speed, "score": score}

# -----------------------------
# 二阶段测速
# -----------------------------
node_results = []

for node in all_nodes:
    proxy_node = random.choice(hk_nodes) if hk_nodes else None
    result = measure_node(node, proxy_node)
    node_results.append({"node": node, **result})
    print(f"{node.get('name')}: 延迟 {result['latency']:.1f} ms, 速度 {result['speed']:.1f} KB/s, 分数 {result['score']:.2f}")

# 排序 & TOP_N
node_results.sort(key=lambda x: x["score"], reverse=True)
best_nodes = [item["node"] for item in node_results if item["score"] > 0][:TOP_N]

# 输出 JSON
with open(OUTPUT_JSON_FILE, "w", encoding="utf-8") as f:
    json.dump(best_nodes, f, ensure_ascii=False, indent=2)

# 输出 Clash YAML
with open(OUTPUT_YAML_FILE, "w", encoding="utf-8") as f:
    yaml.dump({"proxies": best_nodes}, f, allow_unicode=True)

print(f"已生成优选订阅 {OUTPUT_JSON_FILE} & {OUTPUT_YAML_FILE}, 共 {len(best_nodes)} 个节点符合要求")
