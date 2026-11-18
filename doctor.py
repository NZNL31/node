#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
服务器体检：探活结果全 0 时，先定位是哪一层断了。

探活链路有 4 层，任何一层坏掉都会表现成「3264 个节点 0 个可用」：

  1. 系统时间   偏差超过几分钟 → TLS 握手全部失败（延迟探活走的是 HTTPS）
  2. CA 证书    缺 ca-certificates → 同样全部 TLS 失败（精简镜像常见）
  3. 出网       机器本身连不了外网 → 连目标站都到不了，当然测不出节点
  4. 到节点     机房出口 IP 被节点白名单拉黑 → 外网通，但一个节点都连不上

用法：
    python3 doctor.py                 # 自动读 candidates.yaml 抽样测节点
    python3 doctor.py my_nodes.yaml
"""
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

OK, WARN, BAD = "✔", "⚠", "✘"


def hdr(title):
    print(f"\n=== {title} ===")


def check_time():
    """用明文 HTTP 拿对端的 Date 头，比对本地时间（不依赖 TLS/证书）。"""
    hdr("1. 系统时间")
    remote = None
    for url in ("http://www.gstatic.com/generate_204", "http://ip-api.com/line/?fields=query"):
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=8) as r:
                remote = r.headers.get("Date")
                if remote:
                    break
        except Exception:
            continue
    local = datetime.now(timezone.utc)
    print(f"  本地 UTC: {local.strftime('%Y-%m-%d %H:%M:%S')}")
    if not remote:
        print(f"  {WARN} 拿不到对端时间（也可能出网不通，继续看第 3 项）")
        return None
    try:
        rt = datetime.strptime(remote, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
    except Exception:
        print(f"  {WARN} 对端时间格式无法解析: {remote}")
        return None
    skew = abs((local - rt).total_seconds())
    print(f"  对端 UTC: {rt.strftime('%Y-%m-%d %H:%M:%S')}（偏差 {skew:.0f} 秒）")
    if skew > 300:
        print(f"  {BAD} 时间偏差过大，TLS 握手会失败 —— 先校准：timedatectl set-ntp true")
    elif skew > 60:
        print(f"  {WARN} 略有偏差，暂不影响")
    else:
        print(f"  {OK} 正常")
    return skew


def check_ca():
    hdr("2. CA 证书")
    paths = ["/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt",
             "/etc/ssl/cert.pem"]
    found = [p for p in paths if os.path.exists(p)]
    print(f"  系统 CA 文件: {found or '未找到'}")
    ctx = ssl.create_default_context()
    print(f"  ssl 默认加载 {len(ctx.get_ca_certs())} 张证书")
    if not ctx.get_ca_certs():
        print(f"  {BAD} 没有可用 CA —— 装一下：apt install -y ca-certificates")
        return False
    print(f"  {OK} 可用")
    return True


def check_egress():
    hdr("3. 出网（直连，不经代理）")
    target = "https://www.gstatic.com/generate_204"
    t0 = time.time()
    try:
        with urllib.request.urlopen(target, timeout=10) as r:
            code = r.status
            body = r.read(8)
        print(f"  {OK} {target} → {code}（{time.time()-t0:.2f}s, {len(body)} 字节）")
        if code != 204:
            print(f"  {WARN} 期望 204，实际 {code}（可能被中间设备劫持/改写）")
        return True
    except Exception as e:
        print(f"  {BAD} {target} 失败: {type(e).__name__}: {str(e)[:120]}")
        print("     → 这台机器本身出不了网，探活必然是 0；先解决出网再谈节点")
        return False


def check_dns():
    hdr("4. DNS")
    for host in ("www.gstatic.com", "raw.githubusercontent.com"):
        try:
            ip = socket.gethostbyname(host)
            print(f"  {OK} {host} → {ip}")
        except Exception as e:
            print(f"  {BAD} {host} 解析失败: {type(e).__name__}")


def node_sample(path, limit=20):
    hdr("5. 到节点（TCP 直连抽样）")
    if not os.path.exists(path):
        print(f"  {WARN} 找不到 {path}，跳过（可手动传一个节点文件）")
        return None
    try:
        import yaml
        nodes = (yaml.safe_load(open(path, encoding="utf-8")) or {}).get("proxies") or []
    except Exception as e:
        print(f"  {WARN} 读 {path} 失败: {e}")
        return None
    step = max(1, len(nodes) // limit)
    sample = nodes[::step][:limit]
    ok = 0
    fails = []
    for n in sample:
        host, port = str(n.get("server", "")), int(n.get("port") or 0)
        try:
            with socket.create_connection((host, port), timeout=3):
                ok += 1
        except Exception as e:
            fails.append((host, port, type(e).__name__))
    print(f"  抽样 {len(sample)} 个节点，TCP 可达 {ok} 个")
    for f in fails[:5]:
        print(f"    {BAD} {f[0]}:{f[1]}  {f[2]}")
    if ok == 0:
        print(f"  {BAD} 一个都连不上 —— 大概率是这台机房的出口 IP 被节点白名单整段拉黑，")
        print("     或者该机器被限制访问非常规端口。用 PROBE_VIA 换视角，或换机房。")
    elif ok < len(sample) // 4:
        print(f"  {WARN} 可达率很低，符合「云厂商 IP 被大量节点排除」的典型情况")
    return ok


def check_resources():
    hdr("6. 资源与连接数上限")
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        print(f"  文件句柄上限: soft={soft} hard={hard}")
        if soft < 4096:
            print(f"  {WARN} 偏低（3264 个节点 × 并发连接容易撞上限）："
                  f"ulimit -n 65535")
    except Exception:
        pass
    try:
        mem = [l for l in open("/proc/meminfo") if l.startswith("MemTotal")]
        print(f"  {mem[0].strip() if mem else '内存信息不可读'}")
    except Exception:
        pass
    print(f"  负载: {os.getloadavg() if hasattr(os, 'getloadavg') else 'n/a'}")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "candidates.yaml"
    print("服务器探活体检")
    print("=" * 60)
    skew = check_time()
    ca = check_ca()
    egress = check_egress()
    check_dns()
    nodes_ok = node_sample(path)
    check_resources()

    hdr("结论")
    if egress is False:
        print(f"  {BAD} 第一层就断了：这台机器出不了网。探活全 0 与节点无关。")
    elif ca is False or (skew is not None and skew > 300):
        print(f"  {BAD} 时间或证书有问题：所有 TLS 都会失败，看起来就像「节点全死」。")
    elif nodes_ok == 0:
        print(f"  {BAD} 出网正常，但一个节点都连不上 → 出口 IP 被节点大面积拉黑。")
        print("     方案：PROBE_VIA 换视角（链到另一个出口），或换机房。")
    else:
        print(f"  {OK} 底层链路正常（抽样 TCP 通了 {nodes_ok} 个）。")
        print("     全 0 的原因应该在流水线内部：看 logs/run-*.log 里")
        print("     「延迟探活：0/N」之前的几行（sing-box 是否启动成功、是否报校验失败）。")
    print("=" * 60)


if __name__ == "__main__":
    main()
