# -*- coding: utf-8 -*-
"""解析层回归测试：不联网，只验那几个曾经把节点弄残的地方。CI 里每次都会跑。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_subscriptions as fs


def norm(uri):
    n = fs.parse_uri(uri)
    return fs.normalize_node(dict(n)) if n else None


# 1) 空值参数不能让后面的键轮不到：sni= 为空时必须落到 host
n = norm("vless://u-1@a.example.com:443?sni=&host=cdn.example.com"
         "&security=tls&type=ws&path=%2Fws#t")
assert n and n["servername"] == "cdn.example.com", n
assert (n["ws-opts"]["headers"]["Host"]) == "cdn.example.com"

# 2) 开了 TLS 却没有 SNI：必须有兜底，不能空着
n = norm("vless://u-2@1.2.3.4:443?security=tls&type=tcp#t2")
assert n and n["servername"] == "1.2.3.4", n

# 3) ws 有 Host、TLS 无 SNI：SNI 应取 Host 而不是服务器地址
n = norm("vless://u-3@1.2.3.5:443?security=tls&type=ws&host=real.example.net#t3")
assert n and n["servername"] == "real.example.net", n

# 4) xtls-rprx-vision 但没有 TLS：物理上不可能连通，必须拒绝
assert norm("vless://u-4@1.2.3.6:443?flow=xtls-rprx-vision&security=none#t4") is None

# 5) 完整度清理：同一端点+凭证的残缺版本要被丢弃
full = {"name": "full", "type": "vless", "server": "1.1.1.1", "port": 443,
        "uuid": "u-5", "tls": True, "servername": "x.example.com",
        "network": "ws", "ws-opts": {"path": "/", "headers": {"Host": "x.example.com"}}}
bare = {"name": "bare", "type": "vless", "server": "1.1.1.1", "port": 443,
        "uuid": "u-5"}
kept, dropped_n = fs.prefer_complete([full, bare])
assert dropped_n == 1 and kept == [full], (kept, dropped_n)

# 6) 完整度相同则都保留（可能真是不同入口）
a = dict(full, name="a", ws_opts=None)
b = dict(full, name="b")
a.pop("ws_opts")
kept2, dropped2 = fs.prefer_complete([a, b])
assert dropped2 == 0 and len(kept2) == 2

# 6.1) 凭证只有后缀不同也必须视为不同节点，不能截断到前 24 位
prefix = "u-" + "a" * 24
full_long = dict(full, uuid=prefix + "A")
bare_long = dict(bare, uuid=prefix + "B")
kept_long, dropped_long = fs.prefer_complete([full_long, bare_long])
assert dropped_long == 0 and len(kept_long) == 2, (kept_long, dropped_long)

tuic_a = {"name": "ta", "type": "tuic", "server": "1.1.1.2", "port": 443,
          "uuid": prefix + "A", "password": "pw-a"}
tuic_b = dict(tuic_a, name="tb", password="pw-b")
assert fs.node_key(tuic_a) != fs.node_key(tuic_b), fs.node_key(tuic_a)

# 6.2) SIP002 明文 userinfo 中的百分号编码必须还原
n = fs.parse_uri("ss://aes-128-gcm:p%40ss%3Aword@1.2.3.4:8388#ss-percent")
assert n and n["password"] == "p@ss:word", n

# 6.3) 共享地址等非公网单播地址不应进入候选池
assert fs.is_usable_server("100.64.0.1") is False
assert fs.is_usable_server("8.8.8.8") is True

import speed_test as st

# 7) 导出侧：VLESS 必须带 encryption=none，skip-cert-verify 必须还原成 insecure 参数
n = {"name": "x", "type": "vless", "server": "1.2.3.7", "port": 443, "uuid": "u-7",
     "tls": True, "servername": "s.example.com", "skip-cert-verify": True,
     "network": "ws", "ws-opts": {"path": "/ws", "headers": {"Host": "s.example.com"}}}
uri = st.node_to_uri(n)
assert "encryption=none" in uri, uri
assert "allowInsecure=1" in uri and "insecure=1" in uri, uri
back = fs.parse_uri(uri)
assert back and back["servername"] == "s.example.com" and back["tls"] is True, back

# 8) 不带 skip-cert-verify 的节点不应凭空多出 insecure 参数
n2 = dict(n, name="y")
n2.pop("skip-cert-verify")
uri2 = st.node_to_uri(n2)
assert "insecure=1" not in uri2 and "allowInsecure=1" not in uri2, uri2

# 9) PROBE_VIA：让探活从指定代理出去（换探测视角）
st.PROBE_VIA = "socks5://user:p%40ss@10.0.0.1:1080"
v = st.via_outbound()
assert v["type"] == "socks" and v["server"] == "10.0.0.1" and v["server_port"] == 1080
assert v["username"] == "user" and v["password"] == "p@ss", v   # userinfo 要解码

st.PROBE_VIA = "http://10.0.0.2:8080"
v2 = st.via_outbound()
assert v2["type"] == "http" and v2["server_port"] == 8080, v2

st.PROBE_VIA = ""
assert st.via_outbound() is None
obs = [{"tag": "a", "type": "vless"}]
assert st.with_detour(obs, {"tag": "via"})[0]["detour"] == "via"
assert "detour" not in st.with_detour(obs, None)[0]
# 不改原对象
assert "detour" not in obs[0]

# 10) 端口自动避让：服务器上客户端常占着 7890（mihomo 混合端口）
import socket
hold = []
held_ports = set()
for port in (9090, 7890, 7900):
    sk = socket.socket()
    try:
        sk.bind(("127.0.0.1", port))
        sk.listen(1)
        hold.append(sk)
        held_ports.add(port)
    except OSError:
        sk.close()          # 已被别的东西占着，同样能验证避让
try:
    st._PROBE_PORT_CACHE.clear()
    st._VERIFY_BASE_CACHE.clear()
    assert st.probe_listen_port() != 7890 or not hold
    b = st.verify_port_base(4)
    if hold:
        assert b != 7900, b
    assert st.verify_port_base(4) == b, "同进程内必须稳定"
    assert st.probe_listen_port() == st.probe_listen_port()
    st._CONTROLLER_ADDRESS = ""
    api_address = st.controller_address()
    if 9090 in held_ports:
        assert not api_address.endswith(":9090"), api_address
finally:
    for sk in hold:
        sk.close()
    st._PROBE_PORT_CACHE.clear()
    st._VERIFY_BASE_CACHE.clear()
    st._CONTROLLER_ADDRESS = ""

# 11) Clash API 启动失败/超时必须明确报错，不能继续制造假 0 结果
import io


class _FailedProc:
    stderr = io.StringIO("listen failed")

    @staticmethod
    def poll():
        return 1


try:
    st.wait_for_clash_api(_FailedProc(), "测试实例", timeout=0.1)
    raise AssertionError("进程已退出时 wait_for_clash_api 必须抛错")
except RuntimeError as e:
    assert "测试实例" in str(e) and "listen failed" in str(e), e

# 12) 空结果不能写成一份空订阅
try:
    st.write_outputs([])
    raise AssertionError("空节点列表必须拒绝写订阅")
except RuntimeError as e:
    assert "空订阅" in str(e), e

print("解析层回归测试全部通过")
