# vrc-booth-bot（VRC 对口）

基于 [NoneBot2](https://nonebot.dev/) + OneBot v11（NapCat）的 QQ bot，**专注 VRChat 素材圈**（VRC 对口）：接入 [booth-cli](https://github.com/wuhutakeoffyoo/booth-cli)，提供 Booth.pm 的 VRChat 商品搜索与以图搜图。关键词搜索自动附加 `--tag VRChat`（`VRC_TAG` 可关），结果收窄到 VRChat 商品圈。**CLI 只留钩子（`booth bot` JSON 信封），bot 侧负责实现**——本项目即实现侧。

## 亮点

- **三链路搜索，统一收敛 VRChat 圈**：日文关键词直搜 / 中文需求 LLM 转译 /
  以图搜图（识图模型读图生成关键词），三条链路全部收窄到 `VRChat` 商品圈。
- **小语种翻译不裸翻**：LLM 直译日语不可靠——借鉴 E 站（E-Hentai）AI 翻译本子类
  开源实践，以术语约束与写法规范驾驭模型：单词级关键词（Booth 多词 AND 匹配脆弱）、
  专有名词片假名完整转写、部位/用途行业词、假名读音与连写变体。
- **按商品说明文核实兼容性**：「适用于XX素体的服装」这类需求的答案写在商品说明
  （対応素体/仕様 段落）而非标题——自动拉取详情按说明文匹配置顶，并如实标注核实结果。
- **盲测驱动的自迭代**：每条链路盲抽 100 个 VRC 对口样本，目标准确率 ≥90%，
  跑测→改策略→复测。图文链路一测约 50% 撞上「艺术字干扰视觉 OCR」的瓶颈，
  接入 Exa 网络搜索与 LLM 自有知识库（知名模型/热门素材回忆）后二测突破到
  **93%**（JP 95% / ZH 100%）。
- **每一层都有退路**：AI 三级后端（Go 套餐 → GLM Coding Plan → 免费 CLI 模型）、
  网络检索兜底（DDG/Exa）、双层缓存与双层限速，所有降级如实告知用户。

## 功能

- `/vrc search <关键词>` → 调 booth-cli 关键词搜索（自动收窄 VRChat 圈），返回 Top 结果（名称/价格/链接/店铺/R-18 标记）；末尾数字为页码（如 `/vrc search 猫娘女仆装 2`）
- `/vrc search` + 图片（或回复一张图片）→ 识图 AI 提取关键词 + booth-cli 反向图搜，
  双路合并返回候选（合并转发消息，每条附商品图）
- 「适用于XX素体的服装」类需求：AI 同时产出标题关键词与说明文核实词，
  按商品说明（対応素体/仕様 段落）匹配后置顶，结果注明核实情况
- 部位/用途类需求（尾巴/耳朵/ギミック 等）由 AI 转成日本圈行业词多路检索
- R-18 过滤策略可配（`R18_MODE=include/exclude/only`）， adult 结果带标记展示

## 架构

```
QQ → 京东云 NapCat --反向WS--> 腾讯云SG: NoneBot(booth-bot) --subprocess(booth bot JSON)--> booth-cli
                                                                        └→ booth.pm / pximg / bing（SG 直连出口）
识图 AI（OpenAI 兼容接口，GLM-5.3-Flash 样例）←— VISION_API_KEY 配置接入
```

系统设计详解（运作原理、三链路设计、分层兜底思路、参考的开源项目、盲测方法论）
见 [ARCHITECTURE.md](ARCHITECTURE.md)。

部署与代理方案（京东云 NapCat + 新加坡 booth 出口）见 [PROXY_DEPLOYMENT.md](PROXY_DEPLOYMENT.md)。

## 本地运行

```bash
pip install -e .          # 或 pip install "nonebot2[fastapi]" "nonebot-adapter-onebot" httpx
cp .env.example .env      # 填入 ONEBOT_ACCESS_TOKEN、VISION_API_KEY 等
python bot.py             # 默认 0.0.0.0:8080，等 NapCat 反向 WS 接入
```

## 配置（.env / 环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `ONEBOT_ACCESS_TOKEN` | 空 | NapCat 反向 WS 的 access_token，两侧必须一致 |
| `BOOTH_CLI_PATH` | PATH 中找 `booth` | 也可指向 booth.py 绝对路径 |
| `VISION_API_KEY` | 空 | 识图 AI 的 API key；**留空则图片只走 CLI 图搜（无 AI 提词）** |
| `VISION_BASE_URL` | `https://open.bigmodel.cn/api/paas/v4` | OpenAI 兼容端点 |
| `VISION_MODEL` | `glm-5.3-flash` | 模型名 |
| `VISION_TIMEOUT` | 60 | 识图请求超时（秒） |
| `BOOTH_LIMIT` | 6 | 返回候选上限 |
| `BOOTH_SORT` | `popularity` | 关键词搜索排序 |
| `R18_MODE` | `include` | include 联合搜索 / exclude / only |
| `IMGSEARCH_HEADLESS` | `true` | CLI 浏览器备援是否无头（服务器务必 true） |
| `IMGSEARCH_TIMEOUT` | 240 | 图搜超时（秒） |
| `SEARCH_TIMEOUT` | 60 | 搜索超时（秒） |

**识图 AI 说明**：任何 OpenAI 兼容的多模态 chat 接口均可。样例预设 GLM
（`https://open.bigmodel.cn/api/paas/v4` + `glm-5.3-flash`）。若换 DeepSeek：
`VISION_BASE_URL=https://api.deepseek.com`、`VISION_MODEL` 按需填写——注意 DeepSeek
官方模型历史上面向纯文本，**若无视觉能力，图片消息将退化为仅 CLI 图搜**（不报错），
文本搜索不受影响。

凭据只从环境变量/.env 读取，本仓库不存任何 key。

## NapCat 侧（京东云）

NapCat 的 OneBot 配置里添加反向 WS：

```json
{
  "enable": true,
  "urls": ["ws://<SG服务器公网IP>:8080/onebot/v11/ws"],
  "messagePostFormat": "array",
  "token": "<与 ONEBOT_ACCESS_TOKEN 相同>"
}
```

SG 安全组放行 8080（或自定端口）；建议 NapCat 与 bot 两侧都配 token，
公网裸奔 ws 会被扫。

## 测试

```bash
python -m unittest discover -s tests   # 纯逻辑单测（CLI 信封封装/格式化/识图 payload），不联网
```

端到端需要 NapCat + QQ 环境，服务器部署步骤见 PROXY_DEPLOYMENT.md。

## 开源说明

- 许可证：MIT（见 [LICENSE](LICENSE)）
- 本项目为个人工具，与 BOOTH/pixiv 官方无关联；使用时请遵守 Booth 利用条款（请求间隔 ≥1 秒，勿高并发）
- 无任何凭据入库：API key、QQ 账号等均通过 `.env` 本地配置
- 欢迎 Issue/PR；涉及部署（服务器/NapCat）的通用问题优先提 Issue
