# node

自用订阅聚合。把多个公开订阅源合并、去重、真实探活，输出三份订阅文件：

| 文件 | 用途 |
|---|---|
| `clash.yaml` | Clash / mihomo 配置，含 `🚀 精选`、`🔄 自动选择`、`🌐 节点选择` 三个策略组 |
| `v2.txt` | base64 编码的分享链接列表，给 v2rayN / Shadowrocket 等客户端用 |
| `singbox.json` | sing-box 内核的配置，同样的三个策略组 |

另外还有两个**状态文件**，由 CI 自动维护，不用来看，也不要手改：

| 文件 | 作用 |
|---|---|
| `hk_latency.json` | 香港探测点的延迟缓存，只影响排序 |
| `node_health.json` | 每个节点的连续探活失败次数，决定谁被淘汰；删掉它等于重置淘汰状态 |

订阅地址：

```
https://raw.githubusercontent.com/NZNL31/node/main/clash.yaml
https://raw.githubusercontent.com/NZNL31/node/main/v2.txt
https://raw.githubusercontent.com/NZNL31/node/main/singbox.json
```

也可以走 Pages（如果开了）：`https://nznl31.github.io/node/clash.yaml`

每 3 小时自动更新一次。

更详细的改动记录、实测数据与已知取舍见 [docs/optimization-report.md](docs/optimization-report.md)。

## 工作流程

```
subscriptions.json
      │
      ▼
fetch_subscriptions.py   ← 拉取 7 个订阅源，解析成 Clash 节点
      │                    去重键 = 类型 + 服务器 + 端口 + 凭证 + 传输层标识
      │                    all_nodes.yaml（中间产物，不入库）
      ▼
hk_probe.py              ← 借 check-host.net 的香港探测点测 TCP 延迟
      │                    每轮只测一小批，结果累积在 hk_latency.json
      ▼
speed_test.py            ← 起 sing-box，逐个节点走真实代理探活
      │                    失败次数累积在 node_health.json（跨轮保持）
      │                    连续 N 轮失败的淘汰，延迟只用于排序
      ▼
校验门禁                 ← 结构自检（悬空引用、条数一致性）
      │                    + sing-box check 加载最终 singbox.json
      ▼
clash.yaml + v2.txt + singbox.json
```

任何一步失败都**不产出**：宁可这一轮不更新、保留上一版能用的订阅，也不要发布
一份加载不了的配置。

### fetch_subscriptions.py

按顺序识别三种订阅格式：

1. Clash YAML（含 `proxies:`）
2. 整份 base64 编码的 URI 列表（最常见的订阅格式）
3. 明文 URI 列表

支持 `vmess` / `vless` / `trojan` / `ss` / `ssr` / `hysteria2` / `tuic`。
节点去重键是 `类型 + 服务器 + 端口 + 凭证`，ws/grpc 还要带上传输层标识
（`Host`/`path`、`serviceName`）—— CDN 后面常见同一个 `server:port`、同一个 UUID、
只靠 Host 区分的几十个节点，只按凭证去重会把它们合并成一个。重名节点自动加序号
后缀（重名会让 Clash 直接拒绝加载配置）。

#### 残缺节点是怎么来的、怎么清掉的

最常见的「节点填好了却连不上」不是导出丢字段，而是**同一个节点被不同源写成了
不同完整度**，而我们过去把两版都发布了。实例：

| 源 | 写法 | 结果 |
|---|---|---|
| 某个源 | `security=none&type=tcp` | 无 TLS、无 SNI、无 ws —— 必然连不上 |
| 另一个源 | `security=tls&sni=…&type=ws&host=…` | 完整 |

三条规则合起来处理这件事（`test_parse.py` 里都有对应用例）：

1. **空值参数不能让后面的键轮不到。** `?sni=&host=cdn.example.com` 过去会取到空
   串当作 SNI，于是 host 永远轮不到。现在 `qget()` 跳过空值。
2. **TLS 开着就必须有 SNI。** 缺失时按「ws 的 `Host` → 服务器地址」兜底 ——
   没有 SNI 的握手在 CDN / 多域名前置上直接失败。
