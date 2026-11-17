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
  5. 输出 clash.yaml（Clash / mihomo 用）、v2.txt（base64 的 URI 列表，
     v2rayN / Shadowrocket 用）、singbox.json（sing-box 内核的客户端用）

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
  所以现在只用延迟做两件事：在同类节点里排序、在名字里打 [US 123ms] 标记。
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
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

ALL_NODES_FILE = "all_nodes.yaml"
CLASH_FILE = "clash.yaml"
V2_FILE = "v2.txt"
SINGBOX_FILE = "singbox.json"
PROBE_FILE = "probe.json"
HK_FILE = "hk_latency.json"
HEALTH_FILE = "node_health.json"

SINGBOX_BIN = os.environ.get("SINGBOX_BIN", "./sing-box")
API_BASE = "http://127.0.0.1:9090"
TEST_URL = os.environ.get("PROBE_URL", "https://www.gstatic.com/generate_204")
TIMEOUT_MS = 5000
MAX_PROBE = int(os.environ.get("MAX_PROBE", 2500))
CONCURRENCY = int(os.environ.get("PROBE_CONCURRENCY", 32))
MAX_OUTPUT = int(os.environ.get("MAX_OUTPUT", 1500))
# 香港延迟数据超过这个时长就不参与排序和打标签（和 hk_probe.py 的 TTL 对齐）
HK_TTL_H = float(os.environ.get("HK_TTL_H", 96))
# 「🚀 精选」组的大小：手机上的健康检查只覆盖这么多节点
FEATURED_SIZE = int(os.environ.get("FEATURED_SIZE", 30))
# 连续多少轮真实探活失败就淘汰。设 1 = 单轮失败即删（激进），默认 2 留一轮容错，
# 因为并发 32 时有相当一批是排队超时/被临时限流，不是真死。
DEAD_ROUNDS = int(os.environ.get("DEAD_ROUNDS", 2))
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


def type_rank(t):
    try:
        return TYPE_ORDER.index(t)
    except ValueError:
        return len(TYPE_ORDER)


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


def apply_health(nodes, alive_names, probed_names, health, dead_rounds, min_keep):
    """
    更新连续失败计数，并淘汰「连续 dead_rounds 轮都连不上」的节点。

    这条策略和「不按延迟筛选」不冲突，是两回事：

      * 延迟只影响排序，永远不淘汰 —— 因为 CI 在美国测出来的快慢与国内无关
        （实测按延迟筛选把可用率从 44% 砍到 15.6%）。
      * 但**连不上**是另一回事：一个节点从任何角度看都建立不了连接，留着它只会
        让客户端的 url-test 白跑一遍。所以连不上要淘汰，只是给一轮容错。

    没被探活的节点（sing-box 表达不了的 xhttp/SSR）不参与判定，永远不会被这条
    规则删掉 —— 我们没测过它，不能因为它「不可测」就当成死了。
    """
    now = int(time.time())
    entries = health["nodes"]
    pool_keys = {node_key(n) for n in nodes}
    bumped = set()          # 本轮已经记过失败的端点

    for n in nodes:
        key = node_key(n)
        if n["name"] in alive_names:
            entries.pop(key, None)                    # 通了就清零
        elif n["name"] in probed_names:
            # 同一个 server:port 上可能挂着好几个节点（不同 UUID）。它们共用一条
            # 记录，所以本轮只累加一次 —— 否则「连续 N 轮」会变成「同一轮 N 次」。
            if key in bumped:
                continue
            bumped.add(key)
            e = entries.get(key) or {"fails": 0}
            e["fails"] = int(e.get("fails", 0)) + 1
            e["last"] = now
            entries[key] = e

    kept, dropped = [], []
    for n in nodes:
        failing = n["name"] in probed_names and n["name"] not in alive_names
        if failing and int((entries.get(node_key(n)) or {}).get("fails", 0)) >= dead_rounds:
            dropped.append(n)
        else:
            kept.append(n)

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
            "sni": node.get("servername", ""),
        }
        return "vmess://" + base64.b64encode(
            json.dumps(obj, ensure_ascii=False).encode()
        ).decode()

    if t == "vless":
        q = {}
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
        query = urllib.parse.urlencode(q)
        pwd = urllib.parse.quote(str(node.get("password", "")), safe="")
        return f"hysteria2://{pwd}@{host}:{port}?{query}#{name}"

    if t == "tuic":
        q = {}
        if node.get("sni"):
            q["sni"] = node["sni"]
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


