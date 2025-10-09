#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import ipaddress
import re
import socket

# -----------------------------
# 配置
# -----------------------------
ALL_NODES_FILE = "all_nodes.json"      # 所有节点文件
OUTPUT_CN_POOL_FILE = "cn_nodes.json"  # 输出国内可用节点
CN_NODE_COUNT = 10                      # 最多保留数量
PORT_TIMEOUT = 3                        # 端口检测超时（秒）

# 国内 IP 段示例（可扩展）
CN_IP_RANGES = [
    "1.0.1.0/24", "1.0.2.0/23", "1.0.8.0/21", "14.0.0.0/8", "27.0.0.0/8",
    "36.0.0.0/8", "39.0.0.0/8", "42.0.0.0/8", "58.0.0.0/7", "60.0.0.0/8",
    "61.232.0.0/14", "101.0.0.0/8", "103.0.0.0/8", "110.0.0.0/8"
]

# -----------------------------
# 判断是否国内 IP
# -----------------------------
def is_cn_ip(ip):
    try:
        for cidr in CN_IP_RANGES:
            if ipaddress.ip_address(ip) in ipaddress.ip_network(cidr):
                return True
        return False
    except:
        return False

# -----------------------------
# 检查节点端口是否可达
# -----------------------------
def is_node_reachable(server: str, port: int, timeout=PORT_TIMEOUT) -> bool:
    try:
        sock = socket.create_connection((server, port), timeout=timeout)
        sock.close()
        return True
    except:
        return False

# -----------------------------
# 读取所有节点
# -----------------------------
with open(ALL_NODES_FILE, "r", encoding="utf-8") as f:
    all_nodes = json.load(f)

cn_nodes = []

# -----------------------------
# 挑选国内可用节点
# -----------------------------
for node in all_nodes:
    name = node.get("name", "").lower()
    server = node.get("server", "").lower()
    port = node.get("port")

    if not server or not port:
        continue

    # 名称关键词判断
    if re.search(r"cn|china|国内", name) or re.search(r"cn|china|国内", server):
        if is_node_reachable(server.split(":")[0], int(port)):
            cn_nodes.append(node)
            continue

    # IP 段判断
    try:
        ip_only = server.split(":")[0]
        if is_cn_ip(ip_only) and is_node_reachable(ip_only, int(port)):
            cn_nodes.append(node)
    except:
        pass

# -----------------------------
# 保留前 N 个参考节点
# -----------------------------
cn_nodes = cn_nodes[:CN_NODE_COUNT]

# -----------------------------
# 输出参考节点池
# -----------------------------
with open(OUTPUT_CN_POOL_FILE, "w", encoding="utf-8") as f:
    json.dump(cn_nodes, f, ensure_ascii=False, indent=2)

print(f"已生成参考国内可用节点池 ({len(cn_nodes)} 个节点)：")
for i, node in enumerate(cn_nodes, start=1):
    print(f"{i}. {node.get('name')} -> {node.get('server')}:{node.get('port')}")
