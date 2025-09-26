#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import ipaddress
import re

# -----------------------------
# 配置
# -----------------------------
ALL_NODES_FILE = "all_nodes.json"         # 拉取后生成的所有节点文件
OUTPUT_HK_POOL_FILE = "hk_nodes.json"     # 输出参考节点池文件
HK_NODE_COUNT = 3                          # 参考节点池最多数量

# 香港 IP 段列表（可扩展）
HK_IP_RANGES = [
    "1.0.180.0/22", "1.34.0.0/16", "14.0.0.0/8", "27.0.0.0/16", "58.96.0.0/12",
    "59.148.0.0/16", "61.64.0.0/11", "119.148.0.0/16", "124.0.0.0/8", "125.64.0.0/11"
]

# -----------------------------
# 判断是否香港 IP
# -----------------------------
def is_hk_ip(ip):
    try:
        for cidr in HK_IP_RANGES:
            if ipaddress.ip_address(ip) in ipaddress.ip_network(cidr):
                return True
        return False
    except:
        return False

# -----------------------------
# 读取所有节点
# -----------------------------
with open(ALL_NODES_FILE, "r", encoding="utf-8") as f:
    all_nodes = json.load(f)

hk_nodes = []

# -----------------------------
# 挑选香港节点
# -----------------------------
for node in all_nodes:
    name = node.get("name", "").lower()
    server = node.get("server", "").lower()

    # 1️⃣ 名称或 server 包含关键词
    if re.search(r"hk|hongkong", name) or re.search(r"hk|hongkong", server):
        hk_nodes.append(node)
        continue

    # 2️⃣ IP 段判断（server 是 IP 地址的情况）
    try:
        ip_only = server.split(":")[0]  # 去掉端口
        if is_hk_ip(ip_only):
            hk_nodes.append(node)
    except:
        pass

# -----------------------------
# 保留前 N 个参考节点
# -----------------------------
hk_nodes = hk_nodes[:HK_NODE_COUNT]

# -----------------------------
# 输出参考节点池
# -----------------------------
with open(OUTPUT_HK_POOL_FILE, "w", encoding="utf-8") as f:
    json.dump(hk_nodes, f, ensure_ascii=False, indent=2)

print(f"已生成参考香港节点池 ({len(hk_nodes)} 个节点)：")
for i, node in enumerate(hk_nodes, start=1):
    print(f"{i}. {node.get('name')} -> {node.get('server')}")
