# node 项目优化报告

> 时间：2026-11-18　对应提交：`93348c7`（产物）/ `3086ce0`（代码）
> 一句话结论：项目**不需要重构**（原始设计有实测依据、CI 长期全绿），但存在若干
> 「跑了不报错、结果静默错」的问题；本轮全部定位并修复，并补上回归测试与真内核校验。

---

## 0. 现状快照

| 产物 | 规模 | 用途 |
|---|---|---|
| `clash.yaml` | 381 节点 | Clash / mihomo / Clash Verge |
| `v2.txt` | 381 条 base64 URI | v2rayN / Shadowrocket |
| `singbox.json` | 351 个 outbound | sing-box ≥ 1.12 |

协议构成：`ss 178 · vless 107 · hysteria2 48 · vmess 44 · trojan 2 · ssr 2`

状态文件：`node_health.json` 814 条（其中 802 条已达淘汰阈值）、`hk_latency.json` 1597 条。

---

## 1. 起始审计：三个「静默错误」

### 1.1 trojan 协议几乎全丢（229 → 1）

`parse_trojan()` 判断 `u.password`，但标准写法 `trojan://password@host:port` 的
userinfo 没有冒号，`urlparse` 把它放进 `username`。8 个源实测：

| 协议 | 源里 | 旧代码解析出 | 丢失率 |
|---|---:|---:|---:|
| trojan | 229 | **1** | **99.6%** |
| vless | 1101 | 1101 | 0% |
| ss | 435 | 435 | 0% |

### 1.2 `v2.txt` 里 27% 的 REALITY 节点是坏的

`node_to_uri()` 的 vless 分支不导出 `reality-opts` / `client-fingerprint`，只写
`security=tls`。客户端按普通 TLS 握手，**必然失败**。849 个节点里 230 个（27%）受影响，
而 REALITY 恰是国内存活率最高的一类。

### 1.3 其他导出缺陷

| 缺陷 | 影响 |
|---|---|
| grpc 的 `serviceName` 丢失 | 43 个 grpc 节点全部连不上 |
| vmess 的 grpc serviceName 被塞进 `path` | v2rayN 不读该字段 |
| trojan 丢 `ws-opts` | 退化成裸 TCP + TLS |
| IPv6 主机未加方括号 | `host:port` 被按多个冒号切错 |
| 密码含 `@ : #` 未转义 | URI 断裂 |
| **缺 `encryption=none`** | VLESS 规范要求，缺了部分客户端直接拒绝 |
| **缺 `insecure=1` / `allowInsecure=1`** | 客户端强制校验证书；免费节点几乎全是自签/CDN 不匹配 → 握手失败 |

后两项是最后一轮才发现并修掉的（见 §3.3）。

---

## 2. 配置与工程层面的问题

| 问题 | 处理 |
|---|---|
| `allow-lan: true` + 7890 无鉴权 | 改为 `false`：公开配置 + 无鉴权端口 = 局域网开放代理 |
| 规则只有 `MATCH` | 加 `GEOIP,LAN/CN → DIRECT`，国内站点不再走代理 |
| 无 DNS 段 | 加 fake-ip + 国内 DoH |
| 无 `unified-delay` / `tcp-concurrent` | 补上（url-test 排序公平、并发建连） |
| `🔄 自动选择` 包住全部节点且默认选中 | `lazy` 失效 → 手机上每 10 分钟对上千节点做健康检查。新增 `🚀 精选`（前 30）并设为默认 |
| 香港延迟无 TTL 检查 | 过期数据不再参与排序/打标签（旧的 8ms 会无条件压过今天实测的 30ms） |
| `hk_latency.json` 只进不出 | 加裁剪（孤儿条目保留 30 天）；实测 1371 条里 1119 条对应的节点早已不在池中 |
| 产物无真内核校验 | CI 增加 `sing-box check -c singbox.json` |
| 依赖未锁 | 锁进 `requirements.txt` + CI 开 pip 缓存 |
| 零测试 | 新增 `test_parse.py`（8 组断言）、`test_health.py`（6 组断言），接入 CI |
| `MAX_PROBE` 截断砍尾部 | 改哈希取样，避免只测前几个源 |

---

## 3. 两轮后续迭代中发现的问题

### 3.1 换内核（mihomo → sing-box）

sing-box 的 `experimental.clash_api` 与 mihomo 的 `/proxies/{name}/delay` 语义一致
（失败同样 503/504），所以探活代码未改。但真内核抓出 4 个只有它才报的坑：

