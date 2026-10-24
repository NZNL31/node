#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
借 check-host.net 的香港探测点（hk1）测「香港 → 节点」的 TCP 连接耗时。

为什么要这一步
--------------
`speed_test.py` 跑在 GitHub Actions 上，在美国机房，测出来的延迟对国内用户
没有参考价值（详见 speed_test.py 文件头）。香港探测点离中国近得多，能给出一个
「这个节点离中国有多远」的估计：

    vless 节点在香港测得 8ms   → 大概率在香港/广东，国内 RTT 也就 30~40ms
    vless 节点在香港测得 220ms → 大概率在美国，国内 RTT 200ms+

实测换算：国内 RTT ≈ 香港 RTT + 30ms 左右（中国→香港这一段本身约 20~40ms）。

必须说清楚它测不出什么
----------------------
**它测不出墙。** GFW 部署在中国出境链路上，封锁发生在流量离开中国的那一刻。
从香港探测走的是「香港 → 节点」，全程不经过 GFW，所以一个在国内被墙的节点，
从香港看照样是通的。所以这个数据只能用来排序（谁离中国近），不能用来判断
「国内能不能用」。

为什么不一次测完
----------------
check-host.net 是免费公共服务，有速率限制，实测连续提交约 50 次后开始返回
`{"error":"limit_exceeded"}`。所以采用「每轮测一小批 + 结果写进 hk_latency.json
缓存 + 按最久没测过的优先轮转」的方式，几轮下来自然覆盖全量。缓存只影响排序，
不影响节点是否被发布，所以数据旧一点没关系。

环境变量：
  HK_BATCH    每轮最多测多少个节点，默认 30
  HK_TTL_H    结果缓存多少小时算过期，默认 96（4 天）
  HK_NODE     探测点，默认 hk1.node.check-host.net
"""

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml

ALL_NODES_FILE = "all_nodes.yaml"
CACHE_FILE = "hk_latency.json"

API = "https://check-host.net"
NODE = os.environ.get("HK_NODE", "hk1.node.check-host.net")
BATCH = int(os.environ.get("HK_BATCH", 30))
TTL_H = float(os.environ.get("HK_TTL_H", 96))
UA = "Mozilla/5.0 (compatible; node-subscription-aggregator/1.0)"

# TCP 探测对 UDP 协议没有意义，直接跳过
UDP_TYPES = {"hysteria2", "tuic", "wireguard", "hysteria"}


def http_json(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": UA,
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def load_cache():
    if not os.path.exists(CACHE_FILE):
        return {"updated": 0, "nodes": {}}
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d.get("nodes"), dict):
            d["nodes"] = {}
        return d
    except Exception:
        return {"updated": 0, "nodes": {}}


def save_cache(cache):
    cache["updated"] = int(time.time())
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, CACHE_FILE)


def pick_batch(nodes, cache, batch):
    """
    挑这一轮要测的节点：没测过的优先，其次是最久没测过的。

    同一批里按 key 的哈希排序，而不是按节点池顺序。节点池是按订阅源顺序拼起来的，
    按顺序取会让前几轮全落在一个源上（实测第一轮 20 个全是同一个源里的伊朗节点），
    哈希排序能让每批都散布在整个池子里，取样更有代表性。
    """
    now = time.time()
    fresh = []
    for n in nodes:
        key = f"{str(n['server']).lower()}:{n['port']}"
        entry = cache["nodes"].get(key)
        if entry and now - entry.get("ts", 0) < TTL_H * 3600:
            continue
        fresh.append((entry.get("ts", 0) if entry else 0,
                      hashlib.md5(key.encode()).hexdigest(), key, n))

    fresh.sort(key=lambda x: (x[0], x[1]))
    return fresh[:batch]


def submit(host, port):
    """提交一次探测，返回 request_id；限流返回字符串 'LIMIT'，其它错误返回 None。"""
    q = urllib.parse.urlencode({"host": f"{host}:{port}", "node": NODE})
    try:
        data = http_json(f"{API}/check-tcp?{q}")
    except Exception as e:
        print(f"  [跳过] {host}:{port} 提交失败: {e}")
        return None
    if data.get("ok") == 1 and data.get("request_id"):
        return data["request_id"]
    if "limit" in json.dumps(data).lower():
        return "LIMIT"
    print(f"  [跳过] {host}:{port} 接口返回: {str(data)[:120]}")
    return None


def parse_result(payload):
    """
    返回 (done, ms)：
      done=False 表示还在跑；done=True 时 ms 为 None 表示连不上。
    """
    if payload is None:
        return False, None
    if isinstance(payload, dict):
        # {"error": "..."} 这种是整体失败
        return True, None
    if not isinstance(payload, list) or not payload:
        return False, None
    for item in payload:
        if isinstance(item, dict) and isinstance(item.get("time"), (int, float)):
            return True, round(float(item["time"]) * 1000, 1)
    return True, None


def main():
    if not os.path.exists(ALL_NODES_FILE):
        print(f"[跳过] 找不到 {ALL_NODES_FILE}", file=sys.stderr)
        return

    with open(ALL_NODES_FILE, encoding="utf-8") as f:
        nodes = (yaml.safe_load(f) or {}).get("proxies") or []

    tcp_nodes = [n for n in nodes
                 if n.get("type") not in UDP_TYPES and n.get("server") and n.get("port")]
    cache = load_cache()
    batch = pick_batch(tcp_nodes, cache, BATCH)

    print(f"香港探测点：可测节点 {len(tcp_nodes)} 个，本轮取 {len(batch)} 个"
          f"（缓存里已有 {len(cache['nodes'])} 条）")
    if not batch:
        print("全部节点都在缓存有效期内，本轮跳过")
        return

    # ---- 提交 ----
    submitted, limited = {}, False
    for _, _, key, n in batch:
        rid = submit(n["server"], n["port"])
        if rid == "LIMIT":
            print("  触发限流，本轮提前收工")
            limited = True
            break
        if rid:
            submitted[rid] = key
        time.sleep(0.6)   # 温和一点，别把免费服务打挂

    print(f"已提交 {len(submitted)} 个探测" + ("（被限流）" if limited else ""))

    # ---- 收集 ----
    got = 0
    deadline = time.time() + 90
    while submitted and time.time() < deadline:
        time.sleep(4)
        for rid in list(submitted):
            try:
                res = http_json(f"{API}/check-result/{rid}")
            except Exception:
                continue
            done, ms = parse_result(res.get(NODE))
            if not done:
                continue
            key = submitted.pop(rid)
            cache["nodes"][key] = {"ms": ms, "ts": int(time.time())}
            got += 1

    if submitted:
        print(f"  {len(submitted)} 个探测超时未回，下一轮再试")

    save_cache(cache)
    ok = sum(1 for k in cache["nodes"].values() if k.get("ms") is not None)
    print(f"本轮拿到 {got} 条结果；缓存共 {len(cache['nodes'])} 条，"
          f"其中 {ok} 条连通")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # 这一步只是锦上添花，失败了不能拖垮整个订阅更新
        print(f"[警告] 香港探测出错，跳过：{e}", file=sys.stderr)
