#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
真实测速：起一个 sing-box 实例，逐个节点走真实代理测延迟。

旧版本是假的：
    latency = random.uniform(30, 150)
    speed   = random.uniform(8, 50)
延迟和速度都是随机数，而且 is_port_open() 做的是 TCP 探测——hysteria2 是 UDP
协议，TCP 端口开不开跟能不能用毫无关系。结果是随机淘汰掉真正可用的节点。

现在的做法：
  1. 把节点翻译成 sing-box outbound 写进 probe.json，启动 sing-box
     （clash_api 的 external_controller: 9090）
  2. 并发调用 /proxies/{name}/delay?url=... 让 sing-box 走真实代理去请求
     （这两个端点和 mihomo 的 Clash API 语义一致，失败同样是 503/504）
  3. 首轮失败的节点再补测一轮（并发高时有一批是排队超时，不是真死）
  4. **不按延迟筛选**，只用来排序和打标记（原因见下）
  5. 只输出 v2.txt（base64 的分享链接列表），给 v2rayN + sing-box 内核用

排序还会参考 hk_probe.py 攒下来的香港探测数据（hk_latency.json）。香港离中国
近，那边的 RTT 加 30ms 左右就接近国内用户的真实体验，比 CI 在美国测出来的值
有用得多；但香港测不出墙，所以同样只用于排序，不用于淘汰。

为什么不按延迟筛选（这是本脚本最重要的一条）：

  延迟是「测速机器 → 节点 → 目标站」的往返时间，测速机器在 GitHub Actions
  的 runner 上，在美国机房。它测出来的数字跟国内能不能连上几乎不相关，甚至
  是负相关——便宜的美西 VPS 对美国快、对国内慢；做了国内线路优化的节点反过来。

  2026-10-24 用同一份节点池、同一台国内机器做 TCP 复测，实测：

    原始池 925 个节点          国内可达 407 个 = 44.0%
    CI 按延迟筛出的 199 个      国内可达  31 个 = 15.6%

  也就是说「测速筛选」把可用率从 44% 砍到 15.6%，筛掉的主要是国内能用的节点。
  所以现在只用延迟做两件事：在同类节点里排序、在名字里打 [123ms] 标记。
  节点集合本身不再因为延迟被淘汰。

  唯一还会被排除的是 sing-box 表达不了的节点（xhttp 传输、SSR 协议）——
  它们只是不参与探活、不进 singbox.json，在 clash.yaml 和 v2.txt 里照常发布。

环境变量：
  SINGBOX_BIN        sing-box 可执行文件路径，默认 ./sing-box
  MAX_PROBE          最多探测多少个节点，默认 2500
  PROBE_CONCURRENCY  并发数，默认 32
  MAX_OUTPUT         最终最多输出多少个节点，默认 1500（超出才按协议配额裁剪）
  FEATURED_SIZE      🚀 精选 组放多少个节点，默认 30
  HK_TTL_H           香港延迟超过多少小时不再参与排序，默认 96
  PROBE_URL          测速目标地址
"""

import base64
import hashlib
import json
import os
import queue as queue_mod
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

ALL_NODES_FILE = "all_nodes.yaml"
# CI 每轮把「解析 + 去重后的候选池」提交成 candidates.yaml，服务器优先消费它；
# 没有或过期时再自己跑 fetch_subscriptions.py 生成 all_nodes.yaml。
CANDIDATES_FILE = "candidates.yaml"
# 本轮通过 204/STUN 的端点。hk_probe.py 读它，把香港探测的有限名额优先给这些节点 ——
# 注定不会发布的节点没必要占用探测预算。
PUBLISHABLE_FILE = "publishable.json"
# 每轮写一份「体检卡」，让「自主运行」这件事可以被一眼确认：
# 最后一次成功是什么时候、发了多少、香港覆盖多少。
STATUS_FILE = "status.json"
# 只含「有探测点证据」的节点，给下游（纯净度排序等）一个更靠谱的挑选池。
# 实测带这类标记的节点国内可用率 27.3%，只有本机延迟标记的约 0~4%。
HK_ONLY_FILE = "v2_hk.txt"
# 可选的服务器 IP 地理位置表（谁产都行：本地查询、离线库、别的机器批量查）。
# 格式：{ips: {"1.2.3.4": {"country": "HK", "asn": "AS12345"}}}
# 它只做「排序偏好」和「产出亚洲机房子集」，不作为发布门槛 —— 地理位置是相关性，不是验证。
GEO_FILE = "geo.json"
# CDN / anycast 前置的节点：探测点连到的是就近边缘，不是源站，延迟标记不可信。
# 实测候选池里约三分之一的 IP 属于 Cloudflare 段。
CDN_ORG_HINTS = ("cloudflare", "akamai", "fastly", "cloudfront", "gcore", "bunnycdn")
CDN_ASNS = {"AS13335", "AS209242", "AS16625", "AS54113", "AS19551", "AS60068"}
ASIA_COUNTRIES = {"HK", "JP", "SG", "TW", "KR", "MY", "TH", "VN", "PH", "ID", "MO"}
ASIA_FILE = "v2_asia.txt"
V2_FILE = "v2.txt"
PROBE_FILE = "probe.json"
VERIFY_FILE = "verify.json"
HK_FILE = "hk_latency.json"
HEALTH_FILE = "node_health.json"

SINGBOX_BIN = os.environ.get("SINGBOX_BIN", "./sing-box")
CONTROLLER = "127.0.0.1:9090"
# 可选：所有探活都从这个代理出去（换探测视角，例如链到一个香港/新加坡出口）。
# 留空 = 直接用本机网络。
PROBE_VIA = os.environ.get("PROBE_VIA", "").strip()
# sing-box 自己的日志级别：排查握手失败时设 debug
PROBE_LOG_LEVEL = os.environ.get("PROBE_LOG_LEVEL", "warn")
SINGBOX_LOG = "singbox.log"
VIA_TAG = "via"
API_BASE = f"http://{CONTROLLER}"
TEST_URL = os.environ.get("PROBE_URL", "https://www.gstatic.com/generate_204")
TIMEOUT_MS = int(os.environ.get("PROBE_TIMEOUT_MS", 5000))
MAX_PROBE = int(os.environ.get("MAX_PROBE", 2500))
CONCURRENCY = int(os.environ.get("PROBE_CONCURRENCY", 32))
MAX_OUTPUT = int(os.environ.get("MAX_OUTPUT", 1500))
# 非香港探测点（jp1/sg1/vn1）测出来的延迟加多少偏移再参与排序。
# 它们离国内更远，直接和香港数值混排会低估香港节点的优势。
HK_NONHK_PENALTY_MS = float(os.environ.get("HK_NONHK_PENALTY_MS", 30))
# 香港延迟数据超过这个时长就不参与排序和打标签（和 hk_probe.py 的 TTL 对齐）
HK_TTL_H = float(os.environ.get("HK_TTL_H", 96))
# 「🚀 精选」组的大小：手机上的健康检查只覆盖这么多节点
FEATURED_SIZE = int(os.environ.get("FEATURED_SIZE", 30))
# 204 校验的并发槽位数与单次超时
VERIFY_SLOTS = int(os.environ.get("VERIFY_SLOTS", 24))
VERIFY_TIMEOUT = float(os.environ.get("VERIFY_TIMEOUT", 8))
VERIFY_PORT_BASE = int(os.environ.get("VERIFY_PORT_BASE", 7900))
# UDP 协议（hysteria2 / tuic）用 STUN 校验，对齐 v2rayN 自己的 stun: 探测思路
STUN_SERVERS = [("stun.cloudflare.com", 3478), ("stun.l.google.com", 19302)]
# 这些类型走 UDP 校验，其余走 204 校验
UDP_PROTOCOLS = {"hysteria2", "tuic"}
# 连续多少轮真实探活失败就淘汰。设 1 = 单轮失败即删（激进），默认 2 留一轮容错，
# 因为并发 32 时有相当一批是排队超时/被临时限流，不是真死。
DEAD_ROUNDS = int(os.environ.get("DEAD_ROUNDS", 2))
# 只发布香港测得通的节点（香港是目前唯一与国内可用性正相关的信号，27.3% vs ~0%）。
# 但「有标记」不等于「数据新鲜」：实测发布集里 185 条有标记、其中只有 51 条在 96h TTL 内。
# 覆盖不足时开这个开关会把订阅砍到几十条，比全量还差，所以地板设 200 条 ——
# 它同时充当「覆盖度闸门」：新鲜数据攒够之前，开关不会真的生效。
HK_ONLY = os.environ.get("HK_ONLY", "0") == "1"
HK_ONLY_MIN = int(os.environ.get("HK_ONLY_MIN", 200))

# 淘汰的地板：如果删完会低于这个数，就整批放弃淘汰（宁可脏一点，也不能把订阅砍空）
MIN_KEEP = int(os.environ.get("MIN_KEEP", 150))

# 国内可达率先验，只用来决定输出顺序（哪种协议排在前面）和超出上限时怎么裁剪。
#
# 2026-10-24 从国内对原始池做 TCP 复测（UDP 协议无法用 TCP 探测，单独标注）：
#   vless 359/632 = 56.8%   vmess 32/57 = 56.1%   ss 13/178 = 7.3%
# 裸 ss 没有 TLS 伪装、流量特征明显，是 GFW 打击的重点，国内存活率只有 vless
# 的八分之一。所以输出时 vless/trojan/hysteria2/tuic 排前面，ss/ssr 排最后。
TYPE_WEIGHT = {
    "vless": 1.0,
    "trojan": 1.0,
    "hysteria2": 1.0,
    "tuic": 1.0,
    "vmess": 0.9,
    "ss": 0.25,
    "ssr": 0.15,
}
DEFAULT_WEIGHT = 0.5

# 输出顺序：国内可达率高的协议排前面，用户手动挑节点时先看到的更可能能用
TYPE_ORDER = ["vless", "trojan", "hysteria2", "tuic", "vmess", "ss", "ssr"]

# 不参与淘汰的协议：UDP 系。GitHub runner 对 UDP 出网限制很严，实测 hysteria2
# 44 个节点里只有 1 个能从 CI 探活成功，而这个协议在国内往往是最好用的那一类。
# 探不通是「测不了」，不是「死了」—— 和 sing-box 表达不了同理。
NO_ELIMINATE_TYPES = {"hysteria2", "tuic"}


def type_rank(t):
    try:
        return TYPE_ORDER.index(t)
    except ValueError:
        return len(TYPE_ORDER)


_GEO = {}


def load_geo(path=GEO_FILE):
    """读地理位置表，返回 {ip: country}；没有文件就返回空字典（功能自动降级）。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[警告] 读 {path} 失败，跳过地理位置加权：{e}")
        return {}
    ips = data.get("ips") or {}
    out = {}
    for ip, info in ips.items():
        info = info or {}
        if not info.get("country"):
            continue
        out[str(ip).strip("[]").lower()] = {
            "country": str(info["country"]).upper(),
            "asn": info.get("asn"),
            "org": info.get("org") or "",
        }
    if out:
        print(f"[地理] {path}: {len(out)} 个 IP 有归属地")
    return out


