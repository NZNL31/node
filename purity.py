#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
纯纯净度标注：经每个节点查它真正的**出口 IP**，交给 ipquality 打分，写回节点名。

为什么需要"经节点"取 IP：ipquality 评的是 IP 的干净程度，而节点服务器地址 ≠ 出口
地址（CDN 前置、中转、家宽落地都会不同）。只有从节点里发请求出去看到的那颗 IP
才是你实际在用的那颗。

用法（在 v2.txt 上原地标注）：
    python3 purity.py                 # 默认 v2.txt，原地改写节点名
    python3 purity.py v2.txt
    PURITY_MIN=80 python3 purity.py   # 只保留评分 ≥80 的节点

依赖 ipquality：
    git clone https://github.com/NZNL31/ipquality ../ipquality
    export IPQUALITY_DIR=../ipquality
    pip install -r ../ipquality/requirements.txt
默认调用 `python3 $IPQUALITY_DIR/compat/ip_lookup <ip> --json`；可用 IPQ_CMD 覆盖。

缓存：ip_purity.json（IP → 分数，默认 7 天）。同一个出口 IP 上常挂着几十个节点，
不缓存会把免费 API 额度瞬间打爆。
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import fetch_subscriptions as fp
import speed_test as st

CACHE_FILE = "ip_purity.json"
QUALITY_FILE = "node_quality.json"
CACHE_TTL = float(os.environ.get("PURITY_CACHE_DAYS", 7)) * 86400
IPQUALITY_DIR = os.environ.get("IPQUALITY_DIR", "../ipquality")
IPQ_CMD = os.environ.get("IPQ_CMD", "")
PURITY_MIN = float(os.environ.get("PURITY_MIN", 0))
# 取出口 IP 的地址，多个是为了容错
IP_ECHO = ["https://www.cloudflare.com/cdn-cgi/trace", "https://api.ipify.org"]


def ipq_command(ip):
    if IPQ_CMD:
        return IPQ_CMD.replace("{ip}", ip).split()
    script = os.path.join(IPQUALITY_DIR, "compat", "ip_lookup")
    return [sys.executable, script, ip, "--json"]


def load_cache():
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        return json.load(open(CACHE_FILE, encoding="utf-8"))
    except Exception:
        return {}


def save_cache(c):
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, CACHE_FILE)


def exit_ip_via(port, timeout):
    """经本地代理端口取出口 IP。"""
    proxy = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    for url in IP_ECHO:
        try:
            with opener.open(url, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "ignore")
        except Exception:
            continue
        if "cdn-cgi/trace" in url:
            for line in body.splitlines():
                if line.startswith("ip="):
                    return line[3:].strip()
        else:
            body = body.strip()
            if body:
                return body
    return None


def score_ip(ip, cache):
    """调 ipquality 给 IP 打分；返回 (score, grade, cached_bool)。"""
    now = time.time()
    hit = cache.get(ip)
    if hit and now - hit.get("ts", 0) < CACHE_TTL and hit.get("score") is not None:
        return hit["score"], hit.get("grade"), True
    try:
        r = subprocess.run(ipq_command(ip), capture_output=True, text=True, timeout=180)
        data = json.loads((r.stdout or "").strip() or "{}")
    except Exception as e:
        print(f"[警告] ipquality 调用失败 {ip}: {type(e).__name__}")
        return None, None, False
    # ip_lookup --json 的字段命名可能随版本变，这里做宽松取值
    score = data.get("score", data.get("ip_score", data.get("total")))
    grade = data.get("grade") or data.get("level")
    if score is None:
        print(f"[警告] ipquality 未给出分数 {ip}: {str(data)[:120]}")
        return None, None, False
    # asn / country 用于 top-N 去重（同一 ASN 不能占满名额）
    asn = data.get("asn") or (data.get("basic") or {}).get("asn")
    country = data.get("country") or (data.get("basic") or {}).get("country")
    cache[ip] = {"score": score, "grade": grade, "asn": asn,
                 "country": country, "ts": int(now)}
    return score, grade, False



def quality_tag(score, grade):
    """节点名后缀，例如 `[A 96]`。没分数就空串。"""
    if score is None:
        return ""
    return f"[{grade or '?'} {score:g}]"


def final_name(name, score, grade):
    """标注后的节点名 —— purity 和 select_top 必须用同一个名字，否则对不上。"""
    tag = quality_tag(score, grade)
    return f"{name} {tag}" if tag else name


