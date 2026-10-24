# node

自用订阅聚合。把多个公开订阅源合并、去重、真实探活，输出两份订阅文件：

| 文件 | 用途 |
|---|---|
| `clash.yaml` | Clash / mihomo 配置，含 `🌐 节点选择` 和 `🔄 自动选择` 两个策略组 |
| `v2.txt` | base64 编码的分享链接列表，给 v2rayN / Shadowrocket 等客户端用 |

订阅地址：

```
https://raw.githubusercontent.com/NZNL31/node/main/clash.yaml
https://raw.githubusercontent.com/NZNL31/node/main/v2.txt
```

也可以走 Pages（如果开了）：`https://nznl31.github.io/node/clash.yaml`

每 3 小时自动更新一次。

## 工作流程

```
subscriptions.json
      │
      ▼
fetch_subscriptions.py   ← 拉取 8 个订阅源，解析成 Clash 节点
      │                    all_nodes.yaml（中间产物，不入库）
      ▼
speed_test.py            ← 启动 mihomo 逐个节点走真实代理探活
      │                    探活只用来排序和打标记，不淘汰节点
      ▼
clash.yaml + v2.txt      ← 提交回仓库
```

### fetch_subscriptions.py

按顺序识别三种订阅格式：

1. Clash YAML（含 `proxies:`）
2. 整份 base64 编码的 URI 列表（最常见的订阅格式）
3. 明文 URI 列表

支持 `vmess` / `vless` / `trojan` / `ss` / `ssr` / `hysteria2` / `tuic`。
节点按 `类型 + 服务器 + 端口 + 凭证` 去重，重名节点自动加序号后缀
（重名会让 Clash 直接拒绝加载配置）。

### speed_test.py

起一个 mihomo 实例，调用 `/proxies/{name}/delay` 让**真实代理链路**去请求
`generate_204`，拿到的是真实延迟，不是随机数。首轮失败的节点会补测一轮
（并发 32 时总有一批是排队超时，不是真死）。

只有 mihomo `-t` 校验不通过的节点会被摘掉（字段不合法会让整个配置加载失败）。
mihomo 不可用时降级为「不探活、保留全部节点」并打印警告，不会把订阅写空。

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
所以现在延迟只做两件事：在同类节点里排序、在名字里打 `[US 123ms]` 标记。
**节点集合不再因为延迟被淘汰。**

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
的八分之一。所以 `vless` 排最前、`ss`/`ssr` 排最后；块内 CI 探活的在前（延迟升序），
没探活的在后。

#### 关于节点名里的 `[US 123ms]`

**这个数字不是你的延迟**，是美国机房的测量值。带标记 = CI 探活成功；
不带标记 = CI 没连上，但**不代表国内不能用**（实测大部分国内能用的节点恰好
在未探活的那批里）。

真正该看的延迟是客户端自己测的 —— `clash.yaml` 里的 `🔄 自动选择` 是 `url-test`
组，Clash 会在你本地每 5 分钟重新测一遍，自动切到当前最快的那个。所以正常用法是
**选 `🔄 自动选择`，不要手动挑节点**。

## 加订阅源

编辑 `subscriptions.json`：

```json
{
  "subscriptions": [
    "https://example.com/sub?token=xxx"
  ]
}
```

## 可调参数

都在 `.github/workflows/main.yml` 的 env 里：

| 变量 | 默认 | 说明 |
|---|---:|---|
| `MIN_NODES` | 50 | 解析结果低于此数直接让任务失败，防止订阅源集体失效时发布空配置 |
| `MAX_PROBE` | 1500 | 最多探活多少个节点 |
| `PROBE_CONCURRENCY` | 32 | 探活并发数 |
| `MAX_OUTPUT` | 1000 | 最多输出多少个节点。节点数没超这个值时**全部输出**，只有超了才按协议配额裁剪 |

## 本地运行

```bash
pip install requests pyyaml
python fetch_subscriptions.py
python speed_test.py        # 没有 mihomo 会自动降级为不探活、全部保留
```

## 注意

- `subscriptions.json` 里的订阅链接含私有 token，本仓库是公开的，**加自己的机场链接前先想清楚**。
- 公开免费节点质量参差，不要用来登录重要账号。
- 客户端里选 `🔄 自动选择`，不要手动挑节点 —— 节点名里的延迟是美国机房测的，参考价值有限。
