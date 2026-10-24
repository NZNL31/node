#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
真实测速：起一个 mihomo 实例，逐个节点走真实代理测延迟。

旧版本是假的：
    latency = random.uniform(30, 150)
    speed   = random.uniform(8, 50)
延迟和速度都是随机数，而且 is_port_open() 做的是 TCP 探测——hysteria2 是 UDP
协议，TCP 端口开不开跟能不能用毫无关系。结果是随机淘汰掉真正可用的节点。

现在的做法：
  1. 把全部节点写进 probe.yaml，启动 mihomo（external-controller: 9090）
  2. 并发调用 /proxies/{name}/delay?url=... 让 mihomo 走真实代理去请求
  3. 首轮失败的节点再补测一轮（并发高时有一批是排队超时，不是真死）
  4. 按协议类型配额选取，而不是单纯按延迟取前 N
  5. 输出 clash.yaml（给 Clash 用）和 v2.txt（base64 的 URI 列表）

注意：延迟是「测速机器 → 节点 → 目标站」的往返时间，测速机器在 GitHub Actions
（美国）。对国内用户来说它只能说明节点活着、离美国近，说明不了国内能不能连。
所以节点名里的延迟标记写成 `[US 123ms]`，明确标注这是美国机房测出来的值，
不是你的延迟；真正该看的是客户端自己的延迟（🔄 自动选择 组会本地实测）。

环境变量：
  MIHOMO_BIN         mihomo 可执行文件路径，默认 ./mihomo
  MAX_PROBE          最多探测多少个节点，默认 1500
  PROBE_CONCURRENCY  并发数，默认 32
  TOP_N              最终保留多少个节点，默认 300
  PROBE_URL          测速目标地址