3. **残缺变体在去重之前清理。** 同一 `协议 + 服务器 + 端口 + 凭证` 有多个写法时，
   只保留信息最全的那一版（`prefer_complete()`），只丢**严格更弱**的，完整度相同
   的一律保留。实测这一步在 2505 个解析结果里清掉 130 个。

另外 `xtls-rprx-vision` 是 TLS 上的协议，**带 flow 却没有 TLS 的节点物理上不可能
连通**（实测 34 个，多半是源把 `security=tls` 剥掉了），这类直接拒绝。

> 注意顺序：这一步必须在去重**之前**做。去重是先到先得，先到的常常正是残缺版本，
> 之后再比对就没有参照物了 —— 放在后面只能清掉 17 个，放在前面能清掉 130 个。


### speed_test.py

起一个 sing-box 实例，调用它提供的 Clash 兼容接口 `/proxies/{name}/delay`，让
**真实代理链路**去请求 `generate_204`，拿到的是真实延迟，不是随机数。首轮失败的
节点会补测一轮（并发 32 时总有一批是排队超时，不是真死）。

用 sing-box 而不是 mihomo，是因为它同时能干这两件事：

| 用途 | sing-box 的能力 |
|---|---|
| 探活 | `experimental.clash_api` 提供 `/proxies` 和 `/proxies/{name}/delay`，语义与 mihomo 一致（失败同样是 503/504），探活代码一行不用改 |
| 校验 | `sing-box check -c singbox.json` 真实加载一遍配置 |

代价是 sing-box 表达不了的节点：`xhttp` 传输（mihomo 专有）和 `SSR` 协议
（sing-box 已移除）。这些节点**只是不参与探活、不进 singbox.json**，在
`clash.yaml` 和 `v2.txt` 里照常发布 —— 它们在 Clash 客户端那边是能用的，没有理由
因为探活引擎不认识就丢掉。实测约占全量的 1%（1283 个里 14 个）。

还有三处取值差异，转换时会自动归一化：

| 差异 | 处理 |
|---|---|
| `flow: xtls-rprx-vision-udp443` | sing-box 只认 `xtls-rprx-vision`，去掉后缀 |
| uTLS 指纹 `unsafe` 等 | sing-box 只认白名单，兜底改成 `chrome` |
| 无 `reality-opts` 但要求 TLS | REALITY 在 sing-box 里强制要求 uTLS，自动补上 |

sing-box 不可用时降级为「不探活、保留全部节点」并打印警告，不会把订阅写空。

#### 关键结论：CI 的延迟不能用来筛节点

测速机器是 GitHub Actions 的 runner，在美国机房，所以它测的是
「美国 → 节点 → 目标站」的往返时间。这个数字跟**国内能不能连上**几乎不相关，
甚至是负相关 —— 便宜的美西 VPS 对美国快、对国内慢；做了国内线路优化的节点反过来。

2026-10-24 用同一份节点池、同一台国内机器做 TCP 复测：

| 节点池 | 数量 | 国内可达 | 可用率 |
|---|---:|---:|---:|
| 原始池（不筛选） | 925 | 407 | **44.0%** |
| CI 按延迟筛出的 | 199 | 31 | **15.6%** |

也就是说「按延迟筛选」把可用率从 44% 砍到 15.6%，筛掉的正是国内能用的那些。
所以 **延迟永远不参与淘汰**，只做两件事：在同类节点里排序、在名字里打
`[US 123ms]` 标记。

#### 但「连不上」要淘汰

延迟不淘汰 ≠ 什么都不淘汰。一个节点如果从 CI 起真实代理请求都建立不了连接，
留着它只会让客户端的 url-test 白跑一遍，所以这条要管：

| 淘汰条件 | 说明 |
|---|---|
| 连续 `DEAD_ROUNDS`（默认 2）轮真实探活都失败 | 单轮失败可能是并发排队超时或被临时限流，给一轮容错 |
| 有 `MIN_KEEP`（默认 150）地板 | 删完低于这个数就整轮放弃淘汰，宁可脏也不能把订阅砍空 |
| 没被探活的节点不参与判定 | sing-box 表达不了的 `xhttp`/`SSR` 从没被测过，不能因为「不可测」当成死 |
| UDP 协议豁免 | `hysteria2` / `tuic` 不淘汰 —— CI 对 UDP 出网限制很严，实测 hysteria2 44 个里只有 1 个能从 CI 探活成功，而它在国内往往最好用 |
| **香港测得通的豁免** | 香港探测点（亚洲视角）测得通的节点，即使 CI 连不上也保留 —— 「CI 到不了」不等于「亚洲到不了」。实测这条救回 113 个被误杀的节点（当时池子的 30%） |