def geo_info(node, geo):
    """取归属地信息。兼容两种写法：{"ip": "HK"} 与 {"ip": {"country": "HK", ...}}。"""
    raw = geo.get(str(node.get("server", "")).strip("[]").lower())
    if isinstance(raw, str):
        return {"country": raw.upper()}
    return raw or {}


def geo_country(node, geo):
    return geo_info(node, geo).get("country")


def is_cdn_fronted(node, geo):
    """这个节点的地址是不是 CDN/anycast 边缘（那样探测延迟就没意义了）。"""
    info = geo_info(node, geo)
    if not info:
        return False
    if str(info.get("asn") or "").upper() in CDN_ASNS:
        return True
    org = str(info.get("org") or "").lower()
    return any(h in org for h in CDN_ORG_HINTS)


def probe_tag(entry):
    """探测点前缀：hk1→HK、jp1→JP、sg1→SG、vn1→VN。"""
    node = str((entry or {}).get("node") or "hk1")
    return node.split(".")[0].strip().upper()[:2] or "HK"


def hk_sort_ms(entry):
    """参与排序的毫秒数：非香港探测点按偏移折算，保持可比性。"""
    ms = float(entry.get("ms") or 0)
    return ms + (0 if probe_tag(entry) == "HK" else HK_NONHK_PENALTY_MS)


def node_key(node):
    return f"{str(node.get('server', '')).lower()}:{node.get('port')}"


# ------------------------------------------------------------------ 健康记录

def load_health():
    """读连续失败计数。节点跨轮次才判死，单轮失败只记一笔。"""
    if not os.path.exists(HEALTH_FILE):
        return {"updated": 0, "nodes": {}}
    try:
        with open(HEALTH_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d.get("nodes"), dict):
            d["nodes"] = {}
        return d
    except Exception as e:
        print(f"[警告] 读 {HEALTH_FILE} 失败，按空记录处理：{e}")
        return {"updated": 0, "nodes": {}}


def save_health(health):
    health["updated"] = int(time.time())
    tmp = HEALTH_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(health, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, HEALTH_FILE)


def hk_alive_keys(hk, ttl_h):
    """香港探测点测得通、且数据没过期的端点（server:port）。"""
    now = time.time()
    return {k for k, v in hk.items()
            if v.get("ms") is not None
            and now - float(v.get("ts") or 0) <= ttl_h * 3600}


def apply_health(nodes, alive_names, probed_names, health, dead_rounds, min_keep,
                 hk_alive=frozenset()):
    """
    更新连续失败计数，并淘汰「连续 dead_rounds 轮都连不上」的节点。

    这条策略和「不按延迟筛选」不冲突，是两回事：

      * 延迟只影响排序，永远不淘汰 —— 因为 CI 在美国测出来的快慢与国内无关
        （实测按延迟筛选把可用率从 44% 砍到 15.6%）。
      * 但**连不上**是另一回事：一个节点从任何角度看都建立不了连接，留着它只会
        让客户端的 url-test 白跑一遍。所以连不上要淘汰，只是给一轮容错。

    三条豁免（都是「我们没法证明它死了」）：

      * sing-box 表达不了的 xhttp/SSR：从没测过；
      * UDP 协议（hysteria2/tuic）：CI 对 UDP 出网限制很严；
      * **香港探测点测得通的**：CI 在美国到不了，不等于亚洲到不了。实测这条能
        救回 113 个被误杀的节点（占当时池子的 30%）。
    """
    now = int(time.time())
    entries = health["nodes"]
    pool_keys = {node_key(n) for n in nodes}
    bumped = set()          # 本轮已经记过失败的端点

    for n in nodes:
        key = node_key(n)
        if n["name"] in alive_names:
            entries.pop(key, None)                    # 通了就清零
        elif (n["name"] in probed_names
              and n.get("type") not in NO_ELIMINATE_TYPES):
            # 同一个 server:port 上可能挂着好几个节点（不同 UUID）。它们共用一条
            # 记录，所以本轮只累加一次 —— 否则「连续 N 轮」会变成「同一轮 N 次」。
            if key in bumped:
                continue
            bumped.add(key)
            e = entries.get(key) or {"fails": 0}
            e["fails"] = int(e.get("fails", 0)) + 1
            e["last"] = now
            entries[key] = e

    kept, dropped, hk_saved = [], [], 0
    for n in nodes:
        exempt = n.get("type") in NO_ELIMINATE_TYPES
        failing = (n["name"] in probed_names and n["name"] not in alive_names
                   and not exempt)
        if failing and int((entries.get(node_key(n)) or {}).get("fails", 0)) >= dead_rounds:
            if node_key(n) in hk_alive:
                hk_saved += 1          # 香港测得通：CI 到不了罢了，留着
                kept.append(n)
            else:
                dropped.append(n)
        else:
            kept.append(n)
    if hk_saved:
        print(f"[豁免] {hk_saved} 个节点 CI 侧连不上、但香港测得通，本轮保留")

    # 地板保护：删完太少了就整批放弃，宁可脏也不能把订阅砍空
    if dropped and len(kept) < min_keep:
        print(f"[警告] 淘汰后只剩 {len(kept)} 个节点（低于 MIN_KEEP={min_keep}），"
              f"本轮放弃淘汰（{len(dropped)} 个节点达到条件但被地板拦下）")
        return list(nodes), [], True, health

    # 只保留本轮池子里出现过的记录（含被淘汰的 —— 淘汰了也要继续记失败次数，
    # 否则下轮它会以「首次失败」的身份被放回来，来回抖）。
    health["nodes"] = {k: v for k, v in entries.items() if k in pool_keys}
    return kept, dropped, False, health


