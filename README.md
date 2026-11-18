# node

自用订阅聚合。把多个公开订阅源合并、去重、**真实请求验证**，输出一份订阅：

| 文件 | 用途 |
|---|---|
| `v2.txt` | base64 编码的分享链接列表，给 **v2rayN（sing-box 内核）** / Shadowrocket 等客户端用 |

订阅地址：

```
https://raw.githubusercontent.com/NZNL31/node/main/v2.txt
```

每 3 小时自动更新一次。

另外有两个**状态文件**，由 CI 自动维护，不用来看，也不要手改：

| 文件 | 作用 |
|---|---|
| `hk_latency.json` | 香港探测点的延迟缓存，影响排序与「免死」判定 |
| `node_health.json` | 每个节点的连续失败次数，决定谁被淘汰；删掉它等于重置淘汰状态 |

更详细的改动记录与实测数据见 [docs/optimization-report.md](docs/optimization-report.md)。

## 产物为什么只有一份

目标客户端是 v2rayN + sing-box 内核，它吃的是**分享链接**（v2rayN 自己把链接转成配置），
所以不再产出 `clash.yaml` / `singbox.json`：

- 早期版本产出过 Clash 配置，但那份产物没有 Clash 内核能做真实校验（换掉 mihomo 之后），
  留着等于发一份没人验证过的配置；
- `sing-box` 跑不了的写法（`xhttp` 传输、`SSR` 协议）在这条链路上本来也用不了，
  现在直接**从池里剔除**（实测每轮约 77 个），不会变成「导入后连不上」的噪音。

## 工作流程

```
subscriptions.json（12 个源）
      │
      ▼
fetch_subscriptions.py   ← 拉取、解析成 Clash 节点字典
      │                    单源上限 1200 条（巨型 dump 按哈希取样）
      │                    完整性清理 → 去重 → 保留信息最全的变体
      │                    all_nodes.yaml（中间产物，不入库）
      ▼
hk_probe.py              ← 借 check-host.net 的香港探测点测 TCP 延迟
      │                    每轮 30 个，结果累积在 hk_latency.json
      ▼
speed_test.py            ← 剔除 sing-box 跑不了的 → sing-box check 逐条校验
      │                    ① 延迟探活：/proxies/{name}/delay 走真实隧道
      │                    ② 协议级校验：TCP 走 204，UDP 走 STUN
      │                    ③ 连续失败计数 → 淘汰（带香港免死）
      ▼
v2.txt                   ← 只发布通过协议级校验的节点
```

任何一步失败都**不产出**：宁可这一轮不更新、保留上一版能用的订阅。

### 为什么不只看「端口通不通」

老版本用 `is_port_open()` 做 TCP 探测，对 `hysteria2`/`tuic` 这类 UDP 协议毫无意义，
还会随机淘汰好节点。现在两级验证：

| 级别 | 做什么 | 能发现什么 |
|---|---|---|
| ① 延迟探活 | sing-box 的 `/proxies/{name}/delay` 走真实代理隧道请求 `generate_204` | 节点连不上、握手失败 |
| ② 协议级校验 | TCP 协议（vless/vmess/trojan/ss）：经节点发真实 HTTPS 请求，**要求 204**<br>UDP 协议（hysteria2/tuic）：经节点发 **STUN binding 请求**，要求收到 binding success | 出口被劫持、被插页、只放行特定域名；UDP 隧道通了但转发不通 |

**为什么必须有 ②**：sing-box 的 delay 接口**不校验状态码**。实测同一个节点打
`generate_204` 和打一个必然 404 的路径都会返回延迟 —— 12/12 个节点两个路径都「通」。
也就是说 delay 只能证明隧道建立了，不能证明出口是好的。

② 用 24 个并发槽位实现（每个槽位一个 `mixed` inbound + 一个 `selector`，用
`route.rules` 把 `in-i` 钉到 `sel-i`），实测 768 个隧道通的节点里有 26 个拿不到
204 被剔除。

**UDP 协议单独测的原因**：`hysteria2` / `tuic` 是 UDP 协议，而 GitHub runner 的
UDP 出网限制很严；用 HTTPS(204) 去测它们，测的是「能不能跑 TCP」而不是「能不能
跑它自己的协议」。所以这两类走 STUN（对齐 v2rayN 自己的 `stun:` 测速类型）：
经 sing-box 的 SOCKS5 UDP 中继发 STUN binding 请求，收到 binding success 才算通过。

## 淘汰规则

延迟**永不**参与淘汰 —— 因为 CI 在美国测出来的快慢与国内无关（实测按延迟筛选把
可用率从 44% 砍到 15.6%）。但「连不上」和「出口坏掉」要淘汰：

| 规则 | 说明 |
|---|---|
| 连续 `DEAD_ROUNDS`（默认 2）轮失败 | 单轮失败可能是并发排队超时或被临时限流 |
| `MIN_KEEP`（默认 150）地板 | 删完低于这个数就整轮放弃淘汰，宁可脏也不能砍空 |
| UDP 协议豁免 | `hysteria2`/`tuic` 不淘汰 —— CI 对 UDP 出网限制很严，实测 44 个里只有 1 个能从 CI 探活成功 |
| 香港测得通的豁免 | 亚洲视角测得通的节点，即使 CI 连不上也留在池里（但它们没有 204 证据，所以本轮不发布） |

