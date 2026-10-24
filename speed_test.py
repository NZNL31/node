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
  4. **不按延迟筛选**，只用来排序和打标记（原因见下）
  5. 输出 clash.yaml（给 Clash 用）和 v2.txt（base64 的 URI 列表）

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

  唯一还会被淘汰的是 mihomo -t 校验不通过的节点（字段不合法，会让整个配置
  加载失败），这个跟网络无关，必须摘掉。

环境变量：
  MIHOMO_BIN         mihomo 可执行文件路径，默认 ./mihomo
  MAX_PROBE          最多探测多少个节点，默认 1500
  PROBE_CONCURRENCY  并发数，默认 32
  MAX_OUTPUT         最终最多输出多少个节点，默认 1000（超出才按协议配额裁剪）
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
MAX_OUTPUT = int(os.environ.get("MAX_OUTPUT", 1000))

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


def select_nodes(nodes, delays, max_output):
    """
    选取并排序，不按延迟淘汰。

    nodes    : 全部节点（已过 mihomo -t 校验）
    delays   : {id(node): 延迟}，只包含 CI 探活的节点
    max_output: 输出上限。节点数没超上限时全部输出，只有超了才按协议配额裁剪。

    排序规则：按协议分块，国内可达率高的协议排前面（vless/trojan/hysteria2/tuic
    → vmess → ss → ssr）；块内 CI 探活的在前（延迟升序），没探活的在后。

    这样做的理由见文件头：CI 延迟跟国内可用性负相关，拿它筛选会把可用率从
    44% 砍到 15.6%。它只能当排序依据和参考标记。
    """
    by_type = {}
    for n in nodes:
        by_type.setdefault(n.get("type", "?"), []).append(n)

    for t, lst in by_type.items():
        lst.sort(key=lambda n: (id(n) not in delays,
                                delays.get(id(n), 0)))

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
        d = delays.get(id(node))
        node = dict(node)
        # 只给探活的打标记，明确这是美国机房测出来的值，不是用户的延迟
        if d:
            node["name"] = f"{node['name']} [US {d}ms]"
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
            # 节点多（900+）的时候健康检查本身就不便宜，interval 放宽到 10 分钟，
            # lazy 让这个组没被选中时直接跳过检查，别在手机上白耗流量
            {"name": "🔄 自动选择", "type": "url-test",
             "url": TEST_URL, "interval": 600, "tolerance": 50, "lazy": True,
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
        write_outputs(nodes[:MAX_OUTPUT])
        return

    nodes, ok = validate_config(nodes)
    if not ok:
        print("[警告] 配置校验未通过，跳过测速，保留全部节点")
        write_outputs(nodes[:MAX_OUTPUT])
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

    print(f"探活完成：{len(results)}/{len(nodes)} 个节点从 CI 连得通")

    # 注意：这里不做「可用太少就放弃」的降级。探活数量本来就不该影响发布，
    # 因为 CI 连不通 ≠ 国内连不通（实测 CI 探活的 199 个里国内只有 31 个能用，
    # 而未探活的节点里反而藏着大部分国内能用的）。
    delays = {id(n): d for d, n in results}
    best = select_nodes(nodes, delays, MAX_OUTPUT)

    dist = {}
    probed = 0
    for n in best:
        dist[n["type"]] = dist.get(n["type"], 0) + 1
        if n["name"].endswith("ms]"):
            probed += 1
    print(f"输出 {len(best)} 个节点（不按延迟淘汰，只按协议排序）")
    print(f"  类型分布: {dist}")
    print(f"  其中 CI 探活 {probed} 个，其余 {len(best) - probed} 个未探活"
          f"（不代表国内不能用）")
    if results:
        ds = sorted(d for d, _ in results)
        print(f"  CI 延迟区间: {ds[0]}~{ds[-1]}ms（美国机房测量值，不是你的延迟）")
    write_outputs(best)


if __name__ == "__main__":
    main()
