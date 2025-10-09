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
OUTPUT_CN_POOL_FILE = "cn_nodes.json"  # 输出国内可用节点
CN_NODE_COUNT = 10                      # 最多保留数量
PORT_TIMEOUT = 3                        # 端口检测超时（秒）

# =============================
# 内置中国大陆 IP 段（常用）
# =============================
CN_IP_RANGES = [
    "1.0.1.0/24", "14.0.0.0/8", "27.0.0.0/8", "36.0.0.0/8", "39.0.0.0/8",
    "42.0.0.0/8", "58.0.0.0/7", "59.0.0.0/8", "60.0.0.0/8", "61.232.0.0/14",
    "101.0.0.0/8", "103.0.0.0/8", "106.0.0.0/8", "110.0.0.0/8", "111.0.0.0/8",
    "112.0.0.0/8", "113.0.0.0/8", "114.0.0.0/8", "115.0.0.0/8", "116.0.0.0/8",
    "117.0.0.0/8", "118.0.0.0/8", "119.0.0.0/8", "120.0.0.0/8", "121.0.0.0/8",
    "122.0.0.0/8", "123.0.0.0/8", "124.0.0.0/8", "125.0.0.0/8", "139.0.0.0/8",
    "140.0.0.0/8", "150.0.0.0/8", "180.0.0.0/8", "182.0.0.0/8", "183.0.0.0/8",
    "210.0.0.0/8", "211.0.0.0/8", "218.0.0.0/8", "219.0.0.0/8", "220.0.0.0/8",
    "221.0.0.0/8", "222.0.0.0/8", "223.0.0.0/8"
]

# =============================
# 工具函数
# =============================

def is_cn_ip(ip: str) -> bool:
    """判断是否为中国大陆 IP"""
    try:
        ip_obj = ipaddress.ip_address(ip)
        for cidr in CN_IP_RANGES:
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

cn_nodes = []

print(f"📦 共加载 {len(all_nodes)} 个节点，开始筛选国内可用节点...")

for node in all_nodes:
    name = node.get("name", "").lower()
    server = node.get("server", "").strip()
    port = node.get("port")

    if not server or not port:
        continue

    ip_only = safe_get_ip(server)

    # 匹配节点名或IP判断是否国内
    if re.search(r"(cn|china|国内)", name) or is_cn_ip(ip_only):
        print(f"🔍 检测节点 {name} ({server}:{port}) ... ", end="", flush=True)
        if is_node_reachable(ip_only, int(port)):
            print("✅ 可用")
            cn_nodes.append(node)
        else:
            print("❌ 不可达")

    # 防止 GitHub Action 卡死（延迟）
    time.sleep(random.uniform(0.2, 0.5))

# =============================
# 保留前 N 个
# =============================
cn_nodes = cn_nodes[:CN_NODE_COUNT]

with open(OUTPUT_CN_POOL_FILE, "w", encoding="utf-8") as f:
    json.dump(cn_nodes, f, ensure_ascii=False, indent=2)

print("\n===============================")
print(f"✅ 已生成参考国内可用节点池 ({len(cn_nodes)} 个节点)")
for i, node in enumerate(cn_nodes, 1):
    print(f"{i}. {node.get('name')} -> {node.get('server')}:{node.get('port')}")
print("===============================")
