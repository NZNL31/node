# -*- coding: utf-8 -*-
"""Muse 前置筛选纯函数回归测试；不启动 sing-box、不联网。"""
import base64
import os
import sys
import tempfile
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import muse_filter as mf

# 1) base64 订阅合并后必须按 (服务器, 端口) 去重
tmp = tempfile.mkdtemp()
uris = [
    "vless://u1@1.2.3.4:443?encryption=none&security=tls&sni=a.com#same",
    "vless://u2@1.2.3.4:443?encryption=none&security=tls&sni=b.com#dup",
    "ss://YWVzLTEyOC1nY206cGFzcw==@5.6.7.8:8388#ss",
]
for idx, name in enumerate(("a.txt", "b.txt")):
    with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
        f.write(base64.b64encode("\n".join(uris[idx:]).encode()).decode())
entries = mf.load_entries((os.path.join(tmp, "a.txt"), os.path.join(tmp, "b.txt")))
assert len(entries) == 2, entries

# 1.1) 新鲜候选池是 Clash YAML，也必须能转回订阅 URI
yaml_path = os.path.join(tmp, "candidates.yaml")
with open(yaml_path, "w", encoding="utf-8") as f:
    f.write("""proxies:
- name: yaml-node
  type: vless
  server: 9.9.9.9
  port: 443
  uuid: 11111111-2222-3333-4444-555555555555
  tls: true
  servername: example.com
""")
yaml_entries = mf.load_entries((yaml_path,))
assert len(yaml_entries) == 1 and "://" in yaml_entries[0][0], yaml_entries

# 2) 相同显示名必须生成不同探针 tag，但原 URI 保持不变
probed = mf.unique_probe_names(entries)
assert probed[0][1]["name"] != probed[1][1]["name"]
assert probed[0][0] == entries[0][0]

# 3) Cloudflare trace 解析
trace = mf.parse_trace("ip=203.0.113.7\nloc=us\ncolo=IAD\n")
assert trace == {"ip": "203.0.113.7", "loc": "US"}, trace

# 4) 只有 Muse API 200、支持地区、400ms 内三者同时成立才入池
assert mf.acceptance(200, "US", 120, {"US", "SG"}, 400)
assert not mf.acceptance(200, "DE", 120, {"US", "SG"}, 400)
assert not mf.acceptance(403, "US", 120, {"US", "SG"}, 400)
assert not mf.acceptance(200, "US", 401, {"US", "SG"}, 400)
assert not mf.acceptance(None, None, 10, {"US", "SG"}, 400)
assert mf.acceptance(200, "US", 5000, {"US", "SG"}, 0)

print("Muse 前置筛选回归测试全部通过")
