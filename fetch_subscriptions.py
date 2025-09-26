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
                all_nodes.extend(proxies)
                print(f"  解析 YAML 成功，节点数: {len(proxies)}")
            except Exception as e:
                print(f"  解析 YAML 失败: {e}")
        
        # 2️⃣ Base64 订阅（Vmess / Vless / Trojan / SS）
        else:
            for line in content.splitlines():
                line = line.strip()
                if not line:
                    continue
                # 去掉 URL 前缀
                line_clean = re.sub(r"^(vmess|vless|trojan|ss|ssr)://", "", line, flags=re.I)
                try:
                    decoded = base64.b64decode(line_clean + '=' * (-len(line_clean) % 4)).decode()
                    # JSON 格式的 Vmess
                    if decoded.startswith("{") and decoded.endswith("}"):
                        node = json.loads(decoded)
                        all_nodes.append(node)
                    else:
                        # 非 JSON，存为原始链接
                        all_nodes.append({"name": "raw_node", "server": decoded})
                except:
                    # 非 base64，直接存储原始链接
                    all_nodes.append({"name": "raw_node", "server": line})
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
