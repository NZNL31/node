# node

自用订阅聚合。把多个公开订阅源合并、去重、真实测速，输出两份订阅文件：

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
fetch_subscriptions.py   ← 拉取 10 个订阅源，解析成 Clash 节点
      │                    all_nodes.yaml（中间产物，不入库）
      ▼
speed_test.py            ← 启动 mihomo，逐个节点走真实代理测延迟
      │                    按延迟升序取前 100 个
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
`generate_204`，拿到的是真实延迟，不是随机数。

如果 mihomo 不可用、配置校验失败、或通过的节点少于 `MIN_OK`，会降级为
「不筛选、保留原列表」并打印警告，不会把订阅写空。

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
| `MAX_PROBE` | 1500 | 最多测速多少个节点 |
| `PROBE_CONCURRENCY` | 32 | 测速并发数 |
| `TOP_N` | 100 | 最终保留多少个节点 |
| `MIN_OK` | 10 | 通过测速的节点少于这个数就不筛选 |

## 本地运行

```bash
pip install requests pyyaml
python fetch_subscriptions.py
python speed_test.py        # 没有 mihomo 会自动降级为不测速
```

## 注意

- `subscriptions.json` 里的订阅链接含私有 token，本仓库是公开的，**加自己的机场链接前先想清楚**。
- 公开免费节点质量参差，不要用来登录重要账号。
