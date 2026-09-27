# 双服务器代理部署方案（设计稿 · 尚待实测）

> **状态：待实测（2026-09-27）**。本方案基于网络诊断与部署常识设计，
> 尚未在目标服务器上验证。每一步都对应 `scripts/probe.sh` 的一个验证点，
> 部署时先跑探针、用数据确认，再按清单执行。

## 背景（本机实测结论，2026-09-27）

- `booth.pm`（Cloudflare）国内直连慢且不稳（TTFB 200ms 起、晚高峰丢包）；
- `booth.pximg.net`（pixiv 自有 IP 210.140.139.x）国内直连**不可达**（pixiv 系被墙），
  商品图下载与 imgsearch URL 模式必须海外出口；
- Bing 视觉搜索的 SBI 流程在受控出口下会被弹回首页（HTTP 快路径失败），
  海外直连出口上有望直接生效（待服务器实测确认）。

## 目标架构

| 角色 | 服务器 | 职责 |
|---|---|---|
| QQ 协议端 | 京东云（国内，已部署 NapCat） | 登录 QQ、收发消息；海外 IP 登录有风控风险，故协议端留在国内 |
| booth 出口 | 腾讯云（新加坡） | 运行 booth-cli / booth-bot（NoneBot），booth.pm、pximg、Bing 全部从新加坡直连 |
| 连接 | NapCat → NoneBot | NapCat 主动反向 WS 连到新加坡的 NoneBot（`ws://<SG>:<port>/onebot/v11/ws`），无需公网入站到京东云 |

```
QQ 消息 → 京东云 NapCat --反向WS(带 access_token)--> 腾讯云SG NoneBot(booth-bot)
                                                        └→ booth.pm / booth.pximg.net / bing（SG 直连出口）
```

## 部署清单（每步先跑 probe 对应项）

1. **探针**：两台服务器各跑 `scripts/probe.sh`，确认：
   - SG：booth.pm TTFB < 150ms；pximg 缩略图可下载；Bing HTTP 快路径可用（无需浏览器）。
   - 京东云：预期 booth.pm 勉强通、pximg 不通（作为反例校准）。
2. **SG 装 booth-bot**：Python 3.10+，`pip install -e .`，配 `.env`（见 bot 仓库 README）。
3. **开放端口**：SG 安全组放行 NoneBot 的反向 WS 端口（如 8080）。
4. **NapCat 配置反向 WS**：指向 `ws://<SG公网IP>:8080/onebot/v11/ws`，
   `access_token` 与 SG 端 `.env` 的 `ONEBOT_ACCESS_TOKEN` 一致。
5. **imgsearch 兜底**：若 SG 上 Bing HTTP 快路径不可用（probe 第 3 项失败），
   需装 playwright + Chrome + xvfb 并以 `--headless` 运行浏览器备援路径。

## 待验证假设（实测时逐条打勾）

- [ ] SG 出口下 `bing_search_http` 两步协议能拿到 insights 结果（免浏览器）
- [ ] NapCat 反向 WS 跨境到 SG 长连接稳定性（掉线重连策略）
- [ ] SG IP 访问 booth.pm 的限流阈值与 429 频率
- [ ] pximg 大图（非缩略）下载成功率与速度
- [ ] 京东云 ↔ SG 无需额外隧道（反向 WS 直连即可）；若 QQ 消息图片 URL 拉取在
      SG 侧慢（多媒体 CDN 多为国内），再评估图片中转方案

## 备选路线（若主方案受阻）

- **全放 SG**：QQ 协议端也上 SG（官方 bot API 可行；NapCat 协议端登录有风控风险，
  需先在 SG 上试登录验证）。
- **京东云 + 自建代理出口**：在 SG 起 wireguard，京东云 booth 流量走隧道——
  仅当 NapCat 必须与 booth-bot 同机时才考虑，复杂度最高。
