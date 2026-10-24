#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
订阅聚合器：把 subscriptions.json 里的订阅源统一解析成 Clash 的 proxies 列表。

旧版本的问题：只处理「明文 URI 列表」，而绝大多数订阅是「整份 base64 编码」的。
脚本对整块 base64 解码后没有继续拆行，而是把解码出来的整段文本直接塞进了
server 字段，结果 1352 个节点里有 1336 个的 server 是 "404: Not Found"、
一段 HTML、或者未解析的 vmess:// 原文。

现在按以下顺序判断输入形态：
  1. Clash YAML（含 proxies:）
  2. 整份 base64 编码的 URI 列表（最常见的订阅格式）
  3. 明文 URI 列表（每行一个）

支持的协议：vmess / vless / trojan / ss / ssr / hysteria2 / tuic
"""

import base64
import json
import os
import re
import sys
from urllib.parse import unquote, urlparse, parse_qs

import requests
import yaml

SUBS_FILE = "subscriptions.json"
OUT_FILE = "all_nodes.yaml"

TIMEOUT = 25
# 用 Clash 的 UA，多数订阅服务会直接返回 Clash YAML，比 base64 更好解析
UA = "clash.meta/1.19.33"

SCHEME_RE = re.compile(r"^(vmess|vless|trojan|ss|ssr|hysteria2|hy2|tuic)://", re.I)
B64_RE = re.compile(r"^[A-Za-z0-9+/=_\-\s]+$")

# 这些类型既能被 Clash 使用，也能还原成 URI 写进 v2.txt
SUPPORTED = {"ss", "ssr", "vmess", "vless", "trojan", "hysteria2", "tuic"}

# 每种类型必须存在的字段，缺一个就丢弃，避免生成 mihomo 无法加载的配置
REQUIRED = {
    "ss": ("cipher", "password"),
    "ssr": ("cipher", "password", "protocol", "obfs"),
    "vmess": ("uuid",),
    "vless": ("uuid",),
    "trojan": ("password",),
    "hysteria2": ("password",),
    "tuic": ("uuid", "password"),
}


# ------------------------------------------------------------------ 基础工具

def b64d(text: str) -> str:
    """宽松 base64 解码：自动补齐 padding，兼容 urlsafe 变体。"""
    s = re.sub(r"\s+", "", text or "")
    s = s.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    try:
        return base64.b64decode(s).decode("utf-8", "ignore")
    except Exception:
        return ""


def split_hostport(hostport: str):
    """拆分 host:port，兼容 [IPv6]:port 写法。"""
    hostport = hostport.strip()
    if hostport.startswith("["):
        host, _, port = hostport[1:].partition("]:")
        return host, int(port or 0)
    if hostport.count(":") > 1:          # 裸 IPv6，没有方括号
        return hostport, 0
    host, _, port = hostport.rpartition(":")
    return host, int(port or 0) if port.isdigit() else 0


def qget(q, *keys, default=""):
    """从 parse_qs 结果里取第一个存在的键。"""
    for k in keys:
        if k in q and q[k]:
            return q[k][0]
    return default


def truthy(v) -> bool:
    return str(v).lower() in ("1", "true", "yes", "on")


# ------------------------------------------------------------------ URI 解析

def parse_vmess(body: str, name: str):
    try:
        obj = json.loads(b64d(body))
    except Exception:
        return None
    if not obj.get("add") or not obj.get("id"):
        return None

    net = (obj.get("net") or "tcp").lower()
    node = {
        "name": name or obj.get("ps") or "vmess",
        "type": "vmess",
        "server": obj["add"],
        "port": int(obj.get("port") or 0),
        "uuid": obj["id"],
        "alterId": int(obj.get("aid") or 0),
        "cipher": obj.get("scy") or "auto",
        "udp": True,
    }
    if net != "tcp":
        node["network"] = net
    if net == "ws":
        node["ws-opts"] = {
            "path": obj.get("path") or "/",
            "headers": {"Host": obj.get("host") or ""},
        }
    elif net == "grpc":
        node["grpc-opts"] = {"grpc-service-name": obj.get("path") or ""}
    if truthy(obj.get("tls")):
        node["tls"] = True
        node["servername"] = obj.get("sni") or obj.get("host") or ""
        node["skip-cert-verify"] = True
    return node


def parse_vless(body: str, name: str):
    u = urlparse("vless://" + body)
    if not u.hostname or not u.username:
        return None
    q = parse_qs(u.query)
    security = qget(q, "security").lower()
    net = qget(q, "type", default="tcp").lower()

    node = {
        "name": name or "vless",
        "type": "vless",
        "server": u.hostname,
        "port": u.port or 0,
        "uuid": u.username,
        "udp": True,
    }
    flow = qget(q, "flow")
    if flow:
        node["flow"] = flow
    if net != "tcp":
        node["network"] = net
    if net == "ws":
        node["ws-opts"] = {
            "path": qget(q, "path", default="/"),
            "headers": {"Host": qget(q, "host")},
        }
    elif net == "grpc":
        node["grpc-opts"] = {"grpc-service-name": qget(q, "serviceName")}
    if security in ("tls", "reality"):
        node["tls"] = True
        node["servername"] = qget(q, "sni", "host")
        node["skip-cert-verify"] = True
        if qget(q, "fp"):
            node["client-fingerprint"] = qget(q, "fp")
    if security == "reality":
        pbk = qget(q, "pbk")
        if pbk:
            node["reality-opts"] = {"public-key": pbk, "short-id": qget(q, "sid")}
    return node


def parse_trojan(body: str, name: str):
    u = urlparse("trojan://" + body)
    if not u.hostname or not u.password:
        return None
    q = parse_qs(u.query)
    node = {
        "name": name or "trojan",
        "type": "trojan",
        "server": u.hostname,
        "port": u.port or 443,
        "password": u.password,
        "udp": True,
        "skip-cert-verify": True,
        "sni": qget(q, "sni", "peer", default=u.hostname),
    }
    net = qget(q, "type").lower()
    if net == "ws":
        node["network"] = "ws"
        node["ws-opts"] = {
            "path": qget(q, "path", default="/"),
            "headers": {"Host": qget(q, "host")},
        }
    elif net == "grpc":
        node["network"] = "grpc"
        node["grpc-opts"] = {"grpc-service-name": qget(q, "serviceName")}
    return node


def parse_ss(body: str, name: str):
    # SIP002: ss://base64(method:pass)@host:port
    if "@" in body:
        userinfo, _, hostport = body.rpartition("@")
        if ":" not in userinfo:
            userinfo = b64d(userinfo)
    else:
        # 老格式: ss://base64(method:pass@host:port)
        decoded = b64d(body)
        if "@" not in decoded:
            return None
        userinfo, _, hostport = decoded.rpartition("@")

    method, _, password = userinfo.partition(":")
    host, port = split_hostport(hostport)
    if not (method and password and host and port):
        return None
    return {
        "name": name or host,
        "type": "ss",
        "server": host,
        "port": port,
        "cipher": method,
        "password": password,
        "udp": True,
    }


def parse_ssr(body: str, name: str):
    decoded = b64d(body)
    main, _, params = decoded.partition("/?")
    # host:port:protocol:method:obfs:base64(password)
    parts = main.rsplit(":", 5)
    if len(parts) != 6:
        return None
    host, port, protocol, method, obfs, pwd_b64 = parts
    q = parse_qs(params)
    return {
        "name": name or host,
        "type": "ssr",
        "server": host,
        "port": int(port) if port.isdigit() else 0,
        "cipher": method,
        "password": b64d(pwd_b64),
        "protocol": protocol,
        "obfs": obfs,
        "protocol-param": qget(q, "protoparam"),
        "obfs-param": qget(q, "obfsparam"),
        "udp": True,
    }


def parse_hysteria2(body: str, name: str):
    u = urlparse("hysteria2://" + body)
    if not u.hostname:
        return None
    q = parse_qs(u.query)
    password = u.password or u.username or qget(q, "password")
    if not password:
        return None
    node = {
        "name": name or "hysteria2",
        "type": "hysteria2",
        "server": u.hostname,
        "port": u.port or 443,
        "password": password,
        "skip-cert-verify": True,
    }
    sni = qget(q, "sni", "peer")
    if sni:
        node["sni"] = sni
    if qget(q, "obfs") == "salamander":
        node["obfs"] = "salamander"
        node["obfs-password"] = qget(q, "obfs-password")
    return node


def parse_tuic(body: str, name: str):
    u = urlparse("tuic://" + body)
    if not u.hostname or not u.username:
        return None
    q = parse_qs(u.query)
    node = {
        "name": name or "tuic",
        "type": "tuic",
        "server": u.hostname,
        "port": u.port or 443,
        "uuid": u.username,
        "password": u.password or "",
        "udp": True,
        "skip-cert-verify": True,
    }
    if qget(q, "sni"):
        node["sni"] = qget(q, "sni")
    if qget(q, "congestion_control"):
        node["congestion-controller"] = qget(q, "congestion_control")
    return node


PARSERS = {
    "vmess": parse_vmess,
    "vless": parse_vless,
    "trojan": parse_trojan,
    "ss": parse_ss,
    "ssr": parse_ssr,
    "hysteria2": parse_hysteria2,
    "hy2": parse_hysteria2,
    "tuic": parse_tuic,
}


def parse_uri(line: str):
    """解析一行 URI，返回 Clash 节点字典或 None。"""
    m = SCHEME_RE.match(line)
    if not m:
        return None
    scheme = m.group(1).lower()
    body = line[m.end():]

    # 名称在 # 之后
    name = ""
    if "#" in body:
        body, _, frag = body.partition("#")
        name = unquote(frag).strip()

    parser = PARSERS.get(scheme)
    if not parser:
        return None
    try:
        return parser(body, name)
    except Exception:
        return None


# ------------------------------------------------------------------ 内容形态识别

def extract_yaml_proxies(text: str):
    """尝试按 Clash YAML 解析，失败返回 None。"""
    if "proxies:" not in text:
        return None
    try:
        doc = yaml.safe_load(text)
    except Exception:
        return None
    if not isinstance(doc, dict):
        return None
    proxies = doc.get("proxies")
    return proxies if isinstance(proxies, list) else None


def extract_uris(text: str):
    """
    从任意订阅内容里抠出 URI 列表。

    先按明文找；一行都没有的话，认为整份是 base64，解码后再找一次。
    """
    uris = [ln.strip() for ln in text.splitlines() if SCHEME_RE.match(ln.strip())]
    if uris:
        return uris

    # 整份 base64：去掉空白后能整体解码
    compact = re.sub(r"\s+", "", text)
    if compact and B64_RE.match(compact):
        decoded = b64d(compact)
        if "://" in decoded:
            return [ln.strip() for ln in decoded.splitlines() if SCHEME_RE.match(ln.strip())]
    return []


# ------------------------------------------------------------------ 校验与去重

def normalize_node(node: dict):
    """校验从 YAML 直接拿到的节点，返回干净的字典或 None。"""
    if not isinstance(node, dict):
        return None
    ntype = str(node.get("type", "")).lower()
    if ntype not in SUPPORTED:
        return None
    if not node.get("server") or not node.get("port"):
        return None
    try:
        node["port"] = int(node["port"])
    except (TypeError, ValueError):
        return None
    if not (0 < node["port"] < 65536):
        return None
    for field in REQUIRED.get(ntype, ()):
        if not node.get(field):
            return None
    node["type"] = ntype
    node["name"] = str(node.get("name") or node["server"])[:60]
    return node


def node_key(node: dict):
    """去重键：同一台机器上不同 UUID 是不同节点，所以要带上凭证。"""
    cred = node.get("uuid") or node.get("password") or ""
    return (node["type"], str(node["server"]).lower(), node["port"], str(cred)[:24])


# ------------------------------------------------------------------ 主流程

def main():
    if not os.path.exists(SUBS_FILE):
        print(f"[错误] 找不到 {SUBS_FILE}")
        sys.exit(1)

    with open(SUBS_FILE, encoding="utf-8") as f:
        urls = json.load(f).get("subscriptions", [])

    headers = {"User-Agent": UA}
    seen = set()
    nodes = []
    used_names = {}
    stats = []

    for url in urls:
        host = url.split("//")[-1].split("/")[0]
        try:
            resp = requests.get(url, headers=headers, timeout=TIMEOUT)
        except Exception as e:
            print(f"[跳过] {host} 拉取失败: {type(e).__name__}")
            stats.append((host, 0, "拉取失败"))
            continue

        if resp.status_code != 200:
            print(f"[跳过] {host} HTTP {resp.status_code}")
            stats.append((host, 0, f"HTTP {resp.status_code}"))
            continue

        text = resp.text.strip()
        raw_nodes = []

        proxies = extract_yaml_proxies(text)
        if proxies is not None:
            raw_nodes = [normalize_node(p) for p in proxies]
            kind = "Clash YAML"
        else:
            raw_nodes = [parse_uri(u) for u in extract_uris(text)]
            kind = "URI 列表"

        added = 0
        for node in raw_nodes:
            if not node:
                continue
            node = normalize_node(node)
            if not node:
                continue
            key = node_key(node)
            if key in seen:
                continue
            seen.add(key)

            # 节点重名会让 Clash 直接拒绝加载配置，必须去重
            base = node["name"]
            if base in used_names:
                used_names[base] += 1
                node["name"] = f"{base}-{used_names[base]}"
            else:
                used_names[base] = 1

            nodes.append(node)
            added += 1

        print(f"[{kind}] {host}: 解析 {len(raw_nodes)} 条，新增 {added} 条")
        stats.append((host, added, kind))

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        yaml.dump({"proxies": nodes}, f, allow_unicode=True,
                  sort_keys=False, default_flow_style=False)

    print(f"\n合计 {len(nodes)} 个可用节点 -> {OUT_FILE}")
    print(f"来源明细: " + ", ".join(f"{h}:{n}" for h, n, _ in stats))

    # 订阅源大面积失效时不要发布一份残缺配置，直接让 CI 失败等人来看
    min_nodes = int(os.environ.get("MIN_NODES", 50))
    if len(nodes) < min_nodes:
        print(f"[错误] 只解析出 {len(nodes)} 个节点，低于阈值 {min_nodes}，"
              f"可能是订阅源集体失效", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
