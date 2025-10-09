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
ALL_NODES_FILE = "all_nodes.json"       # 所有节点文件
OUTPUT_SG_POOL_FILE = "sg_nodes.json"   # 输出新加坡可用节点
SG_NODE_COUNT = 3                       # 最多保留数量
PORT_TIMEOUT = 3                        # 端口检测超时（秒）

# =============================
# 新加坡常见 IP 段（2024 更新）
# =============================
SG_IP_RANGES = [
    "14.128.0.0/16", "23.100.224.0/19", "27.54.64.0/19",
    "42.60.0.0/15", "43.225.48.0/22", "43.245.152.0/22",
    "45.64.56.0/22", "45.117.12.0/22", "47.88.0.0/16",
    "52.76.0.0/15", "54.169.0.0/16", "101.100.0.0/22",
    "103.1.152.0/22", "103.3.68.0/22", "103.5.60.0/22",
    "103.8.172.0/22", "103.11.180.0/22", "103.24.72.0/22",
    "103.26.44.0/22", "103.31.200.0/22", "103.104.220.0/22",
    "103.107.220.0/22", "103.133.80.0/22", "103.166.80.0/22",
    "110.35.0.0/16", "111.65.0.0/16", "112.198.0.0/15",
    "113.197.0.0/16", "116.12.0.0/16", "118.189.0.0/16",
    "119.73.0.0/16", "122.11.128.0/18", "124.6.0.0/16",
    "128.199.0.0/16", "139.162.0.0/16", "139.180.0.0/16",
    "147.139.0.0/16", "157.230.0.0/16", "159.89.0.0/16",
    "161.117.0.0/16", "163.47.0.0/16", "165.154.0.0/16",
    "167.71.0.0/16", "172.104.0.0/16", "175.156.0.0/14",
    "182.55.0.0/16", "202.166.64.0/19", "203.117.0.0/16",
    "210.23.0.0/16", "218.186.0.0/15", "220.255.0.0/16"
]

# =============================
# 工具函数
# =============================

def is_sg_ip(ip: str) -> bool:
    """判断是否为新加坡 IP"""
    try:
        ip_obj = ipaddress.ip_address(ip)
        for cidr in SG_IP_RANGES:
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

sg_nodes = []

print(f"📦 共加载 {len(all_nodes)} 个节点，开始筛选新加坡可用节点...")

for node in all_nodes:
    name = node.get("name", "").lower()
    server = node.get("server", "").strip()
    port = node.get("port")

    if not server or not port:
        continue

    ip_only = safe_get_ip(server)

    # 判断是否新加坡节点：名称包含SG或IP属于新加坡段
    if re.search(r"(🇸🇬|sg|singapore|新加坡)", name) or is_sg_ip(ip_only):
        print(f"🔍 检测节点 {name} ({server}:{port}) ... ", end="", flush=True)
        if is_node_reachable(ip_only, int(port)):
            print("✅ 可用")
            sg_nodes.append(node)
        else:
            print("❌ 不可达")

    # 防止 GitHub Action 被限速
    time.sleep(random.uniform(0.2, 0.5))

# =============================
# 保留前 N 个
# =============================
sg_nodes = sg_nodes[:SG_NODE_COUNT]

with open(OUTPUT_SG_POOL_FILE, "w", encoding="utf-8") as f:
    json.dump(sg_nodes, f, ensure_ascii=False, indent=2)

print("\n===============================")
print(f"✅ 已生成新加坡可用节点池 ({len(sg_nodes)} 个节点)")
for i, node in enumerate(sg_nodes, 1):
    print(f"{i}. {node.get('name')} -> {node.get('server')}:{node.get('port')}")
print("===============================")
