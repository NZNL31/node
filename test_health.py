# -*- coding: utf-8 -*-
"""apply_health 的行为测试：不跑网络，只验判定逻辑。CI 里每次都会跑。"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import speed_test as st

def node(server, port, name=None):
    return {"name": name or f"{server}:{port}", "server": server, "port": port, "type": "vless"}

a = node("1.1.1.1", 1)          # 本轮通了
b = node("2.2.2.2", 2)          # 本轮失败，首次
c = node("3.3.3.3", 3)          # 本轮失败，上轮也失败 → 淘汰
d = node("4.4.4.4", 4)          # 没被探活（xhttp/SSR）
nodes = [a, b, c, d]
alive = {a["name"]}
probed = {a["name"], b["name"], c["name"]}
health = {"nodes": {"3.3.3.3:3": {"fails": 1, "last": 0},
                    "1.1.1.1:1": {"fails": 3, "last": 0},
                    "9.9.9.9:9": {"fails": 5, "last": 0}}}   # 最后一个已不在池里

kept, dropped, blocked, h = st.apply_health(nodes, alive, probed, health, 2, 1)
print("kept:", [n["name"] for n in kept])
print("dropped:", [n["name"] for n in dropped])
print("health:", json.dumps(h["nodes"], sort_keys=True))
assert [n["name"] for n in kept] == ["1.1.1.1:1", "2.2.2.2:2", "4.4.4.4:4"]
assert [n["name"] for n in dropped] == ["3.3.3.3:3"]
assert "1.1.1.1:1" not in h["nodes"], "通了的节点必须清零"
assert h["nodes"]["2.2.2.2:2"]["fails"] == 1, "首次失败应记 1"
assert "9.9.9.9:9" not in h["nodes"], "已不在池里的记录要清掉"
assert "4.4.4.4:4" not in h["nodes"], "没探活的节点不该有记录"
assert h["nodes"].get("3.3.3.3:3", {}).get("fails") == 2, \
    "被淘汰的节点必须继续累计失败次数，否则下轮会被放回来"

# 第二轮：b 也累计到 2 次失败，应当被淘汰；c 保持淘汰（不抖回来）
kept_b, dropped_b, blocked_b, h_b = st.apply_health(
    nodes, alive, probed, h, 2, 1)
print("第二轮 kept:", [n["name"] for n in kept_b], "dropped:", [n["name"] for n in dropped_b])
assert sorted(n["name"] for n in dropped_b) == ["2.2.2.2:2", "3.3.3.3:3"]
assert h_b["nodes"]["3.3.3.3:3"]["fails"] == 3, "被淘汰的节点仍在累计"

# 地板保护
kept2, dropped2, blocked2, _ = st.apply_health(nodes, alive, probed, {"nodes": {"3.3.3.3:3": {"fails": 5}}}, 2, 10)
print("地板保护:", len(kept2), "kept /", len(dropped2), "dropped")
assert len(dropped2) == 0 and len(kept2) == 4 and blocked2 is True

print("\n全部断言通过")
