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
import hashlib
import ipaddress
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
# 单个源最多收多少个节点（0 = 不限）。巨型全量 dump 靠这个兜底。
SOURCE_LIMIT = int(os.environ.get("SOURCE_LIMIT", 1200))
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
    """从 parse_qs 结果里取第一个**非空**的键。

    注意必须跳过空值：`?sni=&host=x` 会让 sni 命中但取到空串，后面的 host 就永远
    轮不到 —— 表现为节点「SNI 为空、连不上」。
    """
    for k in keys:
        if k in q and q[k] and str(q[k][0]).strip():
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
    # 标准写法 trojan://password@host:port 里 userinfo 没有冒号，
    # urlparse 会把它放进 username 而不是 password，两个都要看。
    password = unquote(u.password or u.username)
    if not u.hostname or not password:
        return None
    q = parse_qs(u.query)
    node = {
        "name": name or "trojan",
        "type": "trojan",
        "server": u.hostname,
        "port": u.port or 443,
        "password": password,
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
            # 兼容 SIP002 的明文 userinfo；密码里的 @ : # 等字符必须以百分号编码。
            userinfo = unquote(userinfo)
    else:
        # 老格式: ss://base64(method:pass@host:port)
        decoded = b64d(body)
        if "@" not in decoded:
            return None
        userinfo, _, hostport = decoded.rpartition("@")

    method, _, password = userinfo.partition(":")
    host, port = split_hostport(unquote(hostport))
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
        "protocol-param": b64d(qget(q, "protoparam")),
        "obfs-param": b64d(qget(q, "obfsparam")),
        "udp": True,
    }


def parse_hysteria2(body: str, name: str):
    u = urlparse("hysteria2://" + body)
    if not u.hostname:
        return None
    q = parse_qs(u.query)
    password = unquote(u.password or u.username or qget(q, "password"))
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
        "uuid": unquote(u.username),
        "password": unquote(u.password or ""),
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

def is_usable_server(server) -> bool:
    """
    排除指向本机/内网/保留地址的节点。

    有些订阅源会带上占位模板，server 写成 127.0.0.53 之类（实测一个源里有 10 条
    vmess 都是这个地址），这种节点在任何机器上都连不通，纯属占位符。
    域名不在这里判断（很多节点用域名，解析结果这里不做假设）。
    """
    s = str(server or "").strip().strip("[]")
    if not s:
        return False
    try:
        ip = ipaddress.ip_address(s)
    except ValueError:
        return True                      # 是域名，放过
    # is_global 同时排除私有、回环、链路本地、组播和 100.64.0.0/10 等保留段。
    # 节点地址必须是公网可路由的；逐个枚举保留属性容易漏掉新类别。
    return ip.is_global


def sample_source(nodes, limit):
    """
    单个源超过上限时按哈希取样。

    有些源是一整份几十万条的全量 dump（实测 rtwo2 的 curated.txt 有 5.5 万条、18.7MB），
    全量收进来会让候选池远超探活预算 —— 没被探活的节点照样会发布，等于把垃圾灌进
    订阅。按哈希取样既能保留多样性，又不受文件内排序影响（文件头部常常是同一批节点）。
    """
    if limit <= 0 or len(nodes) <= limit:
        return nodes
    return sorted(nodes, key=lambda n: hashlib.md5(
        f"{str(n.get('server', '')).lower()}:{n.get('port')}".encode()
    ).hexdigest())[:limit]


def fill_sni(node: dict):
    """
    给开了 TLS 但没有 SNI 的节点补一个。

    没有 SNI 的 TLS 握手在 CDN / 多域名前置的节点上直接失败（服务端不知道要出示
    哪张证书），客户端表现就是「连不上」。补的值按可信度依次取：ws 的 Host 头 →
    服务器地址本身。前者是真正的目标域名，后者只是兜底。
    """
    if not node.get("tls"):
        return node
    key = "sni" if node.get("type") in ("trojan", "hysteria2", "tuic") else "servername"
    if node.get(key) or node.get("servername") or node.get("sni"):
        return node
    host = ((node.get("ws-opts") or {}).get("headers") or {}).get("Host")
    node[key] = str(host).strip() if host else str(node.get("server", ""))
    return node


