# Muse 云端实测快照

该目录由 GitHub Actions 自动生成；Muse 直接消费，不需要连接节点。

- 生成时间：`1795852434`
- Muse 目标：`https://muse.ai/api/consent/status`
- 候选数：2896
- 实测节点数：2369
- 最终通过：240
- 类型：{"ss": 111, "vless": 115, "vmess": 14}
- 出口：{"AU": 2, "CA": 16, "GB": 11, "JP": 40, "SG": 32, "US": 139}

`muse_reach.json` 已包含每条通过节点的 `exit_ip`；ipquality 应直接对该字段
做新鲜打分，不应从无法连接节点的虚拟机再次进入代理。
