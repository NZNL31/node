#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import ipaddress
import re
import socket
import concurrent.futures

# -----------------------------
# 配置
# -----------------------------
ALL_NODES_FILE = "all_nodes.json"      # 所有节点文件
OUTPUT_CN_POOL_FILE = "cn_nodes.json"  # 输出国内可用节点
CN_NODE_COUNT = 10                     # 最多保留数量
PORT_TIMEOUT = 2                       # 每个连接超时（秒）
MAX_WORKERS = 30                       # 并发线程数（防止卡死）

# -----------------------------
# 国内 IP 段（简版）
# -----------------------------
CN_IP_RANGES = [
    "1.0.1.0/24", "14.0.0.0/8", "27.0.0.0/8", "36.0.0.0/8",
    "39.0.0.0/8", "42.0.0.0/8", "58.0.0.0/7", "60.0.0.0/8",
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
    except Exception:
        return False

# -----------------------------
# 单个节点检测任务
# -----------------------------
def check_node(node):
    name = node.get("name", "").lower()
    server = node.get("server", "").lower()
    port = node.get("port")

    if not server or not port:
        return None

    # 解析域名到 IP（避免 DNS 阻塞）
    try:
        ip_only = socket.gethostbyname(server.split(":")[0])
    except Exception:
        return None

    # 判断国内关键字或 IP 段
    if re.search(r"cn|china|国内", name) or is_cn_ip(ip_only):
        if is_node_reachable(ip_only, int(port)):
            return node
    return None

# -----------------------------
# 主逻辑
# -----------------------------
def main():
    with open(ALL_NODES_FILE, "r", encoding="utf-8") as f:
        all_nodes = json.load(f)

    cn_nodes = []

    # 并发检测
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(check_node, node) for node in all_nodes]
        for i, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            result = future.result()
            if result:
                cn_nodes.append(result)
                print(f"[√] 可用节点：{result['name']} ({result['server']}:{result['port']})")
            else:
                print(f"[×] 检测第 {i} 个节点无效")

    # 截取前 N 个
    cn_nodes = cn_nodes[:CN_NODE_COUNT]

    # 保存结果
    with open(OUTPUT_CN_POOL_FILE, "w", encoding="utf-8") as f:
        json.dump(cn_nodes, f, ensure_ascii=False, indent=2)

    print("\n✅ 已生成国内可用节点池：")
    for i, node in enumerate(cn_nodes, start=1):
        print(f"{i}. {node.get('name')} -> {node.get('server')}:{node.get('port')}")
    print(f"共 {len(cn_nodes)} 个节点")

# -----------------------------
# 启动
# -----------------------------
if __name__ == "__main__":
    main()