_CTRL = re.compile(r"[\x00-\x1f\x7f]")
# SS 的加密方式必须在这张表里（或 2022-blake3-* 系列）。实测源里有把 cipher 写成
# 字面量 "ss"、或者 base64 里混进 \r 变成 "^m" 的，客户端和 sing-box 都会因为
# 「unknown method」拒绝加载整份配置 —— 一个坏节点能拖垮整份订阅。
SS_METHODS = {
    "aes-128-gcm", "aes-192-gcm", "aes-256-gcm",
    "aes-128-cfb", "aes-192-cfb", "aes-256-cfb",
    "aes-128-ctr", "aes-192-ctr", "aes-256-ctr",
    "chacha20", "chacha20-ietf", "xchacha20",
    "chacha20-ietf-poly1305", "xchacha20-ietf-poly1305",
    "rc4-md5", "none",
}


def clean_str(value) -> str:
    """去掉控制字符和首尾空白。"""
    return _CTRL.sub("", str(value if value is not None else "")).strip()


def normalize_node(node: dict):
    """校验从 YAML 直接拿到的节点，返回干净的字典或 None。"""
    if not isinstance(node, dict):
        return None
    ntype = str(node.get("type", "")).lower()
    if ntype not in SUPPORTED:
        return None
    # 先清掉所有字符串字段里的控制字符，再走校验
    for k, v in list(node.items()):
        if isinstance(v, str):
            node[k] = clean_str(v)
    if not node.get("server") or not node.get("port"):
        return None
    if not is_usable_server(node.get("server")):
        return None
    try:
        node["port"] = int(node["port"])
    except (TypeError, ValueError):
        return None
    if not (0 < node["port"] < 65536):
        return None
    if ntype == "ss":
        method = str(node.get("cipher") or "").lower()
        if method not in SS_METHODS and not method.startswith("2022-blake3-"):
            return None
        if method.startswith("2022-blake3-"):
            # 2022 系列的 password 必须是 base64 的 16/32 字节密钥，否则客户端
            # 加载整份配置时直接报 decode key 失败
            try:
                key = base64.b64decode(
                    str(node.get("password", "")) + "=" * (-len(str(node.get("password", ""))) % 4),
                    validate=True)
            except Exception:
                return None
            if len(key) not in (16, 32):
                return None
    for field in REQUIRED.get(ntype, ()):
        if not node.get(field):
            return None
    # xtls-rprx-vision 是 TLS 上的协议，没有 TLS 的 flow 节点物理上不可能连通。
    # 实测源里有 34 个这种（多半是从完整写法里剥掉了 security=tls）。
    if ntype == "vless" and node.get("flow") and not node.get("tls"):
        return None
    node = fill_sni(node)
    node["type"] = ntype
    node["name"] = str(node.get("name") or node["server"])[:60]
    return node


def completeness(node: dict):
    """节点信息完整度，用于在「同一节点被多个源写了不同版本」时挑最全的那个。"""
    return (bool(node.get("tls")),
            bool(node.get("servername") or node.get("sni")),
            (node.get("network") or "tcp") != "tcp",
            bool(node.get("ws-opts")),
            bool(node.get("grpc-opts")),
            len(node))


def identity(node: dict):
    """同一节点的身份：协议 + 服务器 + 端口 + 凭证（不含传输层）。"""
    # tuic 同时用 UUID 和密码认证，因此不能只取其中一个；也不能截断凭证，
    # 否则前 24 位相同的 UUID/长密码会被误判成同一节点。
    cred = (str(node.get("uuid") or ""), str(node.get("password") or ""))
    return (node["type"], str(node["server"]).lower(), int(node["port"]),
            cred)