| 报错 | 原因 | 处理 |
|---|---|---|
| `geoip database is deprecated … removed in 1.12` | `geoip` 规则被移除，FATAL | 国内直连改域名后缀列表（不依赖运行时下载） |
| `missing route.default_domain_resolver` | 1.12 起禁止隐式解析 outbound 域名 | 显式指定 |
| `missing domain resolver for domain server address` | DoH 服务器本身是域名 | 加 `local` 引导解析器 + `domain_resolver` |
| `independent_cache is deprecated in 1.14` | 废弃项 | 删除 |

表达不了的节点（`xhttp` 13 个、`SSR` 2 个）**不进入 singbox.json，但仍在 clash/v2 中发布**。

### 3.2 淘汰机制（用户要求「连不上的要淘汰」）

延迟永不参与淘汰（44% vs 15.6% 的实测教训），但**连接失败**参与：

| 规则 | 说明 |
|---|---|
| 连续 `DEAD_ROUNDS`（默认 2）轮探活失败 | 单轮失败可能是并发排队超时 |
| `MIN_KEEP=150` 地板 | 删完低于此数整轮放弃 |
| 不可测节点不判死 | `xhttp`/`SSR` 从未被测过 |
| **UDP 协议豁免** | `hysteria2` 44 个里只有 1 个能从 CI 探活成功（GitHub runner 限制 UDP 出网）→ 系统性误杀 |

实现中修掉两个坑：

1. **同端点多节点同轮重复计数** —— 健康记录以 `server:port` 为键，同端点上的多个
   节点同轮各累加一次，把「连续 2 轮」变成「同轮 2 次」，首轮误杀 38 个端点。
2. **淘汰后清空计数导致抖动** —— 淘汰后若删掉失败计数，下轮它会以「首次失败」复活。
   改为继续累计。

收敛过程：`1195 → 313（首轮收敛）→ 371（UDP 豁免后）→ 549（修完 SNI 后）`。

### 3.3 残缺变体与导出参数（用户实锤）

用户抽查发现多个节点「配置里 SNI 为空、甚至没有 network」。根因不是导出（`v2.txt`
是从 `clash.yaml` 单向生成的），而是**同一节点被不同源写成不同完整度，两版都发布了**：

| 源 | 写法 | 结果 |
|---|---|---|
| 某源 | `security=none&type=tcp` | 无 TLS / 无 SNI / 无 ws → 必然连不上 |
| 另一源 | `security=tls&sni=…&type=ws&host=…` | 完整 |

修复四条：

1. `qget()` 跳过空值 —— `?sni=&host=x` 过去取到空串当 SNI，host 永远轮不到
2. TLS 开启必须有 SNI —— 按 `ws Host → 服务器地址` 兜底
3. `prefer_complete()` —— 同 `协议+服务器+端口+凭证` 只留信息最全的，只丢严格更弱的
4. 拒绝 `flow` 而无 TLS 的 vless（`xtls-rprx-vision` 是 TLS 上的协议，物理上不可通）

**关键顺序**：清理必须在去重之前。去重先到先得，先到的常常正是残缺版本 —— 放在后面
只能清掉 17 个，放前面能清掉 130 个。

随后又补齐导出侧两个参数（`encryption=none`、`insecure/allowInsecure`），线上核对：

| 检查项 | 结果 |
|---|---|
| vless URI 带 `encryption=none` | 107 / 107 |
| `skip-cert-verify=True` 的 vless 95 个 ↔ 带 `insecure=1` 的 URI | 95 / 95 |
| trojan + hysteria2 带 `insecure=1` | 50 / 50 |
| 缺 SNI / flow 无 TLS / 有 ws-opts 无 network | 0 / 0 / 0 |

> **反向验证**：修完 SNI 后节点池从 371 涨到 549 —— 说明此前有一批节点是被我们
> 自己的解析 bug 判死并淘汰掉的，不是真的死。

---

## 4. 当前架构

```
subscriptions.json（9 个源）
      ▼
fetch_subscriptions.py   解析 → 完整性清理 → 去重 → 完整度优先保留
      ▼                  all_nodes.yaml（中间产物）
hk_probe.py              check-host.net 香港探测点，30 个/轮，累积 hk_latency.json
      ▼
speed_test.py            起 sing-box → /proxies/{name}/delay 真实探活
      │                  连续失败计数 → node_health.json → 淘汰
      ▼
校验门禁                 结构自检 + sing-box check
      ▼
clash.yaml + v2.txt + singbox.json
```