实测这一刀很重：1195 个节点里 CI 侧探活失败的有 823 个，稳态之后池子会从
1200 左右收敛到 **300~400**。这就是为什么它必须带容错轮次、地板和协议豁免，
而不是「失败一次就删」。

失败次数记在 `node_health.json`（跟 hk_latency.json 一样提交回仓库），所以
淘汰状态跨轮保持；节点哪天通了会自动清零并回来。

淘汰之后池子的协议构成会明显偏向 `ss`（当前 371 个里 ss 占 177 个、
vless 只剩 104），因为 ss 是裸 TCP，从美国最容易探通，而 vless/reality 常被
地域限制。这与「ss 国内存活率只有 7.3%」是矛盾的 —— 属于这套淘汰策略的固有
偏差。缓解办法是压低 `MAX_OUTPUT` 让协议配额裁剪生效（`TYPE_WEIGHT` 里
ss 权重 0.25），而不是让它全量输出。

#### 为什么不能干脆「在云端测国内可达性」

能想到的最直接的办法是换个离中国近的探测点，但这条路走不通：

- **GitHub Actions 的 runner 全在境外**（美国/欧洲/Azure 的几个区），没有中国大陆的。
- **免费的第三方多节点探测服务也没有中国大陆节点。** 实测 check-host.net 一共
  51 个探测点，亚洲只有 `hk1`（香港）、`jp1`（东京）、`sg1`（新加坡），没有 `cn*`。
- 就算有香港节点也没用。GFW 部署在中国**出境**链路上，封锁发生在流量离开中国的
  那一刻。从香港探测走的是「香港 → 节点」，压根不经过 GFW，测不出封锁。

所以「云端测国内可用性」在免费方案里做不到。真正有效的做法是**不在云端判断**：
把节点池完整发出去，让身处国内的客户端自己去测 —— 见下面的「自动选择」说明。
客户端是唯一免费的、真正位于国内的观测点，而且它不需要你手动操作。

如果确实需要云端侧的国内探测（比如想在 CI 里做统计），唯一可行的是**国内机房的
机器**：腾讯云 SCF / 阿里云 FC 这类云函数，或者一台轻量应用服务器挂成 self-hosted
runner。都要实名认证和部署，不是零成本，本项目没有采用。

#### 退一步：用香港探测点当跳板

香港测不出墙，但它能测出**「这个节点离中国有多远」**，这本身就有价值：

```
节点在香港测得   3ms  → 就是香港本地的 CDN 边缘，国内 RTT 约 30~40ms
节点在香港测得  70ms  → 大概率在新加坡/日本
节点在香港测得 220ms  → 大概率在欧洲/美国
```

换算关系大致是「国内 RTT ≈ 香港 RTT + 30ms」（中国→香港这一段本身约 20~40ms）。
这比 CI 在美国测出来的值有用得多，所以 `hk_probe.py` 用 check-host.net 的
`hk1.node.check-host.net` 探测点来做这件事，结果存在 `hk_latency.json` 里，
`speed_test.py` 排序时优先参考它。

两个限制要说清楚：

1. **有速率限制。** check-host.net 是免费公共服务，实测连续提交约 50 次后开始
   返回 `{"error":"limit_exceeded"}`。所以每轮只测 30 个，按「最久没测过」优先
   轮转，大约 3~4 天才覆盖全量。缓存只影响排序，不影响节点是否发布，所以数据
   旧一点没关系。
2. **只排序，不淘汰。** 它测不出墙，一个在国内被墙的节点从香港看照样是通的。

#### 输出顺序

按协议分块，国内可达率高的排前面：

| 协议 | 国内可达率（TCP 复测） |
|---|---:|
| vless | 56.8% (359/632) |
| vmess | 56.1% (32/57) |
| hysteria2 / tuic | UDP 协议，TCP 探测测不准，未知 |
| ss | 7.3% (13/178) |
| ssr | — |

