#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
在**本机**验证订阅，只保留真能用的节点。

为什么需要它：CI 里的 204/STUN 校验跑在 GitHub 的 runner 上（美国机房），它证明的是
「这个节点从美国能连」，而**你在国内能不能用是另一回事** —— GFW 在出境链路上，
从境外测不到。所以最终筛选必须在你自己机器上做。

用法：
    python local_filter.py                     # 读线上 v2.txt，写 v2_ok.txt
    python local_filter.py v2.txt ok.txt       # 指定输入输出
    python local_filter.py <订阅地址> ok.txt

产出格式与订阅一致（base64 分享链接列表），可以直接导入 v2rayN。
需要本机有 sing-box 可执行文件（默认 ./sing-box，Windows 上找 ./sing-box.exe）。
"""
import base64
import os
import subprocess
import sys
import time
import urllib.request

import speed_test as st


def find_singbox():
    for name in ("./sing-box.exe", "./sing-box", "sing-box.exe", "sing-box"):
        if os.path.exists(name):
            return name
    return None


def load_uris(src):
    if "://" in src:
        # 显式绕过系统代理：这台机器上 7890 常被某个客户端占着，一旦它没起来，
        # 走系统代理拉订阅就会失败（WinError 10061）。拉订阅不需要走代理。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(src, timeout=60) as resp:
            raw = resp.read().decode()
    else:
        with open(src, encoding="utf-8") as f:
            raw = f.read()
    text = raw.strip()
    if "://" not in text:                       # 整体 base64
        text = base64.b64decode(text + "=" * (-len(text) % 4)).decode()
    return [l.strip() for l in text.splitlines() if "://" in l]


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else \
        "https://raw.githubusercontent.com/NZNL31/node/main/v2.txt"
    out = sys.argv[2] if len(sys.argv) > 2 else "v2_ok.txt"

    box = find_singbox()
    if not box:
        sys.exit("找不到 sing-box：请把 sing-box（或 sing-box.exe）放到当前目录")
    st.SINGBOX_BIN = box

    import fetch_subscriptions as fp
    uris = load_uris(src)
    pairs = []
    for u in uris:
        n = fp.parse_uri(u)
        if n:
            pairs.append((u, n))
    print(f"载入 {len(uris)} 条链接，可解析 {len(pairs)} 条")

    tcp = [(u, n) for u, n in pairs if n["type"] not in st.UDP_PROTOCOLS]
    udp = [(u, n) for u, n in pairs if n["type"] in st.UDP_PROTOCOLS]
    print(f"TCP 协议 {len(tcp)} 条 / UDP 协议 {len(udp)} 条")
    print(f"开始本机校验（{st.VERIFY_SLOTS} 并发，单次超时 {st.VERIFY_TIMEOUT}s）…")

    base = st.verify_port_base(st.VERIFY_SLOTS)
    cfg = st.build_verify_config([n for _, n in pairs], st.VERIFY_SLOTS, base)
    if cfg is None:
        sys.exit("没有可用于校验的节点")
    with open("verify_local.json", "w", encoding="utf-8") as f:
        import json
        json.dump(cfg, f, ensure_ascii=False)

    import queue as queue_mod
    import threading
    proc = subprocess.Popen([box, "run", "-c", "verify_local.json"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    ok_names, tcp_ok, udp_ok = set(), 0, 0
    try:
        for _ in range(40):
            time.sleep(0.5)
            if proc.poll() is not None:
                sys.exit(f"sing-box 启动失败: {(proc.stderr.read() or '')[:300]}")
            try:
                urllib.request.urlopen(f"{st.API_BASE}/version", timeout=2)
                break
            except Exception:
                continue

        todos = queue_mod.Queue()
        for u, n in pairs:
            todos.put((u, n))
        lock = threading.Lock()
        done = {"n": 0}

        def worker(idx):
            nonlocal tcp_ok, udp_ok
            selector, port = f"sel-{idx}", base + idx
            while True:
                try:
                    u, n = todos.get_nowait()
                except queue_mod.Empty:
                    return
                if not st.select_proxy(selector, n["name"]):
                    continue
                if n["type"] in st.UDP_PROTOCOLS:
                    ok = st.stun_via(port, st.VERIFY_TIMEOUT)
                    kind = "udp"
                else:
                    ok = st.http_status_via(port, st.VERIFY_TIMEOUT) == 204
                    kind = "tcp"
                with lock:
                    done["n"] += 1
                    if ok:
                        ok_names.add(n["name"])
                        if kind == "tcp":
                            tcp_ok += 1
                        else:
                            udp_ok += 1
                    if done["n"] % 100 == 0:
                        print(f"  进度 {done['n']}/{len(pairs)}  通过 {len(ok_names)}")

        threads = [threading.Thread(target=worker, args=(i,), daemon=True)
                   for i in range(st.VERIFY_SLOTS)]
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

    survivors = [u for u, n in pairs if n["name"] in ok_names]
    with open(out, "w", encoding="utf-8") as f:
        f.write(base64.b64encode("\n".join(survivors).encode()).decode())

    print(f"\n本机校验结果：{len(survivors)}/{len(pairs)} 可用"
          f"（TCP {tcp_ok}/{len(tcp)}，UDP {udp_ok}/{len(udp)}）")
    print(f"已写出 {out}（直接导入 v2rayN 即可）")


if __name__ == "__main__":
    main()