def annotate(entries, ips, results, purity_min=0):
    """把评分写回节点名；低于阈值的直接丢弃。返回保留下来的链接列表。"""
    out = []
    for idx, (line, n) in enumerate(entries):
        ip = ips.get(idx)
        score, grade = results.get(ip, (None, None))
        if score is not None and purity_min and score < purity_min:
            continue
        tag = quality_tag(score, grade)
        if tag:
            base = line.split("#", 1)[0]
            new_name = urllib.parse.quote(final_name(n["name"], score, grade), safe="")
            line = f"{base}#{new_name}"
        out.append(line)
    return out


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "v2.txt"
    raw = open(src, encoding="utf-8").read().strip()
    text = raw if "://" in raw else base64.b64decode(
        raw + "=" * (-len(raw) % 4)).decode()
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if "://" not in line:
            continue
        n = fp.parse_uri(line)
        if n:
            entries.append([line, n])
    if not entries:
        sys.exit("没有可解析的节点")

    box = st.SINGBOX_BIN
    if not os.path.exists(box):
        box = "./sing-box.exe" if os.path.exists("./sing-box.exe") else "./sing-box"
    st.SINGBOX_BIN = box
    if not os.path.exists(box):
        sys.exit(f"找不到 sing-box（{box}）")

    cache = load_cache()
    cfg = st.build_verify_config([n for _, n in entries], st.VERIFY_SLOTS,
                                 st.VERIFY_PORT_BASE)
    if cfg is None:
        sys.exit("没有可校验的节点")
    with open("purity.json", "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)

    import queue as queue_mod
    import threading
    proc = subprocess.Popen([box, "run", "-c", "purity.json"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    ips = {}
    lock = threading.Lock()
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

        q = queue_mod.Queue()
        for idx, (line, n) in enumerate(entries):
            q.put((idx, n))

        def worker(i):
            sel, port = f"sel-{i}", st.VERIFY_PORT_BASE + i
            while True:
                try:
                    idx, n = q.get_nowait()
                except queue_mod.Empty:
                    return
                if not st.select_proxy(sel, n["name"]):
                    continue
                ip = exit_ip_via(port, st.VERIFY_TIMEOUT)
                with lock:
                    if ip:
                        ips[idx] = ip

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

    print(f"取到出口 IP: {len(ips)}/{len(entries)}")

    by_ip = {}
    for idx, ip in ips.items():
        by_ip.setdefault(ip, []).append(idx)
    print(f"去重后 {len(by_ip)} 个出口 IP，开始打分（命中缓存的不再调 API）…")

    # 评分要调外部 API，几十个 IP 串行会很慢 —— 小并发跑，但要限流（免费额度）
    from concurrent.futures import ThreadPoolExecutor
    results = {}

    def _one(ip):
        score, grade, _cached = score_ip(ip, cache)
        return ip, score, grade

    with ThreadPoolExecutor(max_workers=int(os.environ.get("PURITY_CONCURRENCY", 4))) as pool:
        for ip, score, grade in pool.map(_one, list(by_ip)):
            results[ip] = (score, grade)
        flag = "缓存" if cached else "新查"
        print(f"  {ip:16s} {score if score is not None else '—':>5} "
              f"{(grade or '—'):>2}  ({flag})")
    save_cache(cache)

    # 落一份 sidecar：每个节点的出口 IP 与评分，供 select_top.py 选优选。
    # 名字必须用**标注后**的（带 [A 96]），否则 select_top 按名字回查 v2.txt 会全部落空。
    rows = []
    for idx, (line, n) in enumerate(entries):
        ip = ips.get(idx)
        if not ip:
            continue
        score, grade = results.get(ip, (None, None))
        meta = cache.get(ip) or {}
        rows.append({"name": final_name(n["name"], score, grade),
                     "raw_name": n["name"], "type": n["type"],
                     "server": str(n["server"]), "port": n["port"], "ip": ip,
                     "score": score, "grade": grade,
                     "asn": meta.get("asn"), "country": meta.get("country")})
    with open(QUALITY_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated": int(time.time()), "nodes": rows},
                  f, ensure_ascii=False, indent=1)
    print(f"评分明细已写入 {QUALITY_FILE}（{len(rows)} 条）")

    out = annotate(entries, ips, results, PURITY_MIN)
    kept = len(out)
    dropped = len(entries) - kept

    with open(src, "w", encoding="utf-8") as f:
        f.write(base64.b64encode("\n".join(out).encode()).decode())

    graded = sum(1 for v in results.values() if v[0] is not None)
    print(f"\n完成：{kept} 条写回 {src}（{dropped} 条低于 PURITY_MIN={PURITY_MIN:g} 被剔除）")
    print(f"有评分的出口 IP {graded}/{len(by_ip)}")


if __name__ == "__main__":
    main()