裸 `ss` 没有 TLS 伪装、流量特征明显，是 GFW 打击的重点，国内存活率只有 vless
的八分之一。所以 `vless` 排最前、`ss`/`ssr` 排最后。

块内的顺序依次是：

1. 香港测得通的（按香港延迟升序）—— 最接近国内真实体验
2. 香港没测过、但 CI 探活的（按 CI 延迟升序）
3. 两边都没数据的
4. 香港明确连不上的 —— 大概率是死节点，垫底

#### 关于节点名里的 `[HK 8ms]` / `[US 123ms]`

| 标记 | 含义 |
|---|---|
| `[HK 8ms]` | 从香港探测点测得的 TCP 连接耗时，**最接近你的真实体验**（约 +30ms） |
| `[HK 超时]` | 香港都连不上，基本可以确定是死节点 |
| `[US 123ms]` | 只有美国机房的数据，参考价值有限 |
| 无标记 | 两边都没测到，不代表国内不能用 |

香港数据超过 `HK_TTL_H`（默认 96 小时）就不再参与排序、也不打标签 ——
旧的 8ms 和今天实测的 30ms 混在一起排，前者会被无条件排到最前，标签却还写着
`[HK 8ms]`，那是在骗自己。过期一律按「没数据」处理。

**这些都不是你的延迟。** 真正该看的延迟是客户端自己测的。三个组的分工：

| 组 | 类型 | 用途 |
|---|---|---|
| `🚀 精选` | url-test，前 30 个节点 | **默认选它**。健康检查只覆盖 30 个节点，手机上最省电省流量 |
| `🔄 自动选择` | url-test，全部节点 | 精选组里没你想要的节点时，退到这里全量比较 |
| `🌐 节点选择` | select | 手动指定单个节点 |

`🔄 自动选择` 包住全部节点，而它一旦被选中，`lazy` 就不起作用了，会真的每
10 分钟对全部节点做健康检查。所以别把它当默认 —— 精选组存在的唯一理由就是这个。

#### 规则：国内直连

`clash.yaml`：

```yaml
rules:
  - GEOIP,LAN,DIRECT,no-resolve
  - GEOIP,CN,DIRECT
  - MATCH,🌐 节点选择
```

`singbox.json` 里没有对应写法 —— **`geoip` 规则在 sing-box 1.12 被移除了**，
1.14 加载会直接 FATAL。剩下的两条路是远程 `rule_set`（首次启动要联网下载，
节点全挂时下不下来就起不来）和域名后缀。这里选后者，不依赖任何下载：

```json
{"domain_suffix": ["cn", "baidu.com", "qq.com", "bilibili.com", ...],
 "outbound": "direct"}
```

代价是它只认域名，不认 IP —— 直接访问国内 IP 的流量仍会走代理。对日常使用
（访问的都是域名）够用，而且配置永远能加载。

另外 sing-box 1.12 起不允许隐式解析 outbound 的 server 域名，配置里必须有
`route.default_domain_resolver`，否则整个配置加载失败 —— 这是从 mihomo 迁过来
最容易踩的一个坑。

早期版本只有一条 `MATCH`，等于把国内站点也塞进代理，白付一份延迟和机场流量。
DNS 走 fake-ip + 国内 DoH（clash.yaml）/ 本地 DoH + 域名规则（singbox.json）。

### v2.txt 的导出规则

`clash.yaml` 是完整节点字典，字段不会丢；`v2.txt` 要把节点**还原成分享链接**，
还原时漏一个字段，客户端拿到的就是一个连不上的节点。几个容易漏的：

| 字段 | 漏了会怎样 |
|---|---|
| vless 的 `reality-opts`（`pbk` / `sid`） | 导出成 `security=tls`，客户端按普通 TLS 握手，**必然失败** |
| `client-fingerprint`（`fp`） | REALITY/TLS 指纹伪装缺失，握手失败或特征暴露 |
| grpc 的 `serviceName` | 走 grpc 的节点全部连不上 |
| trojan 的 `ws-opts` | 变成裸 TCP + TLS，握手失败 |
| IPv6 主机的方括号 `[::1]` | `host:port` 被按多个冒号切错，解析不出端口 |

自检方式是用自己的解析器把导出的 URI 再解析一遍，断言关键字段还在：

