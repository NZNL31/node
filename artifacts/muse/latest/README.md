# Muse 云端实测快照

该目录由 GitHub Actions 自动生成；Muse 直接消费，不需要连接节点。

- 生成时间：`1795814035`
- Muse 目标：`https://muse.ai/api/consent/status`
- 候选数：2781
- 实测节点数：2325
- 最终通过：230
- 类型：{"ss": 112, "trojan": 1, "vless": 104, "vmess": 13}
- 出口：{"CA": 14, "GB": 12, "JP": 42, "SG": 32, "US": 130}

`muse_reach.json` 已包含每条通过节点的 `exit_ip`；ipquality 应直接对该字段
做新鲜打分，不应从无法连接节点的虚拟机再次进入代理。
