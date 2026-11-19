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
  HK_BATCH        每轮最多测多少个节点，默认 30
  HK_TTL_H        结果缓存多少小时算过期，默认 96（4 天）
  HK_PRUNE_DAYS   已不在节点池里的条目再保留多少天，默认 30
  HK_NODE         探测点，默认 hk1.node.check-host.net
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
# 与 speed_test.py 一致：优先用 CI 提交的候选池
CANDIDATES_FILE = "candidates.yaml"
# 可选：某台机器跑 `check_cn.py --json tcp_reach.json` 产出的可达端点列表。
# 有了它，香港探测的有限名额优先给「从这个视角可达」的节点。
TCP_REACH_FILE = "tcp_reach.json"
# 上一轮通过 204/STUN 的端点（speed_test.py 写）。香港探测优先给这些节点：
# 注定不会发布的节点不该占用有限的名额。
PUBLISHABLE_FILE = "publishable.json"
CACHE_FILE = "hk_latency.json"

API = "https://check-host.net"
NODE = os.environ.get("HK_NODE", "hk1.node.check-host.net")
BATCH = int(os.environ.get("HK_BATCH", 30))
TTL_H = float(os.environ.get("HK_TTL_H", 96))
# 已不在节点池里的孤儿条目保留多少天（池子会来回换，留几天可能还用得上）
PRUNE_DAYS = float(os.environ.get("HK_PRUNE_DAYS", 30))
# 结果采集窗口。原来 150 秒会丢掉近一半结果（实测提交 50 只回来 27），
# 而这些探测其实稍后是能返回的 —— 只是我们等得不够久。
COLLECT_SECONDS = int(os.environ.get("HK_COLLECT_SECONDS", 300))
POLL_SECONDS = float(os.environ.get("HK_POLL_SECONDS", 2.5))
# 被限流时退避多久再试（实测限流是短冷却，十几秒后恢复）
LIMIT_BACKOFF = float(os.environ.get("HK_LIMIT_BACKOFF", 10))
# 整轮最多为限流等待几次（不是每个节点各自等）。
# 之前写成「每节点最多重试 4 次 × 15 秒」，50 个节点最坏会跑 50 分钟，
# 直接撞上工作流 20 分钟超时。现在整轮共享预算，超了就带着已有结果收工。
MAX_LIMIT_WAITS = int(os.environ.get("HK_MAX_LIMIT_WAITS", 3))
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


def prune_cache(cache, nodes):
    """
    删掉既不在当前节点池里、又超过保留期的条目。

    节点池每轮都在换，缓存以前只进不出：实测 1371 条里有 1119 条对应的节点
    早就不在池子里了，文件变成 96KB 还每 3 小时提交一次。保留期内的孤儿条目
    留着有用（节点可能过几天又回来），超过就没人会再用了。
    """
    live = {f"{str(n['server']).lower()}:{n['port']}" for n in nodes}
    cutoff = time.time() - PRUNE_DAYS * 86400
    before = len(cache["nodes"])
    cache["nodes"] = {
        k: v for k, v in cache["nodes"].items()
        if k in live or float(v.get("ts") or 0) >= cutoff
    }
    return before - len(cache["nodes"])



