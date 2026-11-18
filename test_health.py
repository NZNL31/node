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

# 同一端点上的多个节点，本轮只应记一次失败（否则「连续 2 轮」= 同轮 2 次）
e1 = {"name": "5.5.5.5:5#a", "server": "5.5.5.5", "port": 5, "type": "vless"}
e2 = {"name": "5.5.5.5:5#b", "server": "5.5.5.5", "port": 5, "type": "vless"}
k1, d1, _b1, h1 = st.apply_health([e1, e2], set(), {"5.5.5.5:5#a", "5.5.5.5:5#b"},
                                  {"nodes": {}}, 2, 1)
print("同端点两个节点一轮后 fails:", h1["nodes"]["5.5.5.5:5"]["fails"], "| 淘汰:", len(d1))
assert h1["nodes"]["5.5.5.5:5"]["fails"] == 1, "同轮不应重复累加"
assert len(d1) == 0, "只失败一轮不该淘汰"

# UDP 协议豁免：hysteria2 探不通也不能淘汰（CI 对 UDP 出网限制很严）
h = node("6.6.6.6", 6, "hy2-node")
h["type"] = "hysteria2"
k3, d3, _b3, h3 = st.apply_health([h], set(), {"hy2-node"}, {"nodes": {}}, 1, 1)
print("hysteria2 失败一轮后:", "淘汰" if d3 else "保留", "| 有无记录:", h3["nodes"])
assert len(d3) == 0 and len(k3) == 1, "UDP 协议不该被淘汰"
assert "6.6.6.6:6" not in h3["nodes"], "豁免的协议不该累计失败次数"

# 香港测得通的节点不能因为「CI 连不上」被淘汰（CI 在美国，亚洲通 ≠ 美国通）
h = node("7.7.7.7", 7, "hk-ok")
k4, d4, _b4, h4 = st.apply_health([h], set(), {"hk-ok"}, {"nodes": {}}, 1, 1,
                                  hk_alive={"7.7.7.7:7"})
print("香港测通但 CI 失败:", "保留" if k4 else "被淘汰")
assert len(k4) == 1 and not d4, "香港测得通的节点必须豁免淘汰"

# 香港探测按「最久未测优先」取样，取满一轮的额度
import hk_probe as hkp
nodes = [{"server": f"1.1.1.{i}", "port": 443} for i in range(1, 6)]
batch = hkp.pick_batch(nodes, {"nodes": {}}, 2)
picked = {t[3]["server"] for t in batch}
print("香港探测优先取:", sorted(picked))
assert len(picked) == 2, picked

print("\n全部断言通过")
