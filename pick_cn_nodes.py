#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import re
import socket
import time
import ipaddress

# -----------------------------
# 配置
# -----------------------------
ALL_NODES_FILE = "all_nodes.json"      # 所有节点输入文件
CN_IP_RANGES_FILE = "cn_ip_ranges.json"  # 国内 IP 段文件
OUTPUT_CN_POOL_FILE = "cn_nodes.json"  # 输出文件
CN_NODE_COUNT = 10                      # 保留节点数量
PORT_TIMEOUT = 3                        # 端口检测超时
SPEED_THRESHOLD = 8                     # Mbps 下限（示意）
PING_THRESHOLD = 200                    # ms 上限（示意）

# -----------------------------
# 工具函数
# -----------------------------

def is_node_reachable(server: str, port: int, proto="tcp", timeout=PORT_TIMEOUT) -> bool:
    """检测节点端口可达性（区分 TCP / UDP）"""
    try:
        if proto == "tcp":
            sock = socket.create_connection((server, port), timeout=timeout)
            sock.close()
        else:  # UDP
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            sock.sendto(b"ping", (server, port))
        return True
    except Exception:
        return False


def ping_host(server, timeout=1):
    """估算延迟（伪 ping，基于 TCP 握手时间）"""
    start = time.time()
    try:
        sock = socket.create_connection((server, 80), timeout=timeout)
        sock.close()
        return round((time.time() - start) * 1000, 2)
    except Exception:
        return 9999


def is_cn_ip(ip, cn_ranges):
    """判断 IP 是否属于国内段"""
    try:
        ip_obj = ipaddress.ip_address(ip)
        for net in cn_ranges:
            if ip_obj in ipaddress.ip_network(net):
                return True
    except:
        pass
    return False


# -----------------------------
# 主逻辑
# -----------------------------
print("🚀 加载节点与国内 IP 段...")
with open(ALL_NODES_FILE, "r", encoding="utf-8") as f:
    all_nodes = json.load(f)

with open(CN_IP_RANGES_FILE, "r", encoding="utf-8") as f:
    CN_IP_RANGES = json.load(f)

cn_nodes = []

# -----------------------------
# 筛选逻辑
# -----------------------------
print(f"🔍 正在筛选 {len(all_nodes)} 个节点...")

for node in all_nodes:
    name = node.get("name", "").lower()
    server = node.get("server", "").split(":")[0]
    port = int(node.get("port", 0))
    proto = node.get("type", "").lower()
    proto = "udp" if proto in ["hysteria", "hysteria2", "tuic", "quic"] else "tcp"

    if not server or not port:
        continue

    # 1️⃣ 名称关键词判断
    if re.search(r"(cn|china|国内|sh|bj|ct|cu|cm|cn2|cmi|cmcc|chinatelecom|chinaunicom|chinamobile)", name):
        if is_node_reachable(server, port, proto=proto):
            delay = ping_host(server)
            if delay < PING_THRESHOLD:
                node["ping"] = delay
                cn_nodes.append(node)
            continue

    # 2️⃣ IP 段判断
    try:
        if is_cn_ip(server, CN_IP_RANGES) and is_node_reachable(server, port, proto=proto):
            delay = ping_host(server)
            if delay < PING_THRESHOLD:
                node["ping"] = delay
                cn_nodes.append(node)
    except:
        continue

# -----------------------------
# 排序与输出
# -----------------------------
cn_nodes.sort(key=lambda n: n.get("ping", 9999))
cn_nodes = cn_nodes[:CN_NODE_COUNT]

with open(OUTPUT_CN_POOL_FILE, "w", encoding="utf-8") as f:
    json.dump(cn_nodes, f, ensure_ascii=False, indent=2)

print(f"\n✅ 已生成参考国内可用节点池 ({len(cn_nodes)} 个节点)：\n")
for i, node in enumerate(cn_nodes, start=1):
    print(f"{i}. {node.get('name')} -> {node.get('server')}:{node.get('port')} ({node.get('ping', '?')} ms)")
