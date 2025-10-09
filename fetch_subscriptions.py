#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
import yaml
import base64
import json
import re

# -----------------------------
# 配置文件：subscriptions.json
# -----------------------------
with open("subscriptions.json", "r", encoding="utf-8") as f:
    data = json.load(f)

all_nodes = []

# -----------------------------
# 节点去重集合
# -----------------------------
seen_nodes = set()

# -----------------------------
# 解析每个订阅
# -----------------------------
for url in data["subscriptions"]:
    try:
        print(f"拉取订阅: {url}")
        r = requests.get(url, timeout=15)
        content = r.text.strip()

        # 1️⃣ Clash YAML 格式
        if content.startswith("proxies:") or content.startswith("Proxy:") or "proxies:" in content:
            try:
                y = yaml.safe_load(content)
                proxies = y.get("proxies", [])
                for node in proxies:
                    key = f"{node.get('server','')}:{node.get('port','')}"
                    if key not in seen_nodes:
                        seen_nodes.add(key)
                        if "type" not in node:
                            node["type"] = "unknown"
                        all_nodes.append(node)
                print(f"  解析 YAML 成功，节点数: {len(proxies)}")
            except Exception as e:
                print(f"  解析 YAML 失败: {e}")

        # 2️⃣ Base64 订阅（Vmess / Vless / Trojan / SS / SSR）
        else:
            for line in content.splitlines():
                line = line.strip()
                if not line:
                    continue
                # 去掉 URL 前缀
                proto_match = re.match(r"^(vmess|vless|trojan|ss|ssr)://", line, flags=re.I)
                proto = proto_match.group(1).lower() if proto_match else "unknown"
                line_clean = re.sub(r"^(vmess|vless|trojan|ss|ssr)://", "", line, flags=re.I)
                try:
                    decoded = base64.b64decode(line_clean + '=' * (-len(line_clean) % 4)).decode()
                except Exception:
                    decoded = line_clean  # 非 base64 就保留原始

                node = {"type": proto, "name": "raw_node", "server": "", "port": 0}

                try:
                    if proto == "vmess" and decoded.startswith("{") and decoded.endswith("}"):
                        node_json = json.loads(decoded)
                        node.update({
                            "name": node_json.get("ps", node["name"]),
                            "server": node_json.get("add"),
                            "port": int(node_json.get("port", 0)),
                            "uuid": node_json.get("id"),
                            "alterId": int(node_json.get("aid", 0)),
                            "network": node_json.get("net", "tcp"),
                            "tls": node_json.get("tls") == "tls",
                            "ws-opts": node_json.get("ws-opts", {})
                        })
                    elif proto == "ss":
                        # SS: ss://cipher:password@host:port#name
                        if "@" in decoded:
                            userinfo, hostport = decoded.split("@")
                            cipher, password = userinfo.split(":")
                            host, port = hostport.split(":")
                            node.update({
                                "server": host,
                                "port": int(port),
                                "cipher": cipher,
                                "password": password
                            })
                        else:
                            node["server"] = decoded
                    else:
                        node["server"] = decoded
                except Exception as e:
                    print(f"  节点解析失败: {line} -> {e}")

                key = f"{node.get('server','')}:{node.get('port','')}"
                if key not in seen_nodes and node.get("server"):
                    seen_nodes.add(key)
                    all_nodes.append(node)

    except Exception as e:
        print(f"  拉取失败: {url} -> {e}")

# -----------------------------
# 保存 Clash YAML
# -----------------------------
clash_dict = {"proxies": all_nodes}
with open("all_nodes.yaml", "w", encoding="utf-8") as f:
    yaml.dump(clash_dict, f, allow_unicode=True)
print(f"已生成 all_nodes.yaml，共 {len(all_nodes)} 个节点")

# -----------------------------
# 保存 V2Ray JSON
# -----------------------------
with open("all_nodes.json", "w", encoding="utf-8") as f:
    json.dump(all_nodes, f, ensure_ascii=False, indent=2)
print(f"已生成 all_nodes.json，共 {len(all_nodes)} 个节点")