```python
back = fetch_subscriptions.parse_uri(speed_test.node_to_uri(node))
assert back["reality-opts"] == node["reality-opts"]
```

每次发布前都会用同样的路径回灌一遍，全量无损（CI 的「校验产物」步骤里有
条数一致性检查）。

> 方向要说清楚：**`v2.txt` 是从 `clash.yaml` 单向生成的**（`node_to_uri()`），
> 不是反过来。所以「`v2.txt` 里的 URI 有 `sni=`、Clash 配置里却没 SNI」不可能
> 是这一步丢的 —— 只可能是 Clash 侧那条节点本身来自一个残缺的源变体，
> 见上面「残缺节点是怎么来的」。

### 校验门禁

`clash.yaml` / `singbox.json` 里的规则或组类型写错，在客户端上就是**整个订阅
加载失败**，所以 CI 在提交前过两道关：

1. 结构自检（Python）：有没有节点、代理组里有没有悬空引用、`v2.txt` 条数与
   `clash.yaml` 节点数是否一致、`singbox.json` 的 outbound tag 是否唯一且引用
   不悬空、节点数是否在合理区间、`hk_latency.json` 格式对不对。
2. `./sing-box check -c singbox.json`：用 CI 里已经下好的**同一个版本**真正加载
   一遍。只校验纯 proxies 的 `probe.json` 是不够的 —— 规则、DNS、组是另一半。

`speed_test.py` 里 sing-box 加载失败又定位不到问题节点时，直接 `exit 1` 本轮不产出，
不发布未校验的订阅。

**一个诚实的取舍**：换掉 mihomo 之后，`clash.yaml` 不再被任何 Clash 内核真实加载
验证了（Python 只能检查结构）。好在两份产物出自同一份节点字典，`singbox.json`
被 `sing-box check` 通过，等于这些节点的字段被真内核验证过；差异只在规则/组这些
静态结构上，而它们由 `build_clash_config()` 固定生成。如果哪天要动那个函数，
建议本地拿 mihomo `-t` 抽查一次。

## 加订阅源

编辑 `subscriptions.json`：

```json
{
  "subscriptions": [
    "https://example.com/sub?token=xxx"
  ]
}
```

优先级：同一台机器 + 同一端口 + 同一凭证只保留一条，所以**多源重复不会浪费名额**，
按顺序先出现的源赢。想让自己最在意的源优先，把它放到数组前面。

不支持 `https`（HTTP 代理）/ `socks5` / `hysteria`(v1) / `anytls` 这几类，遇到会
静默跳过 —— 例如 `kooker.jp.txt` 的 740 行里有约 78 行是这些类型。

### 加源之前先看两个指标

**别只看「解析出多少条」，要看「去重后新增多少条」。** 实测近 16 轮：

| 源 | 解析 | 去重后新增 | 说明 |
|---|---:|---:|---|
| chengaopan | 401 | 373 | 主力 |
| free18 | 410 | 321 | 主力，波动大（85~351） |
| kooker | 655 | 241 | 主力，每小时重建 |
| mahdibland | 200 | 108 | 稳定 |
| ermaozi | 489 | 96 | 重叠多 |
| dalazhi | 18 | 15 | 小但干净 |
| peasoft | 385 | **6** | 高度重叠，几乎无独有供给 |

反面例子：`PuddinCat/BestClash`（解析 16 条、新增 **1** 条）和
`Pawdroid/Free-servers`（解析 14 条、新增 **1** 条）已因此被移除 ——
删掉它们只损失 1 个节点（1161 → 1160），省两次请求。

第二个指标是**活跃度**：源仓库近 7 天的提交数。静态清单（几个月不变的文件）不会
补充新节点，里面的节点失效后不会被替换，属于**结构性衰减**；活跃源（像
`kooker/FreeSubsCheck` 近 7 天 100 次提交、每小时重建）才有持续供给。

注意这两个指标要一起看：`peasoft` 近 7 天有 20 次提交（很活跃），但独有供给只有
6 条 —— 活跃 ≠ 有独特节点，只是把同一批节点又列了一遍。

同一个仓库的不同输出文件通常互相重叠（`kooker` 的 `mihomo.yaml`、`base64.txt`
与 `kooker.jp.txt` 是同一批节点），**不必重复添加**。

## 可调参数