失败次数记在 `node_health.json`，跨轮保持；节点哪天通了会自动清零。淘汰掉的节点
下轮如果源里还有、且探活通过，会自己回来。

## 香港探测点

`hk_probe.py` 用 check-host.net 的 `hk1` 测「香港 → 节点」的 TCP 延迟：

- **有速率限制**：实测连续提交约 50 次后开始返回 `limit_exceeded`，所以每轮只测 30 个，
  按「最久没测过」轮转，覆盖 1200 个节点约需 2~3 天；
- **测不出墙**：GFW 在中国出境链路上，从香港探测不经过它；
- 用途：排序（`[HK 8ms]` 标记）+ 淘汰豁免。

香港数据超过 `HK_TTL_H`（默认 96 小时）就不再参与排序和免死判定。

## 输出顺序与标记

按协议分块，国内可达率高的排前面（`vless` → `trojan` → `hysteria2` → `tuic` →
`vmess` → `ss` → `ssr`），块内按香港延迟、再按 CI 延迟排序。

| 标记 | 含义 |
|---|---|
| `[HK 8ms]` | 香港探测点测得的 TCP 耗时，**最接近你的真实体验**（约 +30ms） |
| `[HK 超时]` | 香港都连不上 |
| `[US 123ms]` | 只有美国机房的数据，参考价值有限 |
| 无标记 | 两边都没测到 |

**这些都只是排序依据，不是你的实际延迟** —— 客户端里选自动测速/自动选择即可。

## 订阅源

12 个源，每个源最多收 1200 个节点（`SOURCE_LIMIT`）。加源之前看两个指标：

**1. 去重后新增**（不是解析条数）。实测例子：

| 源 | 解析 | 去重后新增 |
|---|---:|---:|
| barry-far | 8891 | 439 |
| rtwo2 | 55734 | （已移除：5.5 万条全量 dump，会冲垮探活预算） |
| peasoft | 385 | 6 |

**2. 源仓库近 7 天的提交数**。静态清单不补新节点，里面的节点失效后不会被替换。

两个指标要一起看：`peasoft` 很活跃，但独有供给只有 6 条 —— 活跃 ≠ 有独特节点。
同一个仓库的不同输出文件通常互相重叠，不必重复添加。

## 可调参数

都在 `.github/workflows/main.yml` 的 env 里：

| 变量 | 默认 | 说明 |
|---|---:|---|
| `MIN_NODES` | 50 | 解析结果低于此数让任务失败，防止源集体失效 |
| `SOURCE_LIMIT` | 1200 | 单个源最多收多少节点（0 = 不限） |
| `HK_BATCH` / `HK_TTL_H` / `HK_PRUNE_DAYS` | 30 / 96 / 30 | 香港探测批量、过期小时、孤儿条目保留天数 |
| `MAX_PROBE` | 6000 | 最多探活多少个；超限按哈希取样 |
| `PROBE_CONCURRENCY` / `PROBE_TIMEOUT_MS` | 64 / 4000 | 延迟探活的并发与超时 |
| `VERIFY_SLOTS` / `VERIFY_TIMEOUT` | 24 / 8 | 协议级校验（204 / STUN）的并发槽位与单次超时 |
| `MAX_OUTPUT` | 1500 | 输出上限，超出才按协议配额裁剪 |
| `DEAD_ROUNDS` | 2 | 连续几轮失败才淘汰（1 = 立即） |
| `MIN_KEEP` | 150 | 淘汰地板 |
| `SINGBOX_BIN` / `SINGBOX_VERSION` | `./sing-box` / 1.14.4 | 探活、校验、204 检查都靠它 |

依赖锁在 `requirements.txt`。

## 本地运行

```bash
pip install -r requirements.txt
python fetch_subscriptions.py
python hk_probe.py          # 可选，攒香港延迟数据
python test_parse.py        # 解析/导出回归（不联网）
python test_health.py       # 淘汰逻辑回归（不联网）

# 探活与 204 校验需要一个 sing-box 二进制（放仓库根目录，命名 sing-box）
#   https://github.com/SagerNet/sing-box/releases
python speed_test.py        # 没有 sing-box 会自动降级为不探活、全部保留
```

## 想知道哪些节点你这边真的能用

```bash
python check_cn.py                     # 测仓库里已发布的 v2.txt
python check_cn.py my.txt              # 测本地文件（base64 或明文链接列表）
python check_cn.py <订阅地址>           # 测任意订阅
```

它从**你自己机器**出发做 TCP 可达性探测。注意 `hysteria2` / `tuic` 是 UDP 协议，
TCP 探测对它们必然失败，脚本会单独标注并剔除。

## 注意

- `subscriptions.json` 里的订阅链接含私有 token，本仓库是公开的，**加自己的机场链接前先想清楚**。
- 公开免费节点质量参差，不要用来登录重要账号。
- 核对线上产物**不要用 `raw.githubusercontent.com` 裸链接**：CDN 会返回不同时间的缓存版本
  （实测同一时刻能抓到两个不同版本）。按 commit SHA 走 API 才权威：
  `curl -s "https://api.github.com/repos/NZNL31/node/contents/v2.txt?ref=<sha>"`。