def write_probe_config(outbounds, path=PROBE_FILE):
    config = {
        "log": {"level": "warn"},
        "inbounds": [{"type": "mixed", "tag": "in", "listen": "127.0.0.1",
                      "listen_port": 7890}],
        "outbounds": list(outbounds) + [{"type": "direct", "tag": "direct"}],
        "route": {"final": "direct"},
        "experimental": {"clash_api": {"external_controller": "127.0.0.1:9090"}},
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
    proc = subprocess.Popen(
        [SINGBOX_BIN, "run", "-c", PROBE_FILE],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
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


def probe_one(name: str):
    """让代理核心走指定节点请求一次，返回延迟毫秒；失败返回 None。"""
    q = urllib.parse.urlencode({"timeout": TIMEOUT_MS, "url": TEST_URL})
    url = f"{API_BASE}/proxies/{urllib.parse.quote(str(name), safe='')}/delay?{q}"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_MS / 1000 + 5) as resp:
            data = json.loads(resp.read().decode())
        delay = data.get("delay")
        return int(delay) if isinstance(delay, int) and delay > 0 else None
    except Exception:
        return None


def probe_all(nodes, label="测速"):
    """并发探测一批节点，返回 [(delay, node), ...]。"""
    out = []
    if not nodes:
        return out
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {pool.submit(probe_one, n["name"]): n for n in nodes}
        done = 0
        for fut in as_completed(futures):
            node = futures[fut]
            delay = fut.result()
            done += 1
            if delay:
                out.append((delay, node))
            if done % 50 == 0:
                print(f"  {label}进度 {done}/{len(nodes)}，可用 {len(out)}")
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

    def sort_key(n):
        e = hk_fresh(n)
        if e:
            ms = e.get("ms")
            if ms is not None:
                return (0, ms)
            return (3, 0)                     # 香港连不上 → 垫底
        d = delays.get(id(n))
        if d:
            return (1, d)
        return (2, 0)

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
        e = hk_fresh(node)
        d = delays.get(id(node))
        node = dict(node)
        if e and e.get("ms") is not None:
            node["name"] = f"{node['name']} [HK {e['ms']:g}ms]"
        elif e:
            node["name"] = f"{node['name']} [HK 超时]"
        elif d:
            # 没有香港数据时退回 CI 的测量值，但必须标明是美国测的
            node["name"] = f"{node['name']} [US {d}ms]"
        out.append(node)
    return out


# ------------------------------------------------------------------ 输出

def build_clash_config(nodes):
    names = [n["name"] for n in nodes]
    # 精选组只放排在最前面的若干个节点（香港延迟最低的一批）。手机上的问题是
    # 「🔄 自动选择」包住全部节点，而它默认被选中，于是 lazy 不起作用，每 10 分钟
    # 真的去健康检查上千个节点。精选组把这份开销压到 FEATURED_SIZE 个。
    featured = names[:FEATURED_SIZE]
    return {
        # 订阅是公开的，配置会落到别人手里；开 allow-lan 等于把一个无鉴权的
        # 代理端口暴露给整个局域网。要共享时自己改成 true。
        "allow-lan": False,
        "mode": "Rule",
        "log-level": "info",
        "port": 7890,
        "socks-port": 7891,
        # 统一延迟：url-test 比较时剔除握手时间，只按真实 RTT 排，否则
        # 慢 TLS 握手会污染排序结果
        "unified-delay": True,
        # 并发建连，多节点池下首包明显更快
        "tcp-concurrent": True,
        "dns": {
            "enable": True,
            "ipv6": False,
            # fake-ip：域名解析不阻塞连接建立，并把 DNS 交给代理远端解析，
            # 顺带避免本地 DNS 污染
            "enhanced-mode": "fake-ip",
            "fake-ip-range": "198.18.0.1/16",
            "default-nameserver": ["223.5.5.5", "119.29.29.29"],
            "nameserver": ["https://dns.alidns.com/dns-query",
                           "https://doh.pub/dns-query"],
            "fallback": ["https://1.1.1.1/dns-query",
                         "https://dns.google/dns-query"],
            "fallback-filter": {
                "geoip": True,
                "geoip-code": "CN",
                "ipcidr": ["240.0.0.0/4"],
            },
        },
        "proxies": nodes,
        "proxy-groups": [
            {"name": "🌐 节点选择", "type": "select",
             "proxies": ["🚀 精选", "🔄 自动选择"] + names},
            {"name": "🚀 精选", "type": "url-test",
             "url": TEST_URL, "interval": 300, "tolerance": 50,
             "expected-status": 204, "proxies": featured},
            # 节点多（900+）的时候健康检查本身就不便宜，interval 放宽到 10 分钟，
            # lazy 让这个组没被选中时直接跳过检查，别在手机上白耗流量
            {"name": "🔄 自动选择", "type": "url-test",
             "url": TEST_URL, "interval": 600, "tolerance": 50, "lazy": True,
             "expected-status": 204, "proxies": names},
        ],
        # 国内和局域网直连。原来只有 MATCH 一条，等于把国内站点也塞进代理，
        # 白付一份延迟和流量。
        "rules": [
            "GEOIP,LAN,DIRECT,no-resolve",
            "GEOIP,CN,DIRECT",
            "MATCH,🌐 节点选择",
        ],
    }


# sing-box 的 route 规则里没有 GEOIP 了（geoip 规则在 1.12 被移除），要直连国内
# 只有两条路：远程 rule_set（首次启动要联网下载，节点全挂时下不下来就起不来），
# 或者域名后缀。这里选后者：不依赖任何下载，配置永远能加载。
CN_DOMAIN_SUFFIX = [
    "cn", "baidu.com", "qq.com", "tencent.com", "weixin.qq.com", "taobao.com",
    "tmall.com", "alicdn.com", "alipay.com", "aliyun.com", "jd.com", "163.com",
    "126.com", "blizzard.com.cn", "bilibili.com", "hdslb.com", "zhihu.com",
    "weibo.com", "sina.com.cn", "iqiyi.com", "youku.com", "douyin.com",
    "meituan.com", "dianping.com", "12306.cn", "ctrip.com", "xiaomi.com",
    "mi.com", "huawei.com", "vivo.com.cn", "oppo.com", "kuaishou.com",
    "bytedance.com", "toutiao.com", "pinduoduo.com", "ximalaya.com",
    "netease.com", "qcloud.com", "myqcloud.com", "bdstatic.com",
]


def build_singbox_config(nodes):
    """
    同一批节点的 sing-box 版配置（给 sing-box 内核的客户端用）。

    和 clash.yaml 是「同一份节点、两种渲染」，不是两套数据：节点字典是唯一真相源。
    差异都在结构性字段上 —— 策略组变成 selector / urltest outbound，规则变成
    route.rules 里的 JSON 对象。

    sing-box 表达不了的节点（xhttp 传输、SSR）会被跳过，所以在 singbox.json 里
    会比 clash.yaml 少几个 —— 数量对不上是预期的，不是 bug。
    """
    names, outbounds = [], []
    for n in nodes:
        ob, _reason = node_to_outbound(n)
        if ob:
            outbounds.append(ob)
            names.append(ob["tag"])
    featured = names[:FEATURED_SIZE]
    return {
        "log": {"level": "info", "timestamp": True},
        "dns": {
            "servers": [
                # DoH 服务器本身是域名，需要一个本地解析器来解析它（sing-box 1.12+
                # 要求显式指定，否则报 missing domain resolver）
                {"tag": "local", "type": "local"},
                {"tag": "ali", "type": "https", "server": "dns.alidns.com",
                 "domain_resolver": "local"},
                {"tag": "dnspod", "type": "https", "server": "doh.pub",
                 "domain_resolver": "local"},
            ],
            # 只解析本地需要的部分：域名是原样交给节点去连的，不会在这里泄漏出去。
            # 故意不使用 detour 走代理 —— 免费节点随时会挂，DNS 不该依赖它们。
            "rules": [],
            "final": "ali",
            "strategy": "prefer_ipv4",
        },
        "inbounds": [
            {"type": "mixed", "tag": "mixed-in", "listen": "127.0.0.1",
             "listen_port": 7890},
        ],
        "outbounds": outbounds + [
            {"type": "selector", "tag": "🌐 节点选择",
             "outbounds": ["🚀 精选", "🔄 自动选择"] + names,
             "default": "🚀 精选"},
            {"type": "urltest", "tag": "🚀 精选", "outbounds": featured,
             "url": TEST_URL, "interval": "5m", "tolerance": 50},
            {"type": "urltest", "tag": "🔄 自动选择", "outbounds": names,
             "url": TEST_URL, "interval": "10m", "tolerance": 50},
            {"type": "direct", "tag": "direct"},
        ],
        "route": {
            "rules": [
                {"action": "sniff"},
                {"protocol": "dns", "action": "hijack-dns"},
                {"ip_is_private": True, "outbound": "direct"},
                {"domain_suffix": CN_DOMAIN_SUFFIX, "outbound": "direct"},
            ],
            "final": "🌐 节点选择",
            # 节点的 server 有不少是域名。sing-box 1.12 起不允许隐式解析，
            # 必须显式指定解析器，否则整个配置加载失败。
            "default_domain_resolver": "ali",
            "auto_detect_interface": True,
        },
    }


def write_outputs(nodes):
    """三份产物一起写，避免其中一份残留上一轮的内容。"""
    with open(CLASH_FILE, "w", encoding="utf-8") as f:
        yaml.dump(build_clash_config(nodes), f, allow_unicode=True,
                  sort_keys=False, default_flow_style=False)

    uris = [u for u in (node_to_uri(n) for n in nodes) if u]
    with open(V2_FILE, "w", encoding="utf-8") as f:
        f.write(base64.b64encode("\n".join(uris).encode()).decode())

    sbox = build_singbox_config(nodes)
    with open(SINGBOX_FILE, "w", encoding="utf-8") as f:
        json.dump(sbox, f, ensure_ascii=False, indent=1)

    node_obs = len(sbox["outbounds"]) - 3          # 去掉两个组和 direct
    print(f"已写出 {CLASH_FILE}（{len(nodes)} 个节点）")
    print(f"已写出 {V2_FILE}（{len(uris)} 条 URI）")
    print(f"已写出 {SINGBOX_FILE}（{node_obs} 个 outbound；"
          f"比 clash.yaml 少 {len(nodes) - node_obs} 个 sing-box 表达不了的节点）")


# ------------------------------------------------------------------ 主流程

def main():
    if not os.path.exists(ALL_NODES_FILE):
        print(f"[错误] 找不到 {ALL_NODES_FILE}", file=sys.stderr)
        sys.exit(1)

    with open(ALL_NODES_FILE, encoding="utf-8") as f:
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

    # 翻译成 sing-box outbound。翻不了的节点（xhttp 传输、SSR）不参与探活，
    # 但仍然原样发布 —— Clash 客户端那边它们是能用的。
    by_name = {n["name"]: n for n in nodes}
    outbounds, unrepresentable = [], {}
    for n in nodes:
        ob, reason = node_to_outbound(n)
        if ob:
            outbounds.append(ob)
        else:
            unrepresentable[reason] = unrepresentable.get(reason, 0) + 1
    if unrepresentable:
        print(f"[跳过探活] {sum(unrepresentable.values())} 个节点 sing-box 表达不了："
              f"{unrepresentable}（仍会发布，只是没有延迟标记）")

    good, bad = validate_outbounds(outbounds)
    if bad:
        print(f"[校验] {len(bad)} 个节点 sing-box 加载不了，本轮不参与探活（仍会发布）")
        for tag, msg in bad[:5]:
            print(f"   {tag} → {msg}")
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

    print(f"探活完成：{len(results)}/{len(probe_nodes)} 个节点从 CI 连得通")

    # 淘汰「连续 DEAD_ROUNDS 轮都连不上」的节点。延迟不参与淘汰，连接失败参与。
    alive = {n["name"] for _, n in results}
    probed = {n["name"] for n in probe_nodes}
    health = load_health()
    before = len(nodes)
    nodes, dropped, floor_blocked, health = apply_health(
        nodes, alive, probed, health, DEAD_ROUNDS, MIN_KEEP)
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

    # 注意：这里仍然不做「可用太少就放弃」的降级。淘汰有 MIN_KEEP 兜底，
    # 但探活数量本身不该影响发布，因为 CI 连不通 ≠ 国内连不通（实测 CI 探活的
    # 199 个里国内只有 31 个能用，而未探活的节点里反而藏着大部分国内能用的）。
    delays = {id(n): d for d, n in results}
    hk = load_hk_latency()
    best = select_nodes(nodes, delays, hk, MAX_OUTPUT)

    dist, stat = {}, {"HK": 0, "HK超时": 0, "US": 0, "无数据": 0}
    for n in best:
        dist[n["type"]] = dist.get(n["type"], 0) + 1
        name = n["name"]
        if name.endswith("[HK 超时]"):
            stat["HK超时"] += 1
        elif "[HK " in name:
            stat["HK"] += 1
        elif name.endswith("ms]"):
            stat["US"] += 1
        else:
            stat["无数据"] += 1
    print(f"输出 {len(best)} 个节点（不按延迟淘汰，只按协议排序）")
    print(f"  类型分布: {dist}")
    print(f"  香港测得 {stat['HK']} 个，香港超时 {stat['HK超时']} 个，"
          f"仅美国探活 {stat['US']} 个，无数据 {stat['无数据']} 个")
    if hk:
        print(f"  香港缓存 {len(hk)} 条（由 hk_probe.py 分轮积累）")
    if results:
        ds = sorted(d for d, _ in results)
        print(f"  CI 延迟区间: {ds[0]}~{ds[-1]}ms（美国机房测量值，不是你的延迟）")
    write_outputs(best)


if __name__ == "__main__":
    main()
