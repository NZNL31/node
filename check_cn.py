#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从本机（国内）复测订阅节点的 TCP 可达性。

用途：`speed_test.py` 是在 GitHub Actions（美国机房）跑的，它测出来的延迟
跟国内能不能连上基本无关。想知道「这些节点我这边到底能不能用」，就在自己
机器上跑这个脚本。

用法：
    python check_cn.py                          # 测仓库里已发布的 clash.yaml
    python check_cn.py my.yaml                  # 测本地文件
    python check_cn.py https://.../clash.yaml   # 测任意订阅

注意：hysteria2 / tuic 是 UDP 协议，TCP 端口探测对它们必然失败，
所以统计时单独标注、并从「排除 UDP」那一行里剔除。
TCP 能连上也不代表一定能用（可能握手失败、证书不对、被限速），
但连不上就肯定不能用，所以这个指标适合用来筛掉死节点。
"""

import concurrent.futures as cf
import socket
import sys
import urllib.request

DEFAULT_URL = "https://raw.githubusercontent.com/NZNL31/node/main/clash.yaml"
UDP_TYPES = {"hysteria2", "tuic", "wireguard", "hysteria"}
TIMEOUT = 3.0


def tcp_ok(host, port):
    try:
        with socket.create_connection((host, int(port)), timeout=TIMEOUT):
            return True
    except Exception:
        return False


def load_nodes(src):
    if "://" in src:
        raw = urllib.request.urlopen(src, timeout=30).read().decode()
    else:
        with open(src, encoding="utf-8") as f:
            raw = f.read()
    import yaml
    doc = yaml.safe_load(raw) or {}
    return [(p.get("name", ""), p.get("type"), p.get("server"), p.get("port"))
            for p in doc.get("proxies") or []]


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    nodes = load_nodes(src)
    if not nodes:
        sys.exit(f"{src} 里没有找到节点")

    print(f"共 {len(nodes)} 个节点，开始 TCP 探测（超时 {TIMEOUT}s）…")
    results = []
    with cf.ThreadPoolExecutor(max_workers=64) as pool:
        futs = {pool.submit(tcp_ok, s, p): t for _, t, s, p in nodes}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            results.append((futs[fut], fut.result()))
            if i % 100 == 0:
                ok = sum(1 for _, r in results if r)
                print(f"  {i}/{len(nodes)}，可达 {ok}")

    total, ok_total = len(results), sum(1 for _, r in results if r)
    print(f"\n总体：{ok_total}/{total} = {ok_total / total * 100:.1f}%")

    by_type = {}
    for t, r in results:
        d = by_type.setdefault(t, [0, 0])
        d[1] += 1
        d[0] += int(r)

    print("\n分协议：")
    for t, (ok, n) in sorted(by_type.items(), key=lambda x: -x[1][1]):
        note = "  (UDP，TCP 探测无效)" if t in UDP_TYPES else ""
        print(f"  {t:12s} {ok:4d}/{n:<4d} = {ok / n * 100:5.1f}%{note}")

    tcp_only = [(t, r) for t, r in results if t not in UDP_TYPES]
    if tcp_only:
        ok = sum(1 for _, r in tcp_only if r)
        print(f"\n排除 UDP 协议后：{ok}/{len(tcp_only)} = "
              f"{ok / len(tcp_only) * 100:.1f}%")


if __name__ == "__main__":
    main()
