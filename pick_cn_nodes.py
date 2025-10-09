#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import ipaddress
import re
import socket
import time
import random

# =============================
# 配置
# =============================
ALL_NODES_FILE = "all_nodes.json"      # 所有节点文件
OUTPUT_HK_POOL_FILE = "hk_nodes.json"  # 输出香港可用节点
HK_NODE_COUNT = 3                      # 最多保留数量
PORT_TIMEOUT = 3                       # 端口检测超时（秒）

# =============================
# 内置香港常见 IP 段（2024 更新）
# =============================
HK_IP_RANGES = [
    "43.224.16.0/22", "45.64.56.0/22", "58.152.0.0/16",
    "59.148.0.0/15", "101.1.0.0/22", "103.11.100.0/22",
    "103.14.32.0/22", "103.30.4.0/22", "103.55.200.0/22",
    "103.70.136.0/22", "103.103.244.0/22", "103.118.40.0/22",
    "103.145.204.0/22", "103.183.188.0/22", "103.243.24.0/22",
    "103.56.112.0/22", "112.118.0.0/16", "112.119.176.0/20",
    "113.254.0.0/16", "118.140.0.0/14", "119.236.0.0/16",
    "122.128.0.0/15", "123.203.0.0/16", "124.217.0.0/16",
    "134.238.0.0/16", "137.59.56.0/22", "143.92.0.0/16",
    "147.8.0.0/16", "155.94.160.0/19", "156.0.0.0/16",
    "180.235.48.0/22", "182.239.112.0/22", "202.40.192.0/19",
    "202.64.0.0/13", "203.145.64.0/18", "210.0.128.0/17",
    "218.102.0.0/15", "219.76.0.0/15", "220.241.0.0/16"
]

# =============================
# 工具函数
# =============================

def is_hk_ip(ip: str) -> bool:
    """判断是否为香港 IP"""
    try:
        ip_obj = ipaddress.ip_address(ip)
        for cidr in HK_IP_RANGES:
            if ip_obj in ipaddress.ip_network(cidr):
                return True
        return False
    except ValueError:
        return False


def is_node_reachable(server: str, port: int, timeout=PORT_TIMEOUT) -> bool:
    """检测节点端口是否可达（TCP连接）"""
    try:
        with socket.create_connection((server, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def safe_get_ip(server: str) -> str:
    """解析域名为 IP（失败则返回原 server）"""
    try:
        return socket.gethostbyname(server)
    except:
        return server


# =============================
# 主逻辑
# =============================

print("🚀 加载节点文件...")

try:
    with open(ALL_NODES_FILE, "r", encoding="utf-8") as f:
        all_nodes = json.load(f)
except Exception as e:
    print(f"❌ 无法读取 {ALL_NODES_FILE}: {e}")
    exit(1)

hk_nodes = []

print(f"📦 共加载 {len(all_nodes)} 个节点，开始筛选香港可用节点...")

for node in all_nodes:
    name = node.get("name", "").lower()
    server = node.get("server", "").strip()
    port = node.get("port")

    if not server or not port:
        continue

    ip_only = safe_get_ip(server)

    # 判断是否香港节点：名称包含HK或IP属于香港段
    if re.search(r"(🇭🇰|hk|hongkong|香港)", name) or is_hk_ip(ip_only):
        print(f"🔍 检测节点 {name} ({server}:{port}) ... ", end="", flush=True)
        if is_node_reachable(ip_only, int(port)):
            print("✅ 可用")
            hk_nodes.append(node)
        else:
            print("❌ 不可达")

    # 防止 GitHub Action 被限速
    time.sleep(random.uniform(0.2, 0.5))

# =============================
# 保留前 N 个
# =============================
hk_nodes = hk_nodes[:HK_NODE_COUNT]

with open(OUTPUT_HK_POOL_FILE, "w", encoding="utf-8") as f:
    json.dump(hk_nodes, f, ensure_ascii=False, indent=2)

print("\n===============================")
print(f"✅ 已生成参考香港可用节点池 ({len(hk_nodes)} 个节点)")
for i, node in enumerate(hk_nodes, 1):
    print(f"{i}. {node.get('name')} -> {node.get('server')}:{node.get('port')}")
print("===============================")
