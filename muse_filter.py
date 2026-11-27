#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从本机真正可访问 Muse 的节点，而不是只凭延迟和纯净度猜测。

纯净度、香港延迟和机房位置都只能预测「可能可用」，不能证明一条节点能在当前
机器上完成代理握手、访问 Muse 后端并从支持的地区出口。本脚本把这三件事放在
纯净度排序之前实测：

  1. 默认读新鲜的 candidates.yaml，并按 (服务器, 端口) 去重；
  2. 先做快速 TCP 预筛，减少完整代理握手的探测量；
  3. 用 sing-box 经剩余节点请求 Muse API，要求 HTTP 200；
  4. 经同一条节点读取 Cloudflare 出口国家，只保留 Muse 支持的地区；
  5. 记录本机 Muse 请求耗时。默认只排序，不做硬淘汰。

注意：香港探针的 400ms 和本机经节点访问 Muse 的耗时不是同一个指标。前者测的是
探针到节点，后者还包含本机出境、代理握手和 Muse 后端响应，不能直接套用同一个阈值。
确需限制本机往返时再设置 MUSE_MAX_LATENCY_MS。

默认不限制输出数量，让后续纯度排名仍有足够候选；可用 MUSE_MAX 截断。

用法：
    python muse_filter.py
    MUSE_COUNTRIES=US python muse_filter.py       # 固定美国出口
    MUSE_MAX=50 python muse_filter.py             # 最多输出 50 条

环境变量：
    MUSE_SOURCES        默认 candidates.yaml；不存在时用 v2.txt
    MUSE_OUT            默认 v2_muse_ok.txt（base64 订阅）
    MUSE_REPORT         默认 muse_reach.json（逐节点结果）
    MUSE_COUNTRIES      默认 US,CA,JP,SG,MX,AU,GB
    MUSE_TARGET         默认 https://muse.ai/api/consent/status
    MUSE_TIMEOUT        单次请求超时，默认沿用 speed_test.VERIFY_TIMEOUT
    MUSE_SLOTS          并发槽位，默认 64
    MUSE_TCP_PRECHECK   1 = 先用 TCP 排除死端口，默认开启
    MUSE_TCP_TIMEOUT    TCP 预筛超时秒数，默认 3.0
    MUSE_TCP_MIN        少于该数量时跳过 TCP 预筛，默认 300
    MUSE_TCP_WORKERS    TCP 预筛并发，默认 128
    MUSE_MAX_LATENCY_MS 本机往返硬门槛，默认 0（不限，只排序）
    MUSE_MAX            最多输出多少条，0 = 不限