都在 `.github/workflows/main.yml` 的 env 里：

| 变量 | 默认 | 说明 |
|---|---:|---|
| `MIN_NODES` | 50 | 解析结果低于此数直接让任务失败，防止订阅源集体失效时发布空配置 |
| `HK_BATCH` | 30 | 每轮最多测多少个节点的香港延迟（受 check-host.net 限流约束） |
| `HK_TTL_H` | 96 | 香港延迟缓存多少小时算过期。过期数据既不参与排序也不打标签 |
| `HK_PRUNE_DAYS` | 30 | 已不在节点池里的缓存条目再保留多少天，超过就删（否则文件只进不出） |
| `DEAD_ROUNDS` | 2 | 连续几轮真实探活失败才淘汰。设 `1` = 单轮失败即删（激进，约 6 小时收敛到 300 个） |
| `MIN_KEEP` | 150 | 淘汰地板：删完低于这个数就整轮放弃淘汰 |
| `MAX_PROBE` | 2500 | 最多探活多少个节点；超限时按哈希取样，而不是砍掉尾部（尾部永远是最后几个源） |
| `PROBE_CONCURRENCY` | 32 | 探活并发数 |
| `MAX_OUTPUT` | 1500 | 最多输出多少个节点。节点数没超这个值时**全部输出**，只有超了才按协议配额裁剪 |
| `FEATURED_SIZE` | 30 | `🚀 精选` 组放多少个节点 |
| `SINGBOX_BIN` | `./sing-box` | sing-box 可执行文件路径（探活与校验都靠它） |
| `SINGBOX_VERSION` | 1.14.4 | CI 下载的 sing-box 版本，建议和客户端大版本对齐 |

依赖锁在 `requirements.txt` 里。pyyaml 的大版本升级会改变 `yaml.dump` 的排版，
让「内容没变」也产生一次全量 diff，把每 3 小时的提交变成噪音。

## 本地运行

```bash
pip install -r requirements.txt
python fetch_subscriptions.py
python hk_probe.py          # 可选，攒香港延迟数据

# 探活需要一个 sing-box 二进制（放到仓库根目录并改名为 sing-box）
#   https://github.com/SagerNet/sing-box/releases
python speed_test.py        # 没有 sing-box 会自动降级为不探活、全部保留
python test_health.py       # 淘汰逻辑的回归测试（不联网）
```

## 想知道哪些节点你这边真的能用

跑 `check_cn.py`，它从**你自己机器**出发做 TCP 可达性探测：

```bash
python check_cn.py                     # 测仓库里已发布的 clash.yaml
python check_cn.py my.yaml             # 测本地文件
python check_cn.py <订阅地址>           # 测任意订阅
```

输出会按协议分组给出可达率。注意 `hysteria2` / `tuic` 是 UDP 协议，TCP 探测
对它们必然失败，脚本会单独标注并剔除。TCP 连不上就一定不能用，能连上也不保证
一定好用 —— 但用它筛掉死节点足够了。

这一步在有了淘汰机制之后更有意义：**淘汰是按 CI 视角判的**（美国机房能不能连上），
而 `check_cn.py` 是从你这里判的。两个视角不一致是常态 —— 想确认「我这边到底哪些
能用」，跑它；想只保留自己机器能连上的节点，就按它的结果手写过滤，或者把
`node_health.json` 重置后调高 `DEAD_ROUNDS` 让淘汰更宽松。

## 注意

- `subscriptions.json` 里的订阅链接含私有 token，本仓库是公开的，**加自己的机场链接前先想清楚**。
- 公开免费节点质量参差，不要用来登录重要账号。
- 客户端里选 `🚀 精选`，不要手动挑节点 —— 节点名里的延迟是美国/香港机房测的，参考价值有限。
- `clash.yaml` 的 `allow-lan` 是 `false`：配置是公开的，开 `true` 等于在局域网里
  暴露出一个无鉴权的代理端口（公司网、咖啡厅尤其危险）。要在多设备间共享再自己改。
- `singbox.json` 需要 **sing-box ≥ 1.12**（它用到了 `route.default_domain_resolver`；
  而 `geoip` 规则在那个版本已经移除，所以国内直连改用域名后缀）。客户端版本太旧会
  直接报「配置加载失败」。