def prefer_complete(nodes):
    """
    同一个节点被多个源写了不同完整度时，丢掉信息更少的那些。

    典型例子：free18 把某节点写成 `security=none&type=tcp`（无 TLS、无 SNI、
    无 ws），而另一个源对同一个 UUID + 同一个 server:port 写的是
    `security=tls&sni=...&type=ws`。两条都被当作不同节点发布的话，前一条必然连
    不上 —— 实测这类「残缺变体」有 137 个。

    只丢弃**严格更弱**的变体；完整度相同的一律都留（可能真的是不同入口）。
    """
    groups = {}
    for n in nodes:
        groups.setdefault(identity(n), []).append(n)
    kept, dropped = [], 0
    for vs in groups.values():
        if len(vs) == 1:
            kept.append(vs[0])
            continue
        best = max(completeness(n) for n in vs)
        for n in vs:
            if completeness(n) == best:
                kept.append(n)
            else:
                dropped += 1
    return kept, dropped


def node_key(node: dict):
    """去重键：同一台机器上不同 UUID 是不同节点，所以要带上凭证。

    ws/grpc 还要带上传输层身份：CDN 后面的节点经常是同一个 server:port + 同一个
    UUID，只靠 Host/path 区分，否则会把几十个不同的节点合并成一个。
    """
    cred = (str(node.get("uuid") or ""), str(node.get("password") or ""))
    ws = node.get("ws-opts") or {}
    grpc = node.get("grpc-opts") or {}
    if ws:
        transport = "ws|%s|%s" % (((ws.get("headers") or {}).get("Host") or "").lower(),
                                  ws.get("path", ""))
    elif grpc.get("grpc-service-name"):
        transport = "grpc|" + grpc["grpc-service-name"]
    else:
        transport = ""
    return (node["type"], str(node["server"]).lower(), node["port"],
            cred, transport)


# ------------------------------------------------------------------ 主流程

def source_label(url: str) -> str:
    """
    给订阅源的日志标签。

    只取 host 是没有意义的：这些源全都挂在 raw.githubusercontent.com 下，
    日志里会变成 9 行一模一样的 "raw.githubusercontent.com: 解析 N 条"，
    看不出来是哪家源在缩水。所以对 GitHub raw 取 owner/repo，其它取 host。
    """
    host = url.split("//")[-1].split("/")[0]
    parts = [p.split("?")[0] for p in url.split("//")[-1].split("/")[1:] if p]
    if ".githubusercontent.com" in host and len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return f"{host}/{parts[0]}" if parts else host


def main():
    if not os.path.exists(SUBS_FILE):
        print(f"[错误] 找不到 {SUBS_FILE}")
        sys.exit(1)

    with open(SUBS_FILE, encoding="utf-8") as f:
        urls = json.load(f).get("subscriptions", [])

    headers = {"User-Agent": UA}
    collected = []          # [(host, kind, [node, ...]), ...]
    stats = []

    for url in urls:
        host = source_label(url)
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

        good = []
        for node in raw_nodes:
            if not node:
                continue
            node = normalize_node(node)
            if node:
                good.append(node)
        good = sample_source(good, SOURCE_LIMIT)
        collected.append((host, kind, good, len(raw_nodes)))

    # 先按「完整度」清理：同一个节点被多个源写成不同完整度时只留最全的。
    # 必须在去重之前做 —— 去重是先到先得，先到的常常正是那个残缺版本，
    # 之后再比对就没有参照物了（实测这样只能清掉 17 个，而实际有 137 个）。
    flat = [n for _, _, good, _cnt in collected for n in good]
    before_clean = len(flat)
    survivors, degraded = prefer_complete(flat)
    survived_ids = {id(n) for n in survivors}
    if degraded:
        print(f"[清理] 丢弃 {degraded} 个残缺变体"
              f"（同一端点+凭证存在更完整的写法）：{before_clean} → {len(survivors)}")

    seen = set()
    nodes = []
    used_names = {}

    for host, kind, good, parsed_count in collected:
        added = 0
        for node in good:
            if id(node) not in survived_ids:
                continue                      # 上面判定为残缺变体
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

        print(f"[{kind}] {host}: 解析 {parsed_count} 条，新增 {added} 条")
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