"""

import base64
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
PROBE_FILE = "probe.yaml"

MIHOMO_BIN = os.environ.get("MIHOMO_BIN", "./mihomo")
API_BASE = "http://127.0.0.1:9090"
TEST_URL = os.environ.get("PROBE_URL", "https://www.gstatic.com/generate_204")
TIMEOUT_MS = 5000
MAX_PROBE = int(os.environ.get("MAX_PROBE", 1500))
CONCURRENCY = int(os.environ.get("PROBE_CONCURRENCY", 32))
TOP_N = int(os.environ.get("TOP_N", 300))

# 国内存活先验：协议越容易被 GFW 识别，国内可达率越低。
#
# 依据是 2026-10-24 对 926 个节点的实测（CI 判定「活」的节点，再从国内 TCP 复测）：
#   vless 362/632 = 57%   vmess 56%   hysteria2 大部分可达   ss 14/179 = 7.8%
# 裸 ss 没有 TLS 伪装、流量特征明显，是 GFW 打击的重点，国内存活率只有 vless 的
# 七分之一。如果按延迟单纯取前 N，ss 会占满名额（它连美国快），而它恰恰是国内
# 最连不上的那批。所以按协议加权分配名额。
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


# ------------------------------------------------------------------ 节点转 URI

def b64url(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().strip("=")


def node_to_uri(node: dict):
    """把 Clash 节点字典还原成分享链接，供 v2.txt 使用。"""
    t = node.get("type")
    name = urllib.parse.quote(str(node.get("name", "")), safe="")
    host = node.get("server")
    port = node.get("port")

    if t == "ss":
        userinfo = b64url(f"{node.get('cipher')}:{node.get('password')}")
        return f"ss://{userinfo}@{host}:{port}#{name}"

    if t == "vmess":
        obj = {
            "v": "2",
            "ps": node.get("name", ""),
            "add": host,
            "port": str(port),
            "id": node.get("uuid"),
            "aid": str(node.get("alterId", 0)),
            "scy": node.get("cipher", "auto"),
            "net": node.get("network", "tcp"),
            "type": "none",
            "host": (node.get("ws-opts", {}).get("headers", {}) or {}).get("Host", ""),
            "path": node.get("ws-opts", {}).get("path", ""),
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
        query = urllib.parse.urlencode(q)
        return f"trojan://{node.get('password')}@{host}:{port}?{query}#{name}"

    if t == "hysteria2":
        q = {}
        if node.get("sni"):
            q["sni"] = node["sni"]
        if node.get("obfs"):
            q["obfs"] = node["obfs"]
            q["obfs-password"] = node.get("obfs-password", "")
        query = urllib.parse.urlencode(q)
        return f"hysteria2://{node.get('password')}@{host}:{port}?{query}#{name}"

    if t == "tuic":
        q = {}
        if node.get("sni"):
            q["sni"] = node["sni"]
        query = urllib.parse.urlencode(q)
        return f"tuic://{node.get('uuid')}:{node.get('password')}@{host}:{port}?{query}#{name}"

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


# ------------------------------------------------------------------ mihomo 控制

def write_probe_config(nodes, path=PROBE_FILE):
    config = {
        "mixed-port": 7890,
        "external-controller": "127.0.0.1:9090",
        "mode": "rule",
        "log-level": "warning",
        "ipv6": False,
        "proxies": nodes,
        "rules": ["MATCH,DIRECT"],
    }
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, sort_keys=False,
                  default_flow_style=False)


def mihomo_available() -> bool:
    if not os.path.exists(MIHOMO_BIN):
        return False
    try:
        subprocess.run([MIHOMO_BIN, "-v"], capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def validate_config(nodes):
    """
    用 mihomo -t 校验配置。个别节点字段不合法会让整个配置加载失败，
    这里根据报错信息把涉事节点摘掉再试，最多 20 轮。
    """
    for _ in range(20):
        write_probe_config(nodes)
        r = subprocess.run([MIHOMO_BIN, "-t", "-d", ".", "-f", PROBE_FILE],
                           capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            return nodes, True
        err = (r.stderr or "") + (r.stdout or "")
        removed = [n for n in nodes if n.get("name") and str(n["name"]) in err]
        if not removed:
            print("[警告] mihomo 校验失败，且无法定位问题节点：")
            print(err.strip()[:600])
            return nodes, False
        for n in removed:
            nodes.remove(n)
        print(f"[校验] 摘掉 {len(removed)} 个不合法节点后重试")
    return nodes, False


def start_mihomo():
    proc = subprocess.Popen(
        [MIHOMO_BIN, "-d", ".", "-f", PROBE_FILE],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    for _ in range(30):
        time.sleep(0.5)
        if proc.poll() is not None:
            err = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(f"mihomo 启动失败: {err[:500]}")
        try:
            with urllib.request.urlopen(f"{API_BASE}/version", timeout=2) as resp:
                if resp.status == 200:
                    return proc
        except Exception:
            continue
    proc.kill()
    raise RuntimeError("mihomo API 30 秒内未就绪")


def probe_one(name: str):
    """让 mihomo 走指定节点请求一次，返回延迟毫秒；失败返回 None。"""
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


def select_nodes(results, top_n):
    """
    按协议加权配额选取节点。

    不能按延迟单纯取前 N：CI 测的是「美国 → 节点」，ss 节点因为离美国近反而
    排前面，而它国内可达率只有 7.8%。按 TYPE_WEIGHT 分名额能把名额让给
    vless / trojan / hysteria2 这些国内存活率高的协议。

    某类型存活数不够时，剩余名额按全局延迟顺序补齐，不会浪费。
    """
    by_type = {}
    for delay, node in sorted(results, key=lambda x: x[0]):
        by_type.setdefault(node.get("type", "?"), []).append((delay, node))

    present = [t for t in by_type if by_type[t]]
    if not present:
        return []

    weight_sum = sum(TYPE_WEIGHT.get(t, DEFAULT_WEIGHT) for t in present)
    picked, used = [], set()
    for t in sorted(present, key=lambda t: -TYPE_WEIGHT.get(t, DEFAULT_WEIGHT)):
        w = TYPE_WEIGHT.get(t, DEFAULT_WEIGHT)
        quota = max(1, round(top_n * w / weight_sum))
        for delay, node in by_type[t][:quota]:
            if len(picked) >= top_n:
                break
            picked.append((delay, node))
            used.add(id(node))

    # 配额没用满（某类型存活数不足）的部分，用「延迟 / 权重」这个有效代价补齐。
    # 不能直接按延迟补：ss 连美国最快，按延迟补等于把刚压下去的名额又还回去。
    if len(picked) < top_n:
        rest = [(delay / TYPE_WEIGHT.get(n.get("type", "?"), DEFAULT_WEIGHT),
                 delay, n)
                for delay, n in results if id(n) not in used]
        rest.sort(key=lambda x: x[0])
        for _, delay, node in rest:
            if len(picked) >= top_n:
                break
            picked.append((delay, node))
            used.add(id(node))

    picked.sort(key=lambda x: x[0])
    out = []
    for delay, node in picked:
        node = dict(node)
        # 标注 US，避免用户把这个数字当成自己的延迟
        node["name"] = f"{node['name']} [US {delay}ms]"
        out.append(node)
    return out


# ------------------------------------------------------------------ 输出

def build_clash_config(nodes):
    names = [n["name"] for n in nodes]
    return {
        "allow-lan": True,
        "mode": "Rule",
        "log-level": "info",
        "port": 7890,
        "proxies": nodes,
        "proxy-groups": [
            {"name": "🌐 节点选择", "type": "select",
             "proxies": ["🔄 自动选择"] + names},
            {"name": "🔄 自动选择", "type": "url-test",
             "url": TEST_URL, "interval": 300, "tolerance": 50,
             "proxies": names},
        ],
        "rules": ["MATCH,🌐 节点选择"],
    }


def write_outputs(nodes):
    """clash.yaml 与 v2.txt 必须一起写，避免 v2.txt 残留上一次的内容。"""
    with open(CLASH_FILE, "w", encoding="utf-8") as f:
        yaml.dump(build_clash_config(nodes), f, allow_unicode=True,
                  sort_keys=False, default_flow_style=False)

    uris = [u for u in (node_to_uri(n) for n in nodes) if u]
    with open(V2_FILE, "w", encoding="utf-8") as f:
        f.write(base64.b64encode("\n".join(uris).encode()).decode())

    print(f"已写出 {CLASH_FILE}（{len(nodes)} 个节点）")
    print(f"已写出 {V2_FILE}（{len(uris)} 条 URI）")


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

    nodes = nodes[:MAX_PROBE]

    # mihomo 不可用时降级：不测速，全部保留，交给客户端自己选
    if not mihomo_available():
        print(f"[警告] 找不到 mihomo（{MIHOMO_BIN}），跳过测速，保留全部节点")
        write_outputs(nodes[:TOP_N])
        return

    nodes, ok = validate_config(nodes)
    if not ok:
        print("[警告] 配置校验未通过，跳过测速，保留全部节点")
        write_outputs(nodes[:TOP_N])
        return

    print(f"启动 mihomo，开始真实测速（{len(nodes)} 个节点，并发 {CONCURRENCY}）")
    proc = start_mihomo()
    try:
        results = probe_all(nodes)
        # 首轮失败的再补一轮：并发 32 的时候总有一批是 mihomo 排队超时，
        # 不是节点真的连不上。补测一次成本很低，能捞回来不少。
        alive = {n["name"] for _, n in results}
        retry = [n for n in nodes if n["name"] not in alive]
        if retry:
            print(f"补测首轮失败的 {len(retry)} 个节点…")
            results += probe_all(retry, label="补测")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    print(f"测速完成：{len(results)}/{len(nodes)} 个节点可用")

    # 可用节点太少时不要发布一份几乎不可用的配置，保留原列表更稳妥
    min_ok = int(os.environ.get("MIN_OK", 10))
    if len(results) < min_ok:
        print(f"[警告] 只有 {len(results)} 个节点通过测速（阈值 {min_ok}），"
              f"跳过筛选，保留原始列表", file=sys.stderr)
        write_outputs(nodes[:TOP_N])
        return

    results.sort(key=lambda x: x[0])
    best = select_nodes(results, TOP_N)

    dist = {}
    for n in best:
        t = n["type"]
        dist[t] = dist.get(t, 0) + 1
    print(f"保留 {len(best)} 个节点（按协议加权配额，ss 名额被压缩）")
    print(f"  类型分布: {dist}")
    print(f"  CI 延迟区间: {results[0][0]}~{results[-1][0]}ms"
          f"（美国机房的测量值，不代表国内延迟）")
    write_outputs(best)


if __name__ == "__main__":
    main()