def load_hk_latency():
    """
    读 hk_probe.py 攒下来的香港探测结果。

    这个数据比 CI 的延迟有用得多：香港离中国近，测出来的值加 30ms 左右就接近
    国内用户的实际 RTT。但它**测不出墙**（GFW 在中国出境链路上，从香港探测
    不经过它），所以只用来排序，不用来淘汰。
    """
    if not os.path.exists(HK_FILE):
        return {}
    try:
        with open(HK_FILE, encoding="utf-8") as f:
            data = json.load(f)
        nodes = data.get("nodes")
        return nodes if isinstance(nodes, dict) else {}
    except Exception as e:
        print(f"[警告] 读 {HK_FILE} 失败，忽略：{e}")
        return {}


# ------------------------------------------------------------------ 节点转 URI

def b64url(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().strip("=")


def uri_host(host) -> str:
    """IPv6 必须写成 [::1]，否则 host:port 会被解析器当成多个冒号切错。"""
    h = str(host or "").strip()
    if ":" in h and not h.startswith("["):
        return f"[{h}]"
    return h


def insecure_params(node):
    """
    把 `skip-cert-verify` 还原成 URI 参数。

    两个名字都要写：v2rayN / Xray 家族认 `allowInsecure`，sing-box 和部分客户端
    认 `insecure`。少写一个，客户端就会老老实实做证书校验 —— 而免费节点几乎全是
    自签证书或 CDN 不匹配的证书，表现就是「配置看着齐全、就是连不上」。
    """
    if not node.get("skip-cert-verify"):
        return {}
    return {"allowInsecure": "1", "insecure": "1"}


def node_to_uri(node: dict):
    """把 Clash 节点字典还原成分享链接，供 v2.txt 使用。"""
    t = node.get("type")
    name = urllib.parse.quote(str(node.get("name", "")), safe="")
    host = uri_host(node.get("server"))
    port = node.get("port")

    if t == "ss":
        userinfo = b64url(f"{node.get('cipher')}:{node.get('password')}")
        return f"ss://{userinfo}@{host}:{port}#{name}"

    if t == "vmess":
        net = node.get("network", "tcp")
        ws = node.get("ws-opts") or {}
        grpc = node.get("grpc-opts") or {}
        if net == "grpc":
            host_field, path_field = "", grpc.get("grpc-service-name", "")
        else:
            host_field = (ws.get("headers") or {}).get("Host", "")
            path_field = ws.get("path", "")
        obj = {
            "v": "2",
            "ps": node.get("name", ""),
            "add": host,
            "port": str(port),
            "id": node.get("uuid"),
            "aid": str(node.get("alterId", 0)),
            "scy": node.get("cipher", "auto"),
            "net": net,
            "type": "none",
            "host": host_field,
            "path": path_field,
            "tls": "tls" if node.get("tls") else "",
            "allowInsecure": bool(node.get("skip-cert-verify")),
            "sni": node.get("servername", ""),
        }
        return "vmess://" + base64.b64encode(
            json.dumps(obj, ensure_ascii=False).encode()
        ).decode()

    if t == "vless":
        # VLESS 分享链接按规范必须带 encryption=none，缺了部分客户端直接不认。
        q = {"encryption": "none"}
        if node.get("network"):
            q["type"] = node["network"]
        if node.get("tls"):
            q["security"] = "tls"
        if node.get("servername"):
            q["sni"] = node["servername"]
        if node.get("flow"):
            q["flow"] = node["flow"]
        # reality 少一个 pbk/sid 就完全连不上（客户端会当普通 TLS 握手，必然失败）
        reality = node.get("reality-opts") or {}
        if reality.get("public-key"):
            q["security"] = "reality"
            q["pbk"] = reality["public-key"]
            if reality.get("short-id"):
                q["sid"] = reality["short-id"]
        # fp 是 REALITY/TLS 指纹伪装，缺了会被识别或握手失败
        if node.get("client-fingerprint"):
            q["fp"] = node["client-fingerprint"]
        grpc = node.get("grpc-opts") or {}
        if grpc.get("grpc-service-name"):
            q["serviceName"] = grpc["grpc-service-name"]
        ws = node.get("ws-opts") or {}
        if ws:
            q["path"] = ws.get("path", "/")
            q["host"] = (ws.get("headers") or {}).get("Host", "")
        q.update(insecure_params(node))
        query = urllib.parse.urlencode({k: v for k, v in q.items() if v})
        return f"vless://{node.get('uuid')}@{host}:{port}?{query}#{name}"

    if t == "trojan":
        q = {}
        if node.get("sni"):
            q["sni"] = node["sni"]
        if node.get("network"):
            q["type"] = node["network"]
        ws = node.get("ws-opts") or {}
        if ws:
            q["path"] = ws.get("path", "/")
            q["host"] = (ws.get("headers") or {}).get("Host", "")
        grpc = node.get("grpc-opts") or {}
        if grpc.get("grpc-service-name"):
            q["serviceName"] = grpc["grpc-service-name"]
        q.update(insecure_params(node))
        query = urllib.parse.urlencode(q)
        pwd = urllib.parse.quote(str(node.get("password", "")), safe="")
        return f"trojan://{pwd}@{host}:{port}?{query}#{name}"

    if t == "hysteria2":
        q = {}
        if node.get("sni"):
            q["sni"] = node["sni"]
        if node.get("obfs"):
            q["obfs"] = node["obfs"]
            q["obfs-password"] = node.get("obfs-password", "")
        q.update(insecure_params(node))
        query = urllib.parse.urlencode(q)
        pwd = urllib.parse.quote(str(node.get("password", "")), safe="")
        return f"hysteria2://{pwd}@{host}:{port}?{query}#{name}"

    if t == "tuic":
        q = {}
        if node.get("sni"):
            q["sni"] = node["sni"]
        q.update(insecure_params(node))
        query = urllib.parse.urlencode(q)
        pwd = urllib.parse.quote(str(node.get("password", "")), safe="")
        return f"tuic://{node.get('uuid')}:{pwd}@{host}:{port}?{query}#{name}"

    if t == "ssr":
        # SSR 链接必须整段 base64，这里做一个最小可用的还原
        pwd = b64url(node.get("password", ""))
        main = ":".join([
            str(host), str(port),
            node.get("protocol", "origin"),
            node.get("cipher", "none"),
            node.get("obfs", "plain"),
            pwd,
        ])
        params = urllib.parse.urlencode({
            "remarks": b64url(node.get("name", "")),
            "obfsparam": b64url(node.get("obfs-param", "")),
            "protoparam": b64url(node.get("protocol-param", "")),
        })
        return "ssr://" + b64url(f"{main}/?{params}")

    return None


# ------------------------------------------------------------------ sing-box 控制
#
# 探活引擎是 sing-box。它和 mihomo 在我们要用的两个 Clash 兼容端点上行为一致
# （`/proxies`、`/proxies/{name}/delay`，失败同样返回 503/504），所以 probe_one()
# 一行都不用改。差别只有两处：
#
#   1. sing-box 吃 JSON 不吃 Clash YAML，节点要先翻译成 outbound；
#   2. sing-box 支持的传输和取值比 mihomo 窄（没有 xhttp，不认 SSR，flow 只认
#      xtls-rprx-vision，uTLS 指纹只认白名单）。翻译不了的节点只是**不参与探活**，
#      仍然照常发布 —— 它们在 mihomo/Clash 客户端那边是能用的，没理由因为探活
#      引擎不认识就丢掉。

SUPPORTED_TRANSPORT = {"ws", "grpc", "http", "httpupgrade", "quic"}
# sing-box 只认这些 uTLS 指纹；mihomo 接受任意字符串（例如 unsafe）
FP_ALLOWED = {"chrome", "firefox", "safari", "ios", "android", "edge", "360",
              "qq", "random", "randomized"}


def _transport(node):
    net = str(node.get("network") or "tcp").lower()
    if net in ("tcp", "raw", ""):
        return None, None
    if net == "ws":
        ws = node.get("ws-opts") or {}
        t = {"type": "ws", "path": ws.get("path") or "/"}
        host = (ws.get("headers") or {}).get("Host")
        if host:
            t["headers"] = {"Host": host}
        return t, None
    if net == "grpc":
        return {"type": "grpc",
                "service_name": (node.get("grpc-opts") or {}).get(
                    "grpc-service-name", "")}, None
    if net in SUPPORTED_TRANSPORT:
        return {"type": net}, None
    return None, f"传输 {net}"


def _tls(node):
    reality = node.get("reality-opts") or {}
    if not (node.get("tls") or reality):
        return None
    tls = {"enabled": True}
    sni = node.get("servername") or node.get("sni") or node.get("server")
    if sni:
        tls["server_name"] = str(sni)
    if node.get("skip-cert-verify"):
        tls["insecure"] = True
    fp = str(node.get("client-fingerprint") or "").lower()
    if fp:
        tls["utls"] = {"enabled": True,
                       "fingerprint": fp if fp in FP_ALLOWED else "chrome"}
    if reality.get("public-key"):
        # REALITY 在 sing-box 里强制要求 uTLS
        tls.setdefault("utls", {"enabled": True, "fingerprint": "chrome"})
        tls["reality"] = {"enabled": True, "public_key": reality["public-key"],
                          "short_id": reality.get("short-id") or ""}
    return tls


def node_to_outbound(node):
    """把 Clash 节点字典翻译成 sing-box outbound；翻不了返回 (None, 原因)。"""
    ntype = node.get("type")
    transport, reason = _transport(node)
    if reason:
        return None, reason
    if ntype == "ssr":
        return None, "SSR"
    out = {"tag": node["name"], "server": str(node["server"]).strip("[]"),
           "server_port": int(node["port"])}
    if ntype == "vless":
        out.update(type="vless", uuid=node["uuid"])
        flow = str(node.get("flow") or "")
        if flow:
            # mihomo 认 xtls-rprx-vision-udp443，sing-box 只认 xtls-rprx-vision
            out["flow"] = flow.replace("-udp443", "")
    elif ntype == "vmess":
        out.update(type="vmess", uuid=node["uuid"],
                   security=node.get("cipher") or "auto",
                   alter_id=int(node.get("alterId") or 0))
    elif ntype == "trojan":
        out.update(type="trojan", password=node["password"])
    elif ntype == "ss":
        out.update(type="shadowsocks", method=node["cipher"],
                   password=node["password"])
    elif ntype == "hysteria2":
        out.update(type="hysteria2", password=node["password"])
        if node.get("obfs"):
            out["obfs"] = {"type": node["obfs"],
                           "password": node.get("obfs-password") or ""}
    elif ntype == "tuic":
        out.update(type="tuic", uuid=node["uuid"], password=node["password"])
    else:
        return None, f"类型 {ntype}"
    if transport:
        out["transport"] = transport
    tls = _tls(node)
    if tls:
        out["tls"] = tls
    elif ntype in ("trojan", "hysteria2", "tuic"):
        out["tls"] = {"enabled": True}
    return out, None



def via_outbound():
    """把 PROBE_VIA 解析成 sing-box 出站；没配就返回 None。"""
    if not PROBE_VIA:
        return None
    raw = PROBE_VIA if "://" in PROBE_VIA else "socks5://" + PROBE_VIA
    u = urllib.parse.urlparse(raw)
    scheme = (u.scheme or "socks5").lower()
    typ = {"socks5": "socks", "socks5h": "socks", "socks": "socks",
           "http": "http", "https": "http"}.get(scheme)
    if not typ or not u.hostname:
        print(f"[警告] PROBE_VIA 无法解析: {PROBE_VIA}")
        return None
    ob = {"type": typ, "tag": VIA_TAG, "server": u.hostname,
          "server_port": int(u.port or (1080 if typ == "socks" else 8080))}
    if u.username:
        ob["username"] = urllib.parse.unquote(u.username)
    if u.password:
        ob["password"] = urllib.parse.unquote(u.password)
    if scheme == "https":
        ob["tls"] = {"enabled": True}
    return ob


def with_detour(outbounds, via):
    """给每个节点出站挂 detour —— 连节点这一步就走代理，从而换掉探测视角。"""
    if not via:
        return [dict(o) for o in outbounds]
    out = []
    for o in outbounds:
        o = dict(o)
        o["detour"] = via["tag"]
        out.append(o)
    return out



def _free_ports(count, start):
    """
    找一段连续可用的本地端口。

    为什么必须动态挑：探活实例要绑 127.0.0.1，而服务器上常常已经有客户端在跑
    （mihomo/clash 的混合端口就是 7890，正好和默认值撞上）。撞上就是
    "bind: address already in use"，整轮直接失败。
    """
    import socket
    for base in range(start, start + 200):
        socks = []
        ok = True
        for i in range(count):
            sk = socket.socket()
            try:
                sk.bind(("127.0.0.1", base + i))
                socks.append(sk)
            except OSError:
                ok = False
                break
        for sk in socks:
            sk.close()
        if ok:
            return base
    raise RuntimeError(f"找不到连续的 {count} 个空闲端口（从 {start} 起）")


_PROBE_PORT_CACHE = {}
_VERIFY_BASE_CACHE = {}


def probe_listen_port():
    """探活实例的本地入站端口（避开被占用的）。"""
    if "p" not in _PROBE_PORT_CACHE:
        _PROBE_PORT_CACHE["p"] = _free_ports(
            1, int(os.environ.get("PROBE_LISTEN_PORT", 7890)))
    return _PROBE_PORT_CACHE["p"]


def verify_port_base(slots):
    """校验槽位用的连续端口段基址（同一进程内只算一次，调用方必须共用）。"""
    if slots not in _VERIFY_BASE_CACHE:
        _VERIFY_BASE_CACHE[slots] = _free_ports(slots, VERIFY_PORT_BASE)
    return _VERIFY_BASE_CACHE[slots]


def write_probe_config(outbounds, path=PROBE_FILE):
    via = via_outbound()
    outs = with_detour(outbounds, via)
    if via:
        outs.append(via)
    config = {
        "log": {"level": PROBE_LOG_LEVEL},
        "inbounds": [{"type": "mixed", "tag": "in", "listen": "127.0.0.1",
                      "listen_port": probe_listen_port()}],
        "outbounds": outs + [{"type": "direct", "tag": "direct"}],
        "route": {"final": "direct"},
        "experimental": {"clash_api": {"external_controller": CONTROLLER}},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False)


def singbox_available() -> bool:
    if not os.path.exists(SINGBOX_BIN):
        return False
    try:
        subprocess.run([SINGBOX_BIN, "version"], capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def check_outbounds(outbounds, path=PROBE_FILE):
    write_probe_config(outbounds, path)
    r = subprocess.run([SINGBOX_BIN, "check", "-c", path],
                       capture_output=True, text=True, timeout=120)
    return r.returncode == 0, ((r.stderr or "") + (r.stdout or "")).strip()


def validate_outbounds(outbounds):
    """
    用 `sing-box check` 筛掉它加载不了的 outbound。

    分批校验（100 个一批），只在某批失败时才逐条定位 —— 1269 个节点正常情况
    十几秒跑完。被筛掉的节点只是不参与探活，仍然照常发布：它们可能只是 sing-box
    不支持的写法，在 mihomo/Clash 客户端那边是好的。
    """
    good, bad = [], []
    for i in range(0, len(outbounds), 100):
        part = outbounds[i:i + 100]
        ok, _ = check_outbounds(part)
        if ok:
            good += part
            continue
        for ob in part:
            ok, msg = check_outbounds([ob])
            if ok:
                good.append(ob)
            else:
                bad.append((ob["tag"], (msg.splitlines() or [""])[-1][:120]))
    return good, bad


def start_singbox():
    """起探活实例；stderr 落盘到 singbox.log，排查时不用靠猜。"""
    try:
        os.makedirs("logs", exist_ok=True)
        err = open(SINGBOX_LOG, "w", encoding="utf-8")
    except Exception:
        err = subprocess.DEVNULL
    proc = subprocess.Popen(
        [SINGBOX_BIN, "run", "-c", PROBE_FILE],
        stdout=subprocess.DEVNULL, stderr=err, text=True,
    )
    for _ in range(40):
        time.sleep(0.5)
        if proc.poll() is not None:
            err = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(f"sing-box 启动失败: {err[:500]}")
        try:
            with urllib.request.urlopen(f"{API_BASE}/version", timeout=2) as resp:
                if resp.status == 200:
                    return proc
        except Exception:
            continue
    proc.kill()
    raise RuntimeError("sing-box 的 clash_api 30 秒内未就绪")


def build_verify_config(nodes, slots, port_base=None):
    """
    构造「多槽位」校验配置：每个槽位一个 mixed inbound + 一个 selector。

    校验必须让请求真正走指定节点，而 sing-box 的路由是全局的。做法是开 N 组
    互相独立的 inbound/selector，用 route.rules 把 in-i 固定指到 sel-i，
    每个 worker 只改自己那个 selector —— 这样 N 个节点可以并发校验，互不干扰。
    """
    obs = [o for o in (node_to_outbound(n)[0] for n in nodes) if o]
    if not obs:
        return None
    if port_base is None:
        port_base = verify_port_base(slots)
    via = via_outbound()
    obs = with_detour(obs, via)
    tags = [o["tag"] for o in obs]
    return {
        "log": {"level": "error"},
        "inbounds": [{"type": "mixed", "tag": f"in-{i}", "listen": "127.0.0.1",
                      "listen_port": port_base + i} for i in range(slots)],
        "outbounds": obs + [
            {"type": "selector", "tag": f"sel-{i}", "outbounds": tags,
             "default": tags[0]} for i in range(slots)
        ] + ([via] if via else []) + [{"type": "direct", "tag": "direct"}],
        "route": {
            "rules": [{"inbound": [f"in-{i}"], "outbound": f"sel-{i}"}
                      for i in range(slots)],
            "final": "direct",
        },
        "experimental": {"clash_api": {"external_controller": CONTROLLER}},
    }


def select_proxy(selector, name):
    """PUT /proxies/{selector} 切换该槽位使用的节点。"""
    body = json.dumps({"name": name}).encode()
    req = urllib.request.Request(
        f"{API_BASE}/proxies/{urllib.parse.quote(selector, safe='')}",
        data=body, method="PUT",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5):
            return True
    except Exception:
        return False


def http_status_via(port, timeout):
    """经本地代理端口发一次真实 HTTPS 请求，返回状态码（失败返回 None）。"""
    proxy = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    try:
        with opener.open(TEST_URL, timeout=timeout) as resp:
            return getattr(resp, "status", resp.getcode())
    except Exception:
        return None


def verify_204(nodes, slots=VERIFY_SLOTS, timeout=VERIFY_TIMEOUT):
    """
    逐节点验证「经该节点发真实 HTTP 请求，且收到 204」。

    为什么需要这一步：sing-box 的 /proxies/{name}/delay 只保证「请求完成了」，
    **不校验状态码** —— 实测同一个节点打 generate_204 和打一个必然 404 的路径
    都会返回延迟。所以延迟通过只能说明隧道通了，不能说明出口是好的（被劫持、
    被墙插页、只放行特定域名的节点都会通过 delay 但根本不能用）。
    """
    if not nodes:
        return set(), {}
    port_base = verify_port_base(slots)
    cfg = build_verify_config(nodes, slots, port_base)
    if cfg is None:
        return set(), {}
    with open(VERIFY_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)

    proc = subprocess.Popen([SINGBOX_BIN, "run", "-c", VERIFY_FILE],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    verified, statuses = set(), {}
    try:
        for _ in range(40):
            time.sleep(0.5)
            if proc.poll() is not None:
                print(f"[警告] 校验实例启动失败: {(proc.stderr.read() or '')[:300]}")
                return set(), {}
            try:
                with urllib.request.urlopen(f"{API_BASE}/version", timeout=2):
                    break
            except Exception:
                continue

        queue = queue_mod.Queue()
        for n in nodes:
            queue.put(n)
        lock = threading.Lock()

        def worker(idx):
            selector = f"sel-{idx}"
            port = port_base + idx
            while True:
                try:
                    n = queue.get_nowait()
                except queue_mod.Empty:
                    return
                if not select_proxy(selector, n["name"]):
                    continue
                code = http_status_via(port, timeout)
                with lock:
                    statuses[code] = statuses.get(code, 0) + 1
                    if code == 204:
                        verified.add(n["name"])

        threads = [threading.Thread(target=worker, args=(i,), daemon=True)
                   for i in range(slots)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return verified, statuses


def _socks5_udp_associate(port, timeout):
    """在本地 mixed 入站上开一条 SOCKS5 UDP ASSOCIATE，返回 (tcp_sock, 中继地址)。"""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.settimeout(timeout)
    try:
        s.sendall(b"\x05\x01\x00")
        if s.recv(2) != b"\x05\x00":
            s.close()
            return None
        s.sendall(b"\x05\x03\x00\x01" + b"\x00" * 4 + b"\x00\x00")
        resp = s.recv(10)
        if len(resp) < 10 or resp[1] != 0:
            s.close()
            return None
        ip = socket.inet_ntoa(resp[4:8])
        if ip == "0.0.0.0":
            ip = "127.0.0.1"
        return s, (ip, struct.unpack("!H", resp[8:10])[0])
    except Exception:
        try:
            s.close()
        except Exception:
            pass
        return None


def stun_via(port, timeout, servers=STUN_SERVERS):
    """
    经本地代理端口发一个 UDP STUN binding 请求，收到 binding success 才算通。

    为什么 UDP 协议要单独测：hysteria2 / tuic 是 UDP 协议，而 GitHub runner 的
    UDP 出网限制很严；用 HTTPS(204) 去测它们，测的是「能不能跑 TCP」，不是
    「能不能跑它自己的协议」。v2rayN 自己的测速类型里也有 stun:/dns: 这类
    UDP 探测，这里对齐同样的思路。
    """
    for host, port_num in servers:
        try:
            ip = socket.gethostbyname(host)
        except Exception:
            continue
        got = _socks5_udp_associate(port, timeout)
        if not got:
            continue
        tcp, relay = got
        udp = None
        try:
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.settimeout(timeout)
            tid = os.urandom(12)
            stun = b"\x00\x01\x00\x00\x21\x12\xa4\x42" + tid
            pkt = (b"\x00\x00\x00\x01" + socket.inet_aton(ip)
                   + struct.pack("!H", port_num) + stun)
            udp.sendto(pkt, relay)
            data, _ = udp.recvfrom(1500)
            if len(data) < 10:
                continue
            atyp = data[3]
            hlen = 4 + (4 if atyp == 1 else 16 if atyp == 4 else 1 + data[4]) + 2
            payload = data[hlen:]
            # 0x0101 = STUN Binding Success Response
            if len(payload) >= 20 and payload[0:2] == b"\x01\x01":
                return True
        except Exception:
            pass
        finally:
            if udp:
                udp.close()
            try:
                tcp.close()
            except Exception:
                pass
    return False


def verify_udp(nodes, slots=VERIFY_SLOTS, timeout=VERIFY_TIMEOUT):
    """UDP 协议（hysteria2 / tuic）的校验：并发槽位 + STUN，与 verify_204 同构。"""
    if not nodes:
        return set()
    port_base = verify_port_base(slots)
    cfg = build_verify_config(nodes, slots, port_base)
    if cfg is None:
        return set()
    with open(VERIFY_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)

    proc = subprocess.Popen([SINGBOX_BIN, "run", "-c", VERIFY_FILE],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    ok = set()
    try:
        for _ in range(40):
            time.sleep(0.5)
            if proc.poll() is not None:
                print(f"[警告] UDP 校验实例启动失败: {(proc.stderr.read() or '')[:300]}")
                return set()
            try:
                with urllib.request.urlopen(f"{API_BASE}/version", timeout=2):
                    break
            except Exception:
                continue

        queue = queue_mod.Queue()
        for n in nodes:
            queue.put(n)
        lock = threading.Lock()

        def worker(idx):
            selector = f"sel-{idx}"
            port = port_base + idx
            while True:
                try:
                    n = queue.get_nowait()
                except queue_mod.Empty:
                    return
                if not select_proxy(selector, n["name"]):
                    continue
                if stun_via(port, timeout):
                    with lock:
                        ok.add(n["name"])

        threads = [threading.Thread(target=worker, args=(i,), daemon=True)
                   for i in range(slots)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return ok


def probe_one(name: str):
    """
    让代理核心走指定节点请求一次，返回 (延迟毫秒, 失败原因)。

    失败原因必须留住 —— HTTP 状态码本身就是判据：
      404  → 探活配置里没有这个节点（我们自己配置/命名的问题，不是节点问题）
      503  → 拨号或握手失败（节点或链路）
      504  → 超时（给的时间太短，或链路慢/不通）
    以前一律吞成 None，现场只能看到「0/N」，没法区分是哪一类。
    """
    q = urllib.parse.urlencode({"timeout": TIMEOUT_MS, "url": TEST_URL})
    url = f"{API_BASE}/proxies/{urllib.parse.quote(str(name), safe='')}/delay?{q}"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_MS / 1000 + 5) as resp:
            data = json.loads(resp.read().decode())
        delay = data.get("delay")
        if isinstance(delay, int) and delay > 0:
            return delay, None
        return None, "empty"
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, type(e).__name__


def probe_all(nodes, label="测速"):
    """并发探测一批节点，返回 [(delay, node), ...]。"""
    out = []
    if not nodes:
        return out
    reasons = {}
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {pool.submit(probe_one, n["name"]): n for n in nodes}
        done = 0
        for fut in as_completed(futures):
            node = futures[fut]
            delay, why = fut.result()
            done += 1
            if delay:
                out.append((delay, node))
            elif why:
                reasons[why] = reasons.get(why, 0) + 1
            if done % 50 == 0:
                print(f"  {label}进度 {done}/{len(nodes)}，可用 {len(out)}")
    if reasons:
        top = sorted(reasons.items(), key=lambda kv: -kv[1])
        print(f"  {label}失败原因分布: " + ", ".join(f"{k}×{v}" for k, v in top[:6]))
    return out


def select_nodes(nodes, delays, hk, max_output):
    """
    选取并排序，不按延迟淘汰。

    nodes     : 全部节点（可能含少量 sing-box 表达不了、因而没探活的节点）
    delays    : {id(node): 延迟}，CI（美国）探活的节点
    hk        : {"server:port": {"ms": 毫秒或 None}}，香港探测点的结果
    max_output: 输出上限。节点数没超上限时全部输出，只有超了才按协议配额裁剪。

    排序规则：按协议分块，国内可达率高的协议排前面（vless/trojan/hysteria2/tuic
    → vmess → ss → ssr）；块内依次是：

      1. 香港测得通的（按香港延迟升序）—— 最接近国内真实体验
      2. 香港没测过、但 CI 探活的（按 CI 延迟升序）
      3. 两边都没数据的
      4. 香港明确连不上的 —— 大概率是死节点，垫底

    为什么以香港数据优先：香港离中国近，它测出的 RTT 加 30ms 左右就是国内用户
    的实际体验；CI 在美国，测出来的值基本只能说明「节点活着」。但它测不出墙，
    所以只影响排序，不影响是否发布。
    """
    by_type = {}
    for n in nodes:
        by_type.setdefault(n.get("type", "?"), []).append(n)

    now = time.time()

    def hk_fresh(n):
        """
        取这个节点的香港数据，超过 TTL 的当作没有。

        缓存里的条目可能很旧（按最久未测优先轮转，一个节点可能要几天才轮到一次），
        旧的 8ms 和今天实测的 30ms 放一起排序，前者会被无条件排到最前，标签还写着
        [HK 8ms] —— 那是在骗自己。过期就当没数据。
        """
        e = hk.get(node_key(n))
        if not e:
            return None
        if now - float(e.get("ts") or 0) > HK_TTL_H * 3600:
            return None
        return e

    def geo_rank(n):
        """机房在亚洲的排前面（0），未知/其它为 1。没有 geo.json 时恒为 1（不改变原顺序）。"""
        c = geo_country(n, _GEO)
        if not c:
            return 1
        return 0 if c in ASIA_COUNTRIES else 1

    def sort_key(n):
        e = None if is_cdn_fronted(n, _GEO) else hk_fresh(n)
        if e:
            ms = e.get("ms")
            if ms is not None:
                return (0, geo_rank(n), hk_sort_ms(e))
            return (3, 1, 0)                  # 探测点连不上 → 垫底
        d = delays.get(id(n))
        if d:
            return (1, geo_rank(n), d)
        return (2, geo_rank(n), 0)

    for t, lst in by_type.items():
        lst.sort(key=sort_key)

    order = sorted(by_type, key=type_rank)

    if len(nodes) <= max_output:
        chosen = [(t, n) for t in order for n in by_type[t]]
    else:
        # 超上限时按协议加权配额裁剪，避免 ss 这种数量多但国内存活率低的协议
        # 把名额吃光
        weight_sum = sum(TYPE_WEIGHT.get(t, DEFAULT_WEIGHT) for t in order)
        quota = {t: max(1, round(max_output * TYPE_WEIGHT.get(t, DEFAULT_WEIGHT)
                                 / weight_sum))
                 for t in order}
        chosen, idx = [], {t: 0 for t in order}
        while len(chosen) < max_output:
            progressed = False
            for t in order:
                if len(chosen) >= max_output:
                    break
                if idx[t] < quota[t] and idx[t] < len(by_type[t]):
                    chosen.append((t, by_type[t][idx[t]]))
                    idx[t] += 1
                    progressed = True
            if not progressed:
                break
        # 配额没取满（某类型节点数不够）的，剩余名额按协议顺序补齐
        while len(chosen) < max_output:
            progressed = False
            for t in order:
                if len(chosen) >= max_output:
                    break
                if idx[t] < len(by_type[t]):
                    chosen.append((t, by_type[t][idx[t]]))
                    idx[t] += 1
                    progressed = True
            if not progressed:
                break

    out = []
    for _, node in chosen:
        cdn = is_cdn_fronted(node, _GEO)
        e = None if cdn else hk_fresh(node)
        d = delays.get(id(node))
        node = dict(node)
        if cdn:
            # CDN/anycast 前置：探测点连到的是就近边缘，延迟不是到源站的路径，不标数值
            node["name"] = f"{node['name']} [CDN]"
        elif e and e.get("ms") is not None:
            node["name"] = f"{node['name']} [{probe_tag(e)} {e['ms']:g}ms]"
        elif e:
            node["name"] = f"{node['name']} [{probe_tag(e)} 超时]"
        elif d:
            # 没有香港数据时退回 CI 的测量值，但必须标明是美国测的
            node["name"] = f"{node['name']} [{d}ms]"
        out.append(node)
    return out


# ------------------------------------------------------------------ 输出

def write_outputs(nodes):
    """
    只写一份产物：`v2.txt`（base64 的分享链接列表）。

    目标是 v2rayN + sing-box 内核，所以不再产出 clash.yaml / singbox.json ——
    sing-box 内核认的是分享链接，v2rayN 负责把链接转成它自己的配置。反过来，
    sing-box 跑不了的写法（xhttp 传输、SSR 协议）在这条链路上也用不了，那些节点
    在 main() 里就被剔除了，不会进入 v2.txt。
    """
    uris = [u for u in (node_to_uri(n) for n in nodes) if u]
    with open(V2_FILE, "w", encoding="utf-8") as f:
        f.write(base64.b64encode("\n".join(uris).encode()).decode())
    print(f"已写出 {V2_FILE}（{len(uris)} 条 URI）")
    return len(uris)


# ------------------------------------------------------------------ 主流程

def main():
    src = CANDIDATES_FILE if os.path.exists(CANDIDATES_FILE) else ALL_NODES_FILE
    if not os.path.exists(src):
        print(f"[错误] 找不到 {CANDIDATES_FILE} 或 {ALL_NODES_FILE}", file=sys.stderr)
        sys.exit(1)
    print(f"输入: {src}")

    with open(src, encoding="utf-8") as f:
        nodes = (yaml.safe_load(f) or {}).get("proxies") or []
    print(f"载入 {len(nodes)} 个节点")

    if not nodes:
        print("[错误] 没有节点可用", file=sys.stderr)
        sys.exit(1)

    # 超过上限时按哈希取样，而不是砍掉尾部。池子是按订阅源顺序拼起来的，
    # 直接切片等于只测前几个源，后面的源永远拿不到延迟数据（hk_probe.py 里
    # 同样的理由）。
    if len(nodes) > MAX_PROBE:
        nodes = sorted(nodes, key=lambda n: hashlib.md5(
            f"{str(n.get('server', '')).lower()}:{n.get('port')}".encode()
        ).hexdigest())[:MAX_PROBE]
        print(f"[注意] 节点数超过 MAX_PROBE={MAX_PROBE}，本轮按哈希取样 {len(nodes)} 个")

    # sing-box 不可用时降级：不测速，全部保留，但香港排序数据还能用
    if not singbox_available():
        print(f"[警告] 找不到 sing-box（{SINGBOX_BIN}），跳过测速，保留全部节点")
        write_outputs(select_nodes(nodes, {}, load_hk_latency(), MAX_OUTPUT))
        return

    # 翻译成 sing-box outbound。翻不了的（xhttp 传输、SSR 协议）**直接剔除**：
    # 产物只给 v2rayN + sing-box 内核用，这些写法在那条链路上根本跑不起来，
    # 留在订阅里只会变成「导入后连不上」的噪音。
    representable, dropped_repr = [], {}
    for n in nodes:
        ob, reason = node_to_outbound(n)
        if ob:
            representable.append(n)
        else:
            dropped_repr[reason] = dropped_repr.get(reason, 0) + 1
    if dropped_repr:
        print(f"[剔除] {sum(dropped_repr.values())} 个节点 sing-box 跑不了："
              f"{dropped_repr}")
    nodes = representable
    by_name = {n["name"]: n for n in nodes}
    outbounds = [node_to_outbound(n)[0] for n in nodes]

    good, bad = validate_outbounds(outbounds)
    if bad:
        # 从池里彻底剔除：这类节点是「translate 得出来但内核加载不了」，典型原因是
        # 加密方式非法（实测 cipher 写成字面量 "ss"）或 2022 密钥不是合法 base64。
        # 一个这样的节点就能让客户端整份配置加载失败，不能留。
        bad_tags = {tag for tag, _ in bad}
        print(f"[校验] {len(bad)} 个节点 sing-box 加载不了，从池中剔除")
        for tag, msg in bad[:5]:
            print(f"   {tag} → {msg}")
        nodes = [n for n in nodes if n["name"] not in bad_tags]
        by_name = {n["name"]: n for n in nodes}
    if not good:
        print("[错误] 没有任何节点能被 sing-box 加载，本轮不产出", file=sys.stderr)
        sys.exit(1)

    probe_nodes = [by_name[o["tag"]] for o in good if o["tag"] in by_name]

    write_probe_config(good)

    print(f"启动 sing-box，开始真实测速（{len(probe_nodes)} 个节点，并发 {CONCURRENCY}）")
    proc = start_singbox()
    try:
        results = probe_all(probe_nodes)
        # 首轮失败的再补一轮：并发 32 的时候总有一批是核心排队超时，
        # 不是节点真的连不上。补测一次成本很低，能捞回来不少。
        alive = {n["name"] for _, n in results}
        retry = [n for n in probe_nodes if n["name"] not in alive]
        if retry:
            print(f"补测首轮失败的 {len(retry)} 个节点…")
            results += probe_all(retry, label="补测")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    print(f"延迟探活：{len(results)}/{len(probe_nodes)} 个节点隧道能通")

    # 一个都不通，几乎不可能是「节点全死了」，而是这台机器的出网被拦了：
    # 实测同一批节点在美国机房 725/765 能过 204，所以 0 只说明探测路径有问题。
    # 直接失败、把排查方向打出来，别让它静默产出空订阅。
    if not results:
        print("[错误] 所有节点都没通过延迟探活 —— 先怀疑探测路径，而不是节点：", file=sys.stderr)
        print("   1) python3 doctor.py 看是时间/证书/出网/到节点 哪一层断",
              file=sys.stderr)
        print("   2) 如果这台机器直连被拦（云厂商限制或中间设备干扰），"
              "用 PROBE_VIA 换出口：", file=sys.stderr)
        print("      PROBE_VIA=socks5://127.0.0.1:7890 ./run.sh", file=sys.stderr)
        print("   3) 先小样本验证链路再全量：MAX_PROBE=50 ./run.sh", file=sys.stderr)
        print("   4) 如果这台机器有「按连接询问」的防火墙（Little Snitch / LuLu 等），"
              "给 sing-box 二进制建一条「任意目标/任意端口」的永久允许规则 ——"
              "否则每条连接都会卡在待审核队列里", file=sys.stderr)
        print(f"   5) 抓 sing-box 日志：PROBE_LOG_LEVEL=debug ./run.sh"
              f"（会写到 {SINGBOX_LOG}）", file=sys.stderr)
        try:
            tail = open(SINGBOX_LOG, encoding="utf-8", errors="replace").read().splitlines()[-20:]
        except Exception:
            tail = []
        if tail:
            print(f"\n[sing-box 最后 {len(tail)} 行]", file=sys.stderr)
            for line in tail:
                print("   " + line[:200], file=sys.stderr)
        sys.exit(1)

    # 关键一步：延迟通过只说明隧道通了，不说明出口是好的。这一步经每个节点发一次
    # 真实 HTTP 请求，要求状态码必须是 204 才认为可用（详见 verify_204 的注释）。
    by_name2 = {n["name"]: n for n in probe_nodes}
    delay_alive = [n for _, n in results if n["name"] in by_name2]
    udp_nodes = [n for n in delay_alive if n["type"] in UDP_PROTOCOLS]
    tcp_nodes = [n for n in delay_alive if n["type"] not in UDP_PROTOCOLS]

    # TCP 协议：经节点发真实 HTTPS 请求，必须 204
    verified, statuses = verify_204(tcp_nodes)
    print(f"204 校验（TCP 协议）：{len(verified)}/{len(tcp_nodes)} 通过；"
          f"状态码分布 {dict(sorted(statuses.items(), key=lambda x: str(x[0])))}")

    # UDP 协议：经节点发 STUN binding 请求，必须收到 binding success
    udp_ok = verify_udp(udp_nodes)
    print(f"STUN 校验（UDP 协议）：{len(udp_ok)}/{len(udp_nodes)} 通过")

    verified |= udp_ok

    # 落一份「本轮到过协议级校验」的端点清单，供下一轮 hk_probe.py 优先探测
    try:
        keys = sorted({node_key(n) for n in probe_nodes if n["name"] in verified})
        with open(PUBLISHABLE_FILE, "w", encoding="utf-8") as f:
            json.dump({"updated": int(time.time()), "keys": keys},
                      f, ensure_ascii=False, indent=1)
        print(f"已写出 {PUBLISHABLE_FILE}（{len(keys)} 个端点，供香港探测优先使用）")
    except Exception as e:
        print(f"[警告] 写 {PUBLISHABLE_FILE} 失败: {e}")

    results = [(d, n) for d, n in results if n["name"] in verified]
    print(f"探活完成：{len(results)}/{len(probe_nodes)} 个节点通过协议级校验")

    # 淘汰「连续 DEAD_ROUNDS 轮都连不上」的节点。延迟不参与淘汰，连接失败参与。
    alive = verified
    probed = {n["name"] for n in probe_nodes}
    health = load_health()
    hk = load_hk_latency()
    global _GEO
    _GEO = load_geo()
    before = len(nodes)
    nodes, dropped, floor_blocked, health = apply_health(
        nodes, alive, probed, health, DEAD_ROUNDS, MIN_KEEP,
        hk_alive=hk_alive_keys(hk, HK_TTL_H))
    save_health(health)
    if dropped:
        dist = {}
        for n in dropped:
            dist[n["type"]] = dist.get(n["type"], 0) + 1
        print(f"淘汰 {len(dropped)} 个连续 {DEAD_ROUNDS} 轮连不上的节点："
              f"{dist}（池 {before} → {len(nodes)}）")
    elif floor_blocked:
        print(f"本轮仍未淘汰（地板 MIN_KEEP={MIN_KEEP} 拦下了），池 {len(nodes)} 个")
    else:
        print(f"本轮没有节点达到淘汰条件（连续 {DEAD_ROUNDS} 轮失败），"
              f"池 {len(nodes)} 个")

    # 只发布通过 204 校验的节点。香港豁免的节点保留在池里（下一轮可能被 CI 探到），
    # 但本轮没有 204 证据，所以不进订阅 —— 宁可少发，不发没验证过的。
    publishable = [n for n in nodes if n["name"] in verified]

    # 只发「香港测得通」的节点：把没有亚洲侧证据的去掉。覆盖不足时自动回退，
    # 避免把订阅砍成个位数 —— 那还不如发一份宽一点但带标记的。
    if HK_ONLY:
        hk_alive = hk_alive_keys(hk, HK_TTL_H)
        kept = [n for n in publishable if node_key(n) in hk_alive]
        if len(kept) >= HK_ONLY_MIN:
            print(f"[HK_ONLY] 只保留香港测得通的节点：{len(publishable)} → {len(kept)}")
            publishable = kept
        else:
            print(f"[HK_ONLY] 香港覆盖不足（仅 {len(kept)} 条，低于 HK_ONLY_MIN="
                  f"{HK_ONLY_MIN}），本轮回退为全量发布")
    print(f"发布 {len(publishable)} 个（池内 {len(nodes)} 个，其余本轮未通过 204 校验）")

    # 额外产出一份「有探测点证据」的子集：下游按纯净度挑优选时，在这个池子里挑
    # 才不会挑到「干净但连不上」的节点。
    try:
        hk_keys = hk_alive_keys(hk, HK_TTL_H)
        hk_subset = [n for n in publishable if node_key(n) in hk_keys]
        uris = [u for u in (node_to_uri(n) for n in hk_subset) if u]
        with open(HK_ONLY_FILE, "w", encoding="utf-8") as f:
            f.write(base64.b64encode("\n".join(uris).encode()).decode())
        print(f"已写出 {HK_ONLY_FILE}（{len(uris)} 条带探测点证据）")
    except Exception as e:
        print(f"[警告] 写 {HK_ONLY_FILE} 失败: {e}")

    # 再产一份「服务器机房在亚洲」的子集：这是免费、不限量的弱信号，
    # 用来给下游挑优选时扩大候选池（池子不再只有带探测点证据的那几十条）。
    try:
        if _GEO:
            asia = [n for n in publishable if geo_country(n, _GEO) in ASIA_COUNTRIES]
            uris = [u for u in (node_to_uri(n) for n in asia) if u]
            with open(ASIA_FILE, "w", encoding="utf-8") as f:
                f.write(base64.b64encode("\n".join(uris).encode()).decode())
            print(f"已写出 {ASIA_FILE}（{len(uris)} 条，机房在亚洲）")
        else:
            print(f"[跳过] 没有 {GEO_FILE}，不生成 {ASIA_FILE}")
    except Exception as e:
        print(f"[警告] 写 {ASIA_FILE} 失败: {e}")
    delays = {id(n): d for d, n in results}
    best = select_nodes(publishable, delays, hk, MAX_OUTPUT)

    # 落一份状态快照（不参与任何判断，只用于观测）
    try:
        hk_alive = hk_alive_keys(hk, HK_TTL_H)
        fresh_hk = {k for k, v in hk.items()
                    if time.time() - float(v.get("ts") or 0) <= HK_TTL_H * 3600}
        dist = {}
        for n in best:
            dist[n["type"]] = dist.get(n["type"], 0) + 1
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "updated": int(time.time()),
                "updated_utc": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
                "nodes": len(best),
                "by_type": dist,
                "hk_only": HK_ONLY,
                "hk_fresh": len(fresh_hk),
                "hk_fresh_alive": len(hk_alive),
                "publishable": len(publishable),
                "proxied_via": PROBE_VIA or None,
                "geo_known": len(_GEO),
                "asia_nodes": sum(1 for n in best if geo_country(n, _GEO) in ASIA_COUNTRIES),
                "cdn_nodes": sum(1 for n in best if is_cdn_fronted(n, _GEO)),
            }, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print(f"[警告] 写 {STATUS_FILE} 失败: {e}")

    dist, stat = {}, {"HK": 0, "HK超时": 0, "本机": 0, "无数据": 0}
    for n in best:
        dist[n["type"]] = dist.get(n["type"], 0) + 1
        name = n["name"]
        if name.endswith("超时]") and ("[HK " in name or "[JP " in name or "[SG " in name or "[VN " in name):
            stat["HK超时"] += 1
        elif any(t in name for t in ("[HK ", "[JP ", "[SG ", "[VN ")):
            stat["HK"] += 1
        elif name.endswith("ms]"):
            stat["本机"] += 1
        else:
            stat["无数据"] += 1
    print(f"输出 {len(best)} 个节点（不按延迟淘汰，只按协议排序）")
    print(f"  类型分布: {dist}")
    print(f"  香港测得 {stat['HK']} 个，香港超时 {stat['HK超时']} 个，"
          f"本机探活 {stat['本机']} 个，无数据 {stat['无数据']} 个")
    if hk:
        print(f"  香港缓存 {len(hk)} 条（由 hk_probe.py 分轮积累）")
    if results:
        ds = sorted(d for d, _ in results)
        print(f"  CI 延迟区间: {ds[0]}~{ds[-1]}ms（美国机房测量值，不是你的延迟）")
    write_outputs(best)


if __name__ == "__main__":
    main()
