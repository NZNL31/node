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
