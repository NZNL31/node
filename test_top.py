# -*- coding: utf-8 -*-
"""选优选链路回归测试：纯净度标注 → sidecar → top-N 去重与回查。全程不联网。"""
import base64
import json
import os
import sys
import tempfile
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_subscriptions as fp
import purity
import select_top

# 1) 标注名必须与 sidecar 记录的名字一致（否则 select_top 回查会全部落空）
u = ("vless://11111111-2222-3333-4444-555555555555@1.2.3.4:443"
     "?encryption=none&security=tls&sni=a.com#nodeA")
entries = [[u, fp.parse_uri(u)]]
line = purity.annotate(entries, {0: "10.0.0.1"}, {"10.0.0.1": (96, "A")})[0]
name = fp.parse_uri(line)["name"]
assert name == purity.final_name("nodeA", 96, "A"), name

# 2) 去重：同 /24 上限 3、同 ASN 上限 4、同 IP 上限 1
rows = [{"name": f"n{i}", "ip": f"10.0.0.{i}", "score": 90 - i, "asn": "AS1"} for i in range(10)]
assert len(select_top.select_top(rows, n=10)) == 3

rows = [{"name": f"m{i}", "ip": f"10.{i}.0.1", "score": 80, "asn": "AS1"} for i in range(10)]
assert len(select_top.select_top(rows, n=10)) == 4

rows = [{"name": "x1", "ip": "10.9.9.9", "score": 70, "asn": "AS9"},
        {"name": "x2", "ip": "10.9.9.9", "score": 60, "asn": "AS9"}]
assert len(select_top.select_top(rows, n=5)) == 1

# 3) 无评分的节点不参评
assert select_top.select_top([{"name": "z", "ip": "1.1.1.1", "score": None}], n=5) == []

# 4) 端到端：标注 → sidecar → 选出 top3 且分数最高者在内
tmp = tempfile.mkdtemp()
os.chdir(tmp)
uris = [f"vless://u{i}@1.0.0.{i}:443?encryption=none&security=tls&sni=a.com#node{i}"
        for i in range(6)]
entries = [[x, fp.parse_uri(x)] for x in uris]
ips = {i: f"10.1.0.{i}" for i in range(6)}
res = {f"10.1.0.{i}": (50 + i * 5, "C") for i in range(6)}
lines = purity.annotate(entries, ips, res)
open("v2.txt", "w", encoding="utf-8").write(
    base64.b64encode("\n".join(lines).encode()).decode())
qrows = [{"name": purity.final_name(n["name"], res[ips[i]][0], res[ips[i]][1]),
          "raw_name": n["name"], "type": n["type"], "server": str(n["server"]),
          "port": n["port"], "ip": ips[i], "score": res[ips[i]][0], "grade": "C",
          "asn": f"AS{i}"} for i, (_, n) in enumerate(entries)]
json.dump({"nodes": qrows}, open("node_quality.json", "w", encoding="utf-8"))

sys.argv = ["select_top.py", "v2.txt", "top3.txt"]
select_top.TOP_N = 3
select_top.main()
got = [fp.parse_uri(x) for x in
       base64.b64decode(open("top3.txt", encoding="utf-8").read()).decode().splitlines()]
assert len(got) == 3, got
assert "C 75" in got[0]["name"], got[0]["name"]

# 5) 名字对不上时按 (类型, 服务器, 端口) 兜底找回
sys.argv = ["select_top.py", "v2.txt", "top3b.txt"]
select_top.TOP_N = 2
q2 = {"nodes": [{"name": "完全不对的名字", "raw_name": "也不对", "type": "vless",
                 "server": "1.0.0.2", "port": 443, "ip": "10.2.2.2",
                 "score": 88, "grade": "B", "asn": "AS9"}]}
json.dump(q2, open("node_quality.json", "w", encoding="utf-8"))
select_top.main()
got2 = base64.b64decode(open("top3b.txt", encoding="utf-8").read()).decode().splitlines()
assert len(got2) == 1, got2

print("选优选链路回归测试全部通过")

# 6) ip_lookup --json 的真实结构：分数嵌在 score.score，ASN 只在 org 里
import types


class _CP:
    def __init__(self, out):
        self.stdout = out
        self.returncode = 0


PAYLOAD = {
    "ip": "1.2.3.4", "org": "AS13335 Cloudflare, Inc.", "country": "US",
    "score": {"score": 84.5, "grade": "B", "confidence": 92,
              "verdict": "可用", "dimensions": []},
}
_real_run = purity.subprocess.run
purity.subprocess.run = lambda *a, **k: _CP(json.dumps(PAYLOAD))
try:
    cache = {}
    score, grade, cached = purity.score_ip("1.2.3.4", cache)
    assert score == 84.5 and grade == "B" and cached is False, (score, grade, cached)
    assert cache["1.2.3.4"]["asn"] == "AS13335", cache
    assert cache["1.2.3.4"]["country"] == "US"
    # 第二次必须命中缓存、不再调用外部命令
    calls = []
    purity.subprocess.run = lambda *a, **k: calls.append(1) or _CP("{}")
    score2, grade2, cached2 = purity.score_ip("1.2.3.4", cache)
    assert cached2 is True and score2 == 84.5 and not calls
finally:
    purity.subprocess.run = _real_run

# 6.1) 批量评分必须把 cache 命中标志从 worker 传回，不能引用未定义变量
_real_score_ip = purity.score_ip
purity.score_ip = lambda ip, cache: (88, "B", True)
try:
    scored = purity.score_ips({"1.2.3.4": [0], "5.6.7.8": [1]}, {}, workers=2)
    assert scored == {"1.2.3.4": (88, "B"), "5.6.7.8": (88, "B")}, scored
finally:
    purity.score_ip = _real_score_ip

# 7) 兼容：顶层就是数字的老实现 / 私有地址这类没有分数的返回
purity.subprocess.run = lambda *a, **k: _CP(json.dumps({"score": 77, "grade": "C"}))
try:
    assert purity.score_ip("9.9.9.9", {})[0] == 77
    purity.subprocess.run = lambda *a, **k: _CP(json.dumps(
        {"ip": "10.0.0.1", "applicable": False, "error": "私有地址"}))
    assert purity.score_ip("10.0.0.1", {})[0] is None
finally:
    purity.subprocess.run = _real_run

print("ipquality JSON 契约断言通过")

