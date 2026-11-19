#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成/更新 geo.json：节点服务器 IP → 归属地。

为什么需要：唯一被验证过与「国内能不能用」相关的信号是探测点（check-host），
但它的额度按网段共享，每轮只能提交几十个，覆盖 3000 个候选要几个月。
「服务器机房在哪个国家」不需要任何探测，一次批量查询就能覆盖全池 —— 它是便宜的
先验，用来把有限的探测名额和用户的注意力引到更可能有戏的节点上。

注意：地理位置只是相关性，不是验证。它只影响排序与候选池，不作为发布门槛。

用法：
    python3 geo_lookup.py                      # 增量更新 candidates.yaml 里的 IP
    python3 geo_lookup.py --ttl-days 30        # 超过 30 天的条目重新查
    python3 geo_lookup.py --dry-run            # 只统计要查多少，不联网
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

GEO_FILE = "geo.json"
INPUT_CANDIDATES = ["candidates.yaml", "all_nodes.yaml"]
BATCH_URL = "http://ip-api.com/batch?fields=status,countryCode,as,org,query"
BATCH_SIZE = 100          # 免费额度单次上限
SLEEP_BETWEEN = 1.6       # 免费额度 45 次/分钟，留余量
UA = "node-subscription-aggregator/1.0"


def load_existing(ttl_days):
    """读已有 geo.json，返回 (ip→info, 还需要补查的过期 IP 集合)。"""
    if not os.path.exists(GEO_FILE):
        return {}, set()
    try:
        with open(GEO_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[警告] 读 {GEO_FILE} 失败，当作空表：{e}")
        return {}, set()
    ips = data.get("ips") or {}
    cutoff = time.time() - ttl_days * 86400
    stale = {ip for ip, info in ips.items() if float((info or {}).get("ts") or 0) < cutoff}
    return ips, stale


def server_ips(paths):
    """从候选池里取出去重后的 IPv4/IPv6 字面量（域名跳过，查了也没意义）。"""
    import yaml
    ips = set()
    for p in paths:
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                nodes = (yaml.safe_load(f) or {}).get("proxies") or []
        except Exception as e:
            print(f"[警告] 读 {p} 失败：{e}")
            continue
        for n in nodes:
            s = str(n.get("server", "")).strip().strip("[]")
            if not s:
                continue
            # 纯数字+点/冒号才当 IP；带字母的当域名跳过
            if all(c in "0123456789abcdefABCDEF.:" for c in s) and ("." in s or ":" in s):
                ips.add(s)
    return ips


def lookup(batch):
    """查一批 IP，返回 {ip: {country, asn, org, ts}}。失败返回空字典。"""
    body = json.dumps(batch).encode()
    req = urllib.request.Request(
        BATCH_URL, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            rows = json.loads(r.read().decode())
    except Exception as e:
        print(f"[警告] 批量查询失败：{e}")
        return {}
    now = int(time.time())
    out = {}
    for row in rows or []:
        ip = row.get("query")
        if not ip or row.get("status") != "success":
            continue
        out[ip] = {
            "country": (row.get("countryCode") or "").upper() or None,
            "asn": (row.get("as") or "").split()[0] if row.get("as") else None,
            "org": row.get("org") or None,
            "ts": now,
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ttl-days", type=float, default=30)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    existing, stale = load_existing(args.ttl_days)
    ips = server_ips(INPUT_CANDIDATES)
    if not ips:
        print("[跳过] 候选池里没有 IP（或文件不存在）")
        return

    todo = sorted(ip for ip in ips if ip not in existing or ip in stale)
    print(f"候选 IP {len(ips)} 个；已有归属地 {len(ips & set(existing))} 个；"
          f"待查 {len(todo)} 个（TTL {args.ttl_days:g} 天）")
    if args.dry_run or not todo:
        return

    got, t0 = {}, time.time()
    for i in range(0, len(todo), BATCH_SIZE):
        batch = todo[i:i + BATCH_SIZE]
        got.update(lookup(batch))
        done = min(i + BATCH_SIZE, len(todo))
        print(f"  查询进度 {done}/{len(todo)}（命中 {len(got)}）")
        if done < len(todo):
            time.sleep(SLEEP_BETWEEN)

    merged = dict(existing)
    merged.update(got)
    # 只保留仍然在候选池里的 IP，避免文件无限膨胀
    merged = {ip: info for ip, info in merged.items() if ip in ips}

    payload = {
        "updated": int(time.time()),
        "source": "ip-api.com/batch",
        "count": len(merged),
        "ips": merged,
    }
    with open(GEO_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))

    asia = {"HK", "JP", "SG", "TW", "KR", "MY", "TH", "VN", "PH", "ID", "MO"}
    n_asia = sum(1 for v in merged.values() if v.get("country") in asia)
    print(f"已写出 {GEO_FILE}（{len(merged)} 个 IP，其中亚洲 {n_asia} 个；"
          f"耗时 {time.time() - t0:.0f}s）")


if __name__ == "__main__":
    main()
