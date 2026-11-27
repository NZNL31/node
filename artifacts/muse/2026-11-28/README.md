# Muse 本机实测快照（2026-11-28）

该快照用于让无法直连节点的虚拟机直接消费本机验收结果，不要求 Muse 侧重新运行
`muse_filter.py`。

- 数据源提交：`ef20572`
- 候选数：2788
- sing-box 可校验：2697
- TCP 预筛后：1647
- Muse API 200 且出口地区合格：38
- 类型：vless 37、ss 1
- 出口：美国 15、日本 15、新加坡 8
- Muse 探针：`https://muse.ai/api/consent/status`
- 订阅：`v2_muse_ok.txt`，base64 URI 列表，可直接导入 v2rayN
- 明细：`muse_reach.json`
- SHA256：
  - `v2_muse_ok.txt`：`9200E4A94FF10EB41C6A336530DB5BF3287BC10CE11C0B6F69A9BEFE1A7DAC86`
  - `muse_reach.json`：`479638F5323B58BD5DB4413920186DEF2506788A49B72AC573EE46035CA7D84D`

这 38 条已经完成代理握手和 Muse API 请求。后续 ipquality、L1/L2/L3、多样性
排序和 Drive 重建应只以 `v2_muse_ok.txt` 为候选输入。
