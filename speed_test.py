#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import yaml
import base64
import urllib.parse
import socket
import os

# ===== 文件路径 =====
CN_NODES_FILE = "cn_nodes.json"      # 国内可用节点池
ALL_NODES_FILE = "all_nodes.yaml"    # 所有节点
CLASH_FILE = "clash.yaml"
V2_FILE = "v2.txt"

# ===== 测速配置 =====
PORT_TIMEOUT = 3

# ===== 工具函数 =====
def node_to_uri(node):
    typ = node.get("type", "").lower()
    name = urllib.parse.quote(node.get("name", "Unnamed"))

    if typ == "ss":
        userinfo = f"{node.get('cipher','aes-256-gcm')}:{node.get('password','')}"
        server_part = f"{node.get('server')}:{node.get('port')}"
        base = base64.urlsafe_b64encode(userinfo.encode()).decode().strip("=")
        return f"ss://{base}@{server_part}#{name}"

    elif typ == "ssr":
        # SSR 链接格式较复杂，这里生成原始链接
        server_part = f"{node.get('server')}:{node.get('port')}"
        return f"ssr://{server_part}"

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
        server = node.get("server")
        port = node.get("port")
        uuid = node.get("uuid")
        tls = "tls" if node.get("tls") else "none"
        path = node.get("ws-opts", {}).get("path", "")
        host = node.get("ws-opts", {}).get("headers", {}).get("Host", "")
        query = f"type=ws&security={tls}&path={urllib.parse.quote(path)}&host={host}"
        return f"vless://{uuid}@{server}:{port}?{query}#{name}"

    elif typ == "trojan":
        password = node.get("password", "")
        server = node.get("server")
        port = node.get("port")
        tls = "true" if node.get("tls") else "false"
        return f"trojan://{password}@{server}:{port}#{name}"

    else:
        return None

def is_port_open(host, port, timeout=PORT_TIMEOUT):
    """检测节点端口是否可达"""
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
        sock.close()
        return True
    except:
        return False

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

# ===== 主程序 =====
def main():
    if not os.path.exists(CN_NODES_FILE):
        print(f"❌ {CN_NODES_FILE} 不存在")
        return
    with open(CN_NODES_FILE, "r", encoding="utf-8") as f:
        cn_nodes = json.load(f)
    if not cn_nodes:
        print("❌ 没有可用的国内节点")
        return
    print(f"✅ 国内节点池加载完成 ({len(cn_nodes)} 个节点)")

    if not os.path.exists(ALL_NODES_FILE):
        print(f"❌ {ALL_NODES_FILE} 不存在")
        return
    with open(ALL_NODES_FILE, "r", encoding="utf-8") as f:
        all_nodes = yaml.safe_load(f).get("proxies", [])

    best_nodes = []
    uri_list = []

    for node in all_nodes:
        server = node.get("server")
        port = node.get("port")
        name = node.get("name", "Unnamed")
        if not server or not port:
            continue

        if is_port_open(server.split(":")[0], port):
            best_nodes.append(node)
            uri = node_to_uri(node)
            if uri:
                uri_list.append(uri)
            print(f"[✅ {name}] 节点可用 -> {server}:{port}")
        else:
            print(f"[❌ {name}] 节点不可用 -> {server}:{port}")

    # 生成 Clash YAML
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
        print(f"✅ 已生成 {V2_FILE} (Base64 订阅)")
    else:
        print("⚠️ 没有生成任何可用节点。")

if __name__ == "__main__":
    main()
