#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按纯净度选优选（默认 50 个），输出一份独立的订阅文件。

这是整条流水线的最后一步：`fetch` 拿全量 → `speed_test` 探通路（204/STUN）→
`purity.py` 经节点查出口 IP 并用 ipquality 打分 → **本脚本按分数挑出最优的 N 个**。

去重规则（避免 50 个名额被同一段 IP 吃光）：
  * 同一个出口 IP 只留 1 个
  * 同一个 /24 最多 3 个
  * 同一个 ASN 最多 4 个
排序：分数降序 → 置信度（有则用）→ 同分时保持原顺序。

用法：
    python3 select_top.py                     # 读 v2.txt + node_quality.json，写 top50.txt
    TOP_N=30 python3 select_top.py            # 改数量
    python3 select_top.py v2.txt top50.txt
"""
import base64
import ipaddress
import json
import os
import sys

import fetch_subscriptions as fp

QUALITY_FILE = "node_quality.json"
TOP_N = int(os.environ.get("TOP_N", 50))
CAP_IP, CAP_NET, CAP_ASN = 1, 3, 4


def net24(ip):
    try:
        return str(ipaddress.ip_network(f"{ip}/24", strict=False))
    except Exception:
        return ip


def select_top(rows, n=TOP_N, cap_ip=CAP_IP, cap_net=CAP_NET, cap_asn=CAP_ASN):
    """纯函数：按分数挑 top-N，并做 IP//24/ASN 三层去重。返回选中的行列表。"""
    pool = [r for r in rows if r.get("score") is not None]
    pool.sort(key=lambda r: (-float(r["score"]), -(float(r.get("confidence") or 0))))
    picked, per_ip, per_net, per_asn = [], {}, {}, {}
    for r in pool:
        ip = r.get("ip") or ""
        if per_ip.get(ip, 0) >= cap_ip:
            continue
        net = net24(ip)
        if per_net.get(net, 0) >= cap_net:
            continue
        asn = (r.get("asn") or "")[:16]
        if asn and per_asn.get(asn, 0) >= cap_asn:
            continue
        picked.append(r)
        per_ip[ip] = per_ip.get(ip, 0) + 1
        per_net[net] = per_net.get(net, 0) + 1
        if asn:
            per_asn[asn] = per_asn.get(asn, 0) + 1
        if len(picked) >= n:
            break
    return picked


def load_uris(path):
    raw = open(path, encoding="utf-8").read().strip()
    text = raw if "://" in raw else base64.b64decode(
        raw + "=" * (-len(raw) % 4)).decode()
    return [l.strip() for l in text.splitlines() if "://" in l]


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "v2.txt"
    out = sys.argv[2] if len(sys.argv) > 2 else f"top{TOP_N}.txt"

    if not os.path.exists(QUALITY_FILE):
        sys.exit(f"找不到 {QUALITY_FILE}：先跑 purity.py（或把 PURITY=1 打开）")
    data = json.load(open(QUALITY_FILE, encoding="utf-8"))
    rows = data.get("nodes") or []
    if not rows:
        sys.exit(f"{QUALITY_FILE} 里没有评分记录")

    picked = select_top(rows)
    if not picked:
        sys.exit("没有可用评分，无法选优选")

    # 按名字把链接取回来。名字是权威标识，但要做三层兜底：
    # 1) 标注后的名字（purity.py 直接写的那个）
    # 2) 去掉 `[A 96]` 后缀的原始名（历史上 purity 曾写过原始名）
    # 3) (类型, 服务器, 端口) —— 名字对不上时的最后手段
    import re
    by_name, by_raw, by_ep = {}, {}, {}
    for line in load_uris(src):
        n = fp.parse_uri(line)
        if not n:
            continue
        name = n["name"]
        by_name[name] = line
        by_raw.setdefault(re.sub(r"\s*\[[A-E?] [\d.]+\]$", "", name), line)
        by_ep.setdefault((n["type"], str(n["server"]).lower(), int(n["port"])), line)

    chosen, missing = [], 0
    for r in picked:
        line = (by_name.get(r["name"])
                or by_raw.get(r.get("raw_name") or "")
                or by_ep.get((r.get("type"), str(r.get("server", "")).lower(),
                              int(r.get("port") or 0))))
        if line:
            chosen.append(line)
        else:
            missing += 1

    with open(out, "w", encoding="utf-8") as f:
        f.write(base64.b64encode("\n".join(chosen).encode()).decode())

    print(f"参评 {len(rows)} 个节点（有评分的）→ 选出 {len(chosen)} 个 → {out}")
    if missing:
        print(f"[注意] {missing} 个选中节点在 {src} 里找不到（可能已被淘汰）")
    print("\n前 10：")
    for i, r in enumerate(picked[:10], 1):
        print(f"  {i:2d}. {str(r['score']):>5} {str(r.get('grade') or '-'):>2} "
              f"{r['type']:9s} {r['server']}:{r['port']}  ip={r['ip']}")


if __name__ == "__main__":
    main()