## 5. 可调参数

| 变量 | 默认 | 说明 |
|---|---:|---|
| `MIN_NODES` | 50 | 解析结果低于此数让任务失败 |
| `HK_BATCH` / `HK_TTL_H` / `HK_PRUNE_DAYS` | 30 / 96 / 30 | 香港探测批量、过期小时、孤儿保留天数 |
| `MAX_PROBE` / `PROBE_CONCURRENCY` | 2500 / 32 | 探活上限与并发 |
| `MAX_OUTPUT` | 1500 | 输出上限，超出才按协议配额裁剪 |
| `FEATURED_SIZE` | 30 | `🚀 精选` 组大小 |
| `DEAD_ROUNDS` | 2 | 连续几轮失败才淘汰（1 = 立即） |
| `MIN_KEEP` | 150 | 淘汰地板 |
| `SINGBOX_BIN` / `SINGBOX_VERSION` | `./sing-box` / 1.14.4 | 探活与校验内核 |

## 6. 已知取舍（不打算隐藏的部分）

1. **池子偏向 `ss`**：淘汰后 ss 占比常达 45%~55%，而实测 ss 国内存活率只有 7.3%
   （vless 56.8%）。原因是 ss 是裸 TCP，从美国最容易探通。缓解办法是压低
   `MAX_OUTPUT` 让 `TYPE_WEIGHT`（ss 权重 0.25）参与配额裁剪。
2. **`clash.yaml` 不再被 Clash 内核校验**：CI 只做结构自检。两份产物同源，
   `singbox.json` 过 `sing-box check` 覆盖了节点字段；差异在规则/组这些静态结构上。
   若要改 `build_clash_config()`，建议本地 `mihomo -t` 抽查一次。
3. **淘汰按 CI 视角判定**：与国内实际可用性不一致是常态，用 `check_cn.py` 自查。
4. **3 小时窗口**：上游刚删的节点最长会残留 3 小时（实测当前已发布节点 0 个属于
   永久残留）。要更鲜可把 cron 改成每小时。
5. **UDP 协议不淘汰**：CI 测不了 UDP 出网，宁可留脏数据也不误杀。

## 7. 验证方法

```bash
python test_parse.py    # 解析/导出回归（不联网）
python test_health.py   # 淘汰逻辑回归（不联网）
python check_cn.py      # 从本机复测已发布订阅的国内可达率
```

核对线上产物时**不要用 `raw.githubusercontent.com` 裸链接**——它的 CDN 会返回不同
时间的缓存版本（实测同一时刻能抓到 549 和 381 两个版本）。按 commit SHA 走 API 才权威：

```bash
curl -s "https://api.github.com/repos/NZNL31/node/contents/clash.yaml?ref=<sha>"
```

## 8. 时间线

| 提交 | 内容 |
|---|---|
| `bfe532e` | 首轮修复：trojan 解析、REALITY/gRPC 导出、去重键、密码转义、SSR 参数 |
| `ff041da` | 新增 kooker 订阅源（+242 个独有节点），上限放宽 |
| `da49429` | 探活与校验改用 sing-box，新增 `singbox.json` |
| `25fc85b` | 淘汰连续探活失败的节点 |
| `a4f6bdb` | 同端点同轮去重计数 |
| `fa6b7e3` | hysteria2/tuic 不参与淘汰 |
| `23542e4` | README 同步淘汰机制、修正过期数字 |
| `07f19bc` | 清理残缺变体（SNI 空、flow 无 TLS 的根因） |
| `3086ce0` | 导出补齐 `encryption=none` / `insecure` 参数 |

---

## 9. 三条可复用的经验

1. **「跑通」不等于「正确」**：trojan 全丢、REALITY 丢字段、SNI 为空，三者都不会报错，
   只会表现为「节点连不上」。用往返自测（导出再解析，断言字段）才抓得住。
2. **处理顺序本身就是逻辑**：完整性清理放在去重之后只能清 17 个，放在之前能清 130 个。
3. **校验器要选对**：`mihomo -t` 发现不了 `geoip` 被移除，`sing-box check` 才发现；
   反过来 `sing-box` 不认 `xtls-rprx-vision-udp443` 而 mihomo 认 —— 校验器和产物格式
   必须配对。