"""
import base64
import json
import os
import queue as queue_mod
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import fetch_subscriptions as fp
import speed_test as st
import yaml

DEFAULT_SOURCE = "candidates.yaml" if os.path.exists("candidates.yaml") else "v2.txt"
SOURCES = tuple(p.strip() for p in os.environ.get(
    "MUSE_SOURCES", DEFAULT_SOURCE).split(",") if p.strip())
OUT_FILE = os.environ.get("MUSE_OUT", "v2_muse_ok.txt")
REPORT_FILE = os.environ.get("MUSE_REPORT", "muse_reach.json")
TARGET_URL = os.environ.get(
    "MUSE_TARGET", "https://muse.ai/api/consent/status")
TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
TIMEOUT = float(os.environ.get("MUSE_TIMEOUT", st.VERIFY_TIMEOUT))
SLOTS = int(os.environ.get("MUSE_SLOTS", "64"))
TCP_PRECHECK = os.environ.get("MUSE_TCP_PRECHECK", "1") == "1"
TCP_TIMEOUT = float(os.environ.get("MUSE_TCP_TIMEOUT", "3.0"))
TCP_WORKERS = int(os.environ.get("MUSE_TCP_WORKERS", "128"))
TCP_MIN = int(os.environ.get("MUSE_TCP_MIN", "300"))
MAX_LATENCY_MS = float(os.environ.get("MUSE_MAX_LATENCY_MS", "0"))
MAX_OUTPUT = int(os.environ.get("MUSE_MAX", "0"))
COUNTRIES = {
    c.strip().upper() for c in os.environ.get(
        "MUSE_COUNTRIES", "US,CA,JP,SG,MX,AU,GB").split(",") if c.strip()
}


def decode_subscription(path):
    """读取明文、整份 base64 的 URI 订阅，或 Clash YAML。"""
    with open(path, encoding="utf-8") as f:
        raw = f.read().strip()
    if "proxies:" in raw or path.lower().endswith((".yaml", ".yml")):
        doc = yaml.safe_load(raw) or {}
        lines = []
        for node in doc.get("proxies") or []:
            uri = st.node_to_uri(node)
            if uri:
                lines.append(uri)
        return lines
    if "://" in raw:
        return [line.strip() for line in raw.splitlines() if "://" in line]
    text = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode()
    return [line.strip() for line in text.splitlines() if "://" in line]


def load_entries(paths=SOURCES):
    """合并订阅并按 (服务器, 端口) 去重；保留首次出现的分享链接。"""
    entries, seen = [], set()
    for path in paths:
        if not os.path.exists(path):
            print(f"[跳过] 找不到 {path}")
            continue
        for line in decode_subscription(path):
            node = fp.parse_uri(line)
            if not node:
                continue
            key = (str(node.get("server") or "").strip().lower(),
                   int(node.get("port") or 0))
            if not key[0] or key[1] <= 0 or key in seen:
                continue
            seen.add(key)
            entries.append((line, node))
    return entries


def tcp_ok(host, port, timeout=TCP_TIMEOUT):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def tcp_prefilter(entries):
    """排除端口已死的节点；UDP 协议无法用 TCP 判断，因此直接保留。"""
    if not TCP_PRECHECK or not entries or len(entries) < TCP_MIN:
        return entries
    kept, checked = [], []
    for entry in entries:
        line, node = entry
        if node.get("type") in st.UDP_PROTOCOLS:
            kept.append(entry)
        else:
            checked.append(entry)
    print(f"TCP 预筛 {len(checked)} 条（{TCP_WORKERS} 并发，超时 {TCP_TIMEOUT:g}s）…")
    with ThreadPoolExecutor(max_workers=TCP_WORKERS) as pool:
        futures = {}
        for entry in checked:
            _, node = entry
            futures[pool.submit(tcp_ok, node["server"], node["port"])] = entry
        done = 0
        for future in as_completed(futures):
            entry = futures[future]
            if future.result():
                kept.append(entry)
            done += 1
            if done % 500 == 0:
                print(f"  TCP 进度 {done}/{len(checked)}，保留 {len(kept)}")
    print(f"TCP 预筛保留 {len(kept)}/{len(entries)} 条（含 UDP 例外）")
    return kept


def unique_probe_names(entries):
    """给探针出站生成唯一 tag，同时保留真正要写出的 URI。"""
    counts, used, out = {}, set(), []
    for line, node in entries:
        base = str(node.get("name") or "muse").strip() or "muse"
        counts[base] = counts.get(base, 0) + 1
        alias = base if counts[base] == 1 and base not in used else \
            f"{base} [muse #{counts[base]} {node.get('server')}:{node.get('port')}]"
        while alias in used:
            counts[base] += 1
            alias = f"{base} [muse #{counts[base]} {node.get('server')}:{node.get('port')}]"
        used.add(alias)
        probe = dict(node)
        probe["name"] = alias
        out.append((line, probe))
    return out


def parse_trace(body):
    """解析 Cloudflare trace，返回 {"ip": ..., "loc": ...}。"""
    values = {}
    for line in (body or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    loc = values.get("loc", "").upper()
    return {"ip": values.get("ip"), "loc": loc or None}


def request_via(port, url, timeout):
    """经指定本地 mixed 入站发请求，返回 (状态码, 响应体, 耗时毫秒)。"""
    proxy = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    started = time.monotonic()
    try:
        with opener.open(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "ignore")
            status = getattr(resp, "status", resp.getcode())
    except Exception:
        return None, "", (time.monotonic() - started) * 1000
    return status, body, (time.monotonic() - started) * 1000


def acceptance(status, loc, latency_ms, countries=COUNTRIES,
               max_latency_ms=MAX_LATENCY_MS):
    """纯函数：判断一次 Muse 探针结果是否达到入池门槛。"""
    latency_ok = max_latency_ms <= 0 or latency_ms <= max_latency_ms
    return (status == 200 and bool(loc) and loc.upper() in countries
            and latency_ok)


def resolve_singbox():
    box = st.SINGBOX_BIN
    if not os.path.exists(box):
        for candidate in ("./sing-box.exe", "./sing-box"):
            if os.path.exists(candidate):
                box = candidate
                break
    st.SINGBOX_BIN = box
    if not os.path.exists(box):
        sys.exit("找不到 sing-box：请设置 SINGBOX_BIN，或把 sing-box(.exe) 放在当前目录")
    return box


def loadable_entries(entries):
    """用 sing-box check 剔除会让整份配置加载失败的节点。"""
    outbounds = [st.node_to_outbound(node)[0] for _, node in entries]
    good, bad = st.validate_outbounds(outbounds)
    if not bad:
        return entries
    bad_tags = {tag for tag, _ in bad}
    print(f"[剔除] {len(bad)} 个节点无法被 sing-box 加载")
    for tag, reason in bad[:5]:
        print(f"  {tag} → {reason}")
    return [entry for entry in entries if entry[1]["name"] not in bad_tags]


def run_probes(entries):
    """启动一个多槽位 sing-box，并发探测全部节点。"""
    if not entries:
        return []
    box = resolve_singbox()

    base = st.verify_port_base(SLOTS)
    cfg = st.build_verify_config([n for _, n in entries], SLOTS, base)
    if cfg is None:
        return []
    probe_file = "muse_probe.json"
    with open(probe_file, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)

    proc = subprocess.Popen([box, "run", "-c", probe_file],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True)
    rows, lock = [], threading.Lock()
    try:
        st.wait_for_clash_api(proc, "Muse 校验实例")
        todos = queue_mod.Queue()
        for item in entries:
            todos.put(item)
        done = [0]

        def worker(idx):
            selector, port = f"sel-{idx}", base + idx
            while True:
                try:
                    line, node = todos.get_nowait()
                except queue_mod.Empty:
                    return
                if not st.select_proxy(selector, node["name"]):
                    status, muse_ms, loc, ip = None, None, None, None
                else:
                    status, _, muse_ms = request_via(port, TARGET_URL, TIMEOUT)
                    trace_status, trace_body, _ = request_via(
                        port, TRACE_URL, TIMEOUT)
                    trace = parse_trace(trace_body) if trace_status == 200 else {}
                    loc, ip = trace.get("loc"), trace.get("ip")
                ok = acceptance(status, loc, muse_ms or float("inf"))
                row = {"line": line, "name": node["name"], "type": node["type"],
                       "server": str(node["server"]), "port": int(node["port"]),
                       "muse_status": status, "muse_ms": round(muse_ms, 1)
                       if muse_ms is not None else None,
                       "exit_ip": ip, "exit_country": loc, "ok": ok}
                with lock:
                    rows.append(row)
                    done[0] += 1
                    if done[0] % 20 == 0:
                        passed = sum(r["ok"] for r in rows)
                        print(f"  进度 {done[0]}/{len(entries)}，通过 {passed}")

        threads = [threading.Thread(target=worker, args=(i,), daemon=True)
                   for i in range(SLOTS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return rows


def main():
    all_entries = load_entries()
    if not all_entries:
        sys.exit("候选池为空：检查 MUSE_SOURCES")

    representable = []
    unsupported = {}
    for line, node in all_entries:
        outbound, reason = st.node_to_outbound(node)
        if outbound:
            representable.append((line, node))
        else:
            unsupported[reason] = unsupported.get(reason, 0) + 1
    print(f"候选 {len(all_entries)} 条，sing-box 可校验 {len(representable)} 条")
    if unsupported:
        print(f"[跳过] sing-box 不支持的节点：{unsupported}")

    representable = tcp_prefilter(representable)
    entries = unique_probe_names(representable)
    resolve_singbox()
    entries = loadable_entries(entries)
    if not entries:
        sys.exit("TCP 可连的节点都无法被 sing-box 加载")
    rows = run_probes(entries)
    rows.sort(key=lambda r: (not r["ok"], r["muse_ms"] if r["muse_ms"] is not None
                             else float("inf")))
    passed = [r for r in rows if r["ok"]]
    if MAX_OUTPUT > 0:
        passed = passed[:MAX_OUTPUT]

    report = {
        "updated": int(time.time()),
        "target": TARGET_URL,
        "countries": sorted(COUNTRIES),
        "max_latency_ms": MAX_LATENCY_MS,
        "candidates": len(all_entries),
        "probed": len(rows),
        "passed": len(passed),
        "nodes": [{k: v for k, v in r.items() if k != "line"} for r in rows],
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    if not passed:
        print(f"[错误] 没有节点同时满足 Muse API、出口地区和耗时的门槛；"
              f"保留原有 {OUT_FILE}，详情见 {REPORT_FILE}", file=sys.stderr)
        sys.exit(1)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(base64.b64encode("\n".join(r["line"] for r in passed).encode()).decode())

    by_country, by_type = {}, {}
    for row in passed:
        by_country[row["exit_country"]] = by_country.get(row["exit_country"], 0) + 1
        by_type[row["type"]] = by_type.get(row["type"], 0) + 1
    print(f"Muse 实测通过 {len(passed)}/{len(rows)}（{by_type}，出口 {by_country}）")
    print(f"已写出 {OUT_FILE} 和 {REPORT_FILE}")


if __name__ == "__main__":
    main()