def load_priority():
    """读 publishable.json，返回 (端点集合, 说明文字)。"""
    if not os.path.exists(PUBLISHABLE_FILE):
        return set(), ""
    try:
        with open(PUBLISHABLE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        keys = set(d.get("keys") or [])
    except Exception as e:
        print(f"[警告] 读 {PUBLISHABLE_FILE} 失败，忽略：{e}")
        return set(), ""
    if keys:
        print(f"[香港探测] 优先测 {PUBLISHABLE_FILE} 里的 {len(keys)} 个端点"
              f"（上一轮通过 204/STUN）")
    return keys, "publishable"


def load_tcp_reach(pool_size):
    """
    读 tcp_reach.json（{vantage, ok:["server:port",...]}）。

    带可信度守卫：如果它把几乎全部节点都判为可达，说明那个视角的 TCP 探测被中间设备
    劫持了（实测某台受限机器 15/15、679/679 全通），当筛子等于没筛 —— 直接忽略。
    """
    if not os.path.exists(TCP_REACH_FILE):
        return set(), None
    try:
        with open(TCP_REACH_FILE, encoding="utf-8") as f:
            d = json.load(f)
        ok = set(d.get("ok") or [])
        vantage = d.get("vantage") or "未知"
    except Exception as e:
        print(f"[警告] 读 {TCP_REACH_FILE} 失败，忽略：{e}")
        return set(), None
    if not ok:
        return set(), vantage
    ratio = len(ok) / max(pool_size, 1)
    if ratio > 0.95:
        print(f"[警告] {TCP_REACH_FILE} 声称 {len(ok)}/{pool_size} = {ratio:.0%} 可达，"
              f"这个视角大概率被劫持（全通=没筛），本轮忽略它")
        return set(), vantage
    print(f"[香港探测] 参考 {TCP_REACH_FILE}（vantage={vantage}，{len(ok)} 个可达）")
    return ok, vantage


def pick_batch(nodes, cache, batch, reachable=frozenset(), prioritized=frozenset()):
    """
    挑这一轮要测的节点，优先级：

      1. 在 publishable.json 里（上一轮通过 204/STUN）—— 这是最终可能发布的集合，
         给它优先探测，等于把有限名额从 3000+ 收敛到几百个
      2. 在 tcp_reach.json 里可达的（次优先）
      3. 其它没测过/过期的

    为什么要分档：香港探测每轮只有 50 个名额（check-host.net 限流），全池 3000+ 个节点
    轮不过来。按「某视角 TCP 可达」优先，本质是把名额集中在还活着的节点上 —— 这是
    负面证据的反面用法，不引入「按美国 204 通过筛选」那种偏差（那个群体本机可用率 0%）。

    同一批里按 key 的哈希排序，而不是按节点池顺序。节点池是按订阅源顺序拼起来的，
    按顺序取会让前几轮全落在一个源上（实测第一轮 20 个全是同一个源里的伊朗节点），
    哈希排序能让每批都散布在整个池子里，取样更有代表性。

    注意：这里**故意不按 CI 侧的探活结果排序**。曾经试过「优先测 CI 已通过 204 的
    节点」，但实测那个群体在国内的可用率是 0%（见 README 的校准章节），把香港的
    有限名额投给它们等于浪费预算；无偏轮转才能把「香港到底能碰到谁」测全。
    """
    now = time.time()
    fresh = []
    for n in nodes:
        key = f"{str(n['server']).lower()}:{n['port']}"
        entry = cache["nodes"].get(key)
        if entry and now - entry.get("ts", 0) < TTL_H * 3600:
            continue
        if key in prioritized:
            tier = 0
        elif key in reachable:
            tier = 1
        else:
            tier = 2
        fresh.append((tier, entry.get("ts", 0) if entry else 0,
                      hashlib.md5(key.encode()).hexdigest(), key, n))

    fresh.sort(key=lambda x: (x[0], x[1], x[2]))
    return fresh[:batch]


def merge_caches(local, incoming):
    """
    合并两份 hk_latency.json：同一个端点取 ts 较新的那条。

    为什么需要：这个文件是整文件覆盖写的 —— CI 每小时写一次、别的机器也可能回传，
    谁后推谁赢，先推的那份就丢了。按 ts 合并可以让双方都不丢。
    返回 (合并后的 nodes, 新增条数, 更新条数)。
    """
    merged = dict(local or {})
    added = updated = 0
    for key, entry in (incoming or {}).items():
        if not isinstance(entry, dict):
            continue
        old = merged.get(key)
        if old is None:
            merged[key] = entry
            added += 1
        elif float(entry.get("ts") or 0) > float(old.get("ts") or 0):
            merged[key] = entry
            updated += 1
    return merged, added, updated


def submit(host, port, node=None):
    """提交一次探测，返回 request_id；限流返回字符串 'LIMIT'，其它错误返回 None。"""
    q = urllib.parse.urlencode({"host": f"{host}:{port}", "node": node or NODE})
    try:
        data = http_json(f"{API}/check-tcp?{q}")
    except urllib.error.HTTPError as e:
        # 429 是限流（比 JSON 里的 limit_exceeded 更常见）。以前被当成普通失败跳过，
        # 于是每轮只提交了一半就去采结果了。
        if e.code == 429:
            return "LIMIT"
        print(f"  [跳过] {host}:{port} 提交失败: HTTP {e.code}")
        return None
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
    src = CANDIDATES_FILE if os.path.exists(CANDIDATES_FILE) else ALL_NODES_FILE
    if not os.path.exists(src):
        print(f"[跳过] 找不到 {CANDIDATES_FILE} 或 {ALL_NODES_FILE}", file=sys.stderr)
        return
    print(f"[香港探测] 输入: {src}")

    with open(src, encoding="utf-8") as f:
        nodes = (yaml.safe_load(f) or {}).get("proxies") or []

    tcp_nodes = [n for n in nodes
                 if n.get("type") not in UDP_TYPES and n.get("server") and n.get("port")]
    cache = load_cache()
    prioritized, _src = load_priority()
    reachable, _vantage = load_tcp_reach(len(tcp_nodes))
    batch = pick_batch(tcp_nodes, cache, BATCH, reachable, prioritized)

    print(f"香港探测点：可测节点 {len(tcp_nodes)} 个，本轮取 {len(batch)} 个"
          f"（缓存里已有 {len(cache['nodes'])} 条）")
    if not batch:
        print("全部节点都在缓存有效期内，本轮跳过")
        return

    # ---- 提交 ----
    # 探测点优先序列：hk1 最接近国内，被限流时依次退到 jp1 / sg1，而不是整轮收工。
    # 限流是按来源 IP 的全局短冷却（实测同时被限、十几秒后恢复），所以换点只在
    # 「刚好被限」时有用，不会提升总吞吐。
    nodes_pref = [x.strip() for x in os.environ.get("HK_NODES", NODE).split(",") if x.strip()]
    cur = 0
    limit_waits = 0
    submitted, limited = {}, False
    for _tier, _ts, _h, key, n in batch:
        rid = submit(n["server"], n["port"], nodes_pref[cur])
        while rid == "LIMIT" and limit_waits < MAX_LIMIT_WAITS:
            limit_waits += 1
            cur = (cur + 1) % len(nodes_pref)
            print(f"  被限流，换到 {nodes_pref[cur].split('.')[0]} 并退避 "
                  f"{LIMIT_BACKOFF:g}s（整轮第 {limit_waits}/{MAX_LIMIT_WAITS} 次）")
            time.sleep(LIMIT_BACKOFF)
            rid = submit(n["server"], n["port"], nodes_pref[cur])
        if rid == "LIMIT":
            print(f"  限流退避预算用尽，带着已提交的 {len(submitted)} 个去采结果")
            limited = True
            break
        if rid:
            submitted[rid] = (key, nodes_pref[cur])
        time.sleep(0.6)   # 温和一点，别把免费服务打挂

    print(f"已提交 {len(submitted)} 个探测" + ("（被限流）" if limited else ""))

    # ---- 收集 ----
    got = 0
    deadline = time.time() + COLLECT_SECONDS
    while submitted and time.time() < deadline:
        time.sleep(POLL_SECONDS)
        for rid in list(submitted):
            try:
                res = http_json(f"{API}/check-result/{rid}")
            except Exception:
                continue
            key, used_node = submitted[rid]
            done, ms = parse_result(res.get(used_node))
            if not done:
                continue
            submitted.pop(rid)
            cache["nodes"][key] = {"ms": ms, "ts": int(time.time()), "node": used_node}
            got += 1

    if submitted:
        print(f"  {len(submitted)} 个探测超时未回，下一轮再试")

    pruned = prune_cache(cache, tcp_nodes)
    if pruned:
        print(f"  清理失效条目 {pruned} 条（已不在节点池且超过 {PRUNE_DAYS:g} 天）")

    save_cache(cache)
    ok = sum(1 for k in cache["nodes"].values() if k.get("ms") is not None)
    print(f"本轮拿到 {got} 条结果；缓存共 {len(cache['nodes'])} 条，"
          f"其中 {ok} 条连通")


def merge_from(path):
    """把外部 hk_latency.json 合并进来（按 ts 取新）。"""
    if not path or not os.path.exists(path):
        print(f"[跳过] 没有可合并的文件: {path}")
        return
    try:
        with open(path, encoding="utf-8") as f:
            incoming = (json.load(f) or {}).get("nodes") or {}
    except Exception as e:
        print(f"[警告] 读 {path} 失败: {e}")
        return
    cache = load_cache()
    merged, added, updated = merge_caches(cache["nodes"], incoming)
    cache["nodes"] = merged
    save_cache(cache)
    print(f"[合并] {path}: 新增 {added} 条，更新 {updated} 条，合计 {len(merged)} 条")


if __name__ == "__main__":
    if "--merge" in sys.argv:
        i = sys.argv.index("--merge")
        merge_from(sys.argv[i + 1] if i + 1 < len(sys.argv) else "")
        sys.exit(0)
    try:
        main()
    except Exception as e:
        # 这一步只是锦上添花，失败了不能拖垮整个订阅更新
        print(f"[警告] 香港探测出错，跳过：{e}", file=sys.stderr)
