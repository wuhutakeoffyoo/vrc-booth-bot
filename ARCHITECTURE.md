# 架构与实现思路 —— booth-cli（引擎层）+ vrc-booth-bot（实现侧）

本文面向想读懂或二次开发的贡献者，讲清三件事：**系统怎么运作**、**每一层的实现与兜底思路**、**参考/借鉴了哪些开源项目**。部署步骤见 [PROXY_DEPLOYMENT.md](PROXY_DEPLOYMENT.md)，用法见各仓 README。

## 1. 系统总览与运作原理

```
QQ 群/私聊
   │  消息（/vrc search 关键词 或 关键词+图片）
   ▼
NapCat（QQ 协议端，京东云 docker）──反向 WebSocket──▶ vrc-booth-bot（NoneBot2，腾讯云 SG）
                                                        │  白名单/限速/查询缓存（access.py, qcache.py）
                                                        │  subprocess：booth bot '<json>'（JSON 信封，永不抛栈）
                                                        ▼
                                                   booth-cli（零依赖 Python）
                                                        │  https://*.booth.pm（搜索页/单品 JSON/商店页）
                                                        │  booth.pximg.net（官方图床，带 Referer）
                                                        │  www.bing.com / ascii2d（图搜引擎）
                                                        ▼
                                              AI 后端（bot 侧直连，三级兜底）
                                     OpenCode Zen Go ──▶ GLM Coding Plan ──▶ opencode CLI (mimo free)
                                     网络检索兜底：DuckDuckGo HTML ──▶ Exa API
```

**一次文本查询的生命周期**（`__init__.py::_handle_text`）：

1. 指令解析：正则匹配 `/vrc search`，末尾独立数字（1-3 位）剥为页码。
2. 访问控制：群白名单（白名单外静默忽略）、每用户限速（10 秒间隔、每分钟 5 次）。
3. 查询缓存：相同查询（模式/页码/词）TTL 内直接回缓存结果，标注「可能非最新」。
4. 路由判定：`_looks_chinese` 判定语言 → 中文走 AI 翻译链路，日文/拉丁词走直搜。
5. 搜索执行：多个检索词 3 路并发调 CLI → 合并去重 → 标题含词的候选置顶。
6. 详情补全（`_enrich_entries`）：并发受限 3 路，抓单品详情补收藏数/真实 tags/上架日期。
7. 结果返回：QQ 合并转发（每条附商品缩略图），失败回退纯文本；翻页提示附在首节点。

**一次识图查询的生命周期**（`_handle_image`）：下载 QQ 图片（pximg 5xx 自动退避重试）→ 视觉 AI 提词（图内文字逐字转写）→ CLI 反向图搜（Bing 引擎链，见 §4.3）→ 图搜派生词并入关键词 → 空关键词时知名商品回忆兜底 → 前 3 个关键词各搜一轮合并 → 统一重排 → 补详情 → 合并转发。

### 数据来源（非官方接口）

Booth 无官方公开 API，全部数据来自页面内嵌结构：

| 用途 | 端点 | 提取方式 |
|---|---|---|
| 搜索 | `/ja/search/{query}`、`/ja/browse/{分类}` | 正则解析 `<li class="item-card">` 的 `data-product-id/brand/price/category/event` 属性 |
| 单品 | `/ja/items/{id}.json` | 官方页面自用的 JSON 接口，直接 `json.loads` |
| 商店 | `{sub}.booth.pm/items` | 解析 `data-item="..."` 内嵌 JSON（多数店铺整页前端加载，服务端只渲染最新几件） |

关键站点特性与对策：

- **年龄门**：请求带 cookie `adult=t` 绕过；R-18 结果仍由 `adult` 参数控制。
- **R-18 是一元标记**（情色与怪诞/R18G 同旗、无独立过滤）：默认 `adult=include` 联合搜索，
  每条结果带 `is_adult` 标记，由展示侧决定过滤策略；bot 的 `/vrc r18` 专项搜索仅管理员可用。
- **popularity 排序忽略 page 参数**（站点行为）：bot 翻页时自动切按新着排序并在结果里标注，
  否则翻页永远返回第一页。

## 2. booth-cli（引擎层）实现原理

单文件 `booth.py` + `reverse_search.py`，零第三方依赖（Python 标准库）。

- **解析而非抓取渲染**：Booth 搜索页是服务端渲染的，商品卡片的 data 属性就是结构化数据，
  正则提取即可，无需浏览器；单品走 `.json` 接口，比解析 HTML 更稳。
- **统一出站层 `http_get`**：所有请求过同一管道——URL 白名单校验（仅 `https://*.booth.pm`，
  重定向逐跳复验）→ sqlite 磁盘缓存（商品 6h / 搜索页 10min，`--no-cache` 跳过）→
  全局限速（`_polite_wait` 线程安全，距上一请求 ≥1 秒）→ `Retry-After` 感知的重试
  （最多 4 次，指数退避+抖动）→ gzip 解压。
- **bot JSON 信封**（`booth bot`）：子进程进出、stdout 单行 JSON、退出码恒 0、永不抛栈。
  信封里的 snake_case 参数转成 argv 走同一条 argparse 路径——bot 与 CLI 人类用法永远行为一致。
- **智能搜索 `booth smart`（VRC 对口，与 bot 同源策略）**：需求式描述直接走完整
  bot 侧管线——AI 关键词（单词级 + desc_keywords）→ 单词级分词 3 线程并发合并
  （出站仍受全局限速约束）→ 空结果回忆/网络检索兜底 → 拉详情（简介扩 2000 字）
  按商品说明文匹配置顶。AI 环境变量与 bot 同名（一份 .env 两边通用），缺省降级为
  分词+读音变体直搜。实现于 `smart_search.py`（AI/检索走 urllib；pykakasi 为必装
  依赖，提供假名读音变体）。search/smart 默认收窄 VRChat 圈
  （`--no-vrc` 关闭）；popularity 排序翻页自动切新着并标注 sort_note。
- **版本守卫**：bot 侧首次调用前校验 `booth --version ≥ 1.2.0`（旧版没有 bot 钩子，
  是最常见的部署坑），结果进程内缓存只查一次。
- **错误即信息**：Cloudflare 盾（"Just a moment"）检测后报「该店无法直接抓取」，
  年龄确认页误返回时报内部错误，搜索页语言不渲染时报「试试 --lang ja」——
  每个失败都告诉调用方下一步能做什么。

## 3. bot（实现侧）三链路设计

### 3.1 日文/拉丁词直搜

Booth 标题多是日文原名，原词直搜往往就是最优解。含 4 字以上拉丁词的中文查询
（商品原名/罗马字）**刻意不走翻译**——百样本实测原词直搜 Top1 命中 5/7，AI 翻译 0，
翻译只留作直搜空结果的兜底。

### 3.2 中文链路（AI 翻译 + 分词搜索）

> 本节策略已下沉为 CLI 的 `booth smart`（`smart_search.py` 为 bot 侧 vision/webfind/rank
> 的零依赖移植，环境变量同名共用）——两仓一套策略，两个入口。
> 旧「强制翻译」链路（`vision.translate_keywords` / `smart_search.translate_keywords` /
> `_looks_chinese` 等）保留归档、未启用——是否翻译已改由方案阶段模型自决。

1. **搜索方案**（`vision.plan_search` / `smart_search.plan_search`）：LLM 理解需求输出
   单词级日语关键词 + desc_keywords + translated 标记——**翻译只是可选项**，输入已是
   日文/专名时直接沿用原词（不强行翻译）；E 站（E-Hentai）AI 翻译本子类开源实践的
   术语约束与写法规范（单词级/完整片假名/行业词）在此阶段生效。
2. **变体扩展** `expand_reading_variants`：pykakasi 汉字→平假名读音（信濃→しなの，
   Booth 不做跨字形归一）；含空格的词追加去空格连写形（ショコラ ドレス→ショコラドレス）；
   同读音关键词（リング/指輪）只保留首个，省检索槽位。pykakasi 为必装依赖。
3. **分词搜索** `_search_merged`：关键词拆到单词级（≥2 字），连写形整词保留，
   最多 6 词 3 路并发搜索，结果合并去重——标题含任一检索词的候选置顶（稳定排序）；
   标题命中数足够时直接丢弃不相关填充（多词合并会混进其他词的 popularity 垃圾，
   「铃铛」不加本裁剪会带回鸟居/泳装）。
4. **结果评估 + 二轮重搜**（`evaluate_results`）：LLM 以用户立场严格评估候选标题，
   `verdict=ok` 必须给出 ≥3 条具体命中（hits 字段）否则判 retry；明显偏离 →
   retry 并给第二轮关键词；QQ 侧经 notify 提示「正在执行第二轮搜索」后重搜，
   两轮合并（二轮优先）再评估一次，确认命中或如实说明「仍未完全确认」。
   **保守 retry**：评估放行但标题命中率 ≤1/3 时仍触发二轮（词源=评估词，
   或带第一轮教训 `feedback` 重新规划）——防评估员过宽。
   知名商品回忆不再是文本链路的独立步骤——二轮关键词由评估员给出，天然覆盖
   该场景（识图链路仍用 recall 兜底）。
5. **行业同义词种子层**（`INDUSTRY_SYNONYMS` + 方案提示词对照示例）：中文泛称 →
   Booth 行业词的硬映射（墨镜→サングラス、枪械→銃/ガン、法线贴图→ノーマルマップ…，
   盲测失败词沉淀），方案阶段命中即插入；主要靠提示词让模型举一反三。
6. **分词与排序**：单词级检索词（**CJK 单字是合法词**——鈴/耳 曾被 ≥2 字过滤丢掉，
   导致相关搜索整个缺席）；命中更多检索词的候选优先，同分按首个命中词序位
   （首选词鈴 的命中排在尾部词ベル 的子串噪音前）；标题命中数足够时丢弃
   不相关填充。
7. **描述核实** `desc_keywords`：翻译提示词同时产出『需要到商品说明文核实的词』
   （対応素体/仕様 段落里的素体名/功能名）——「适用于XX素体的服装」这类兼容性需求，
   对应信息写在商品说明里而非标题，只搜标题永远碰不到。命中的候选拉详情
   （简介扩到 2000 字）核对，说明文/标题含核实词的置顶（`rank.desc_boost` 三级重排），
   并在结果里注明核实结果（N 件命中 / 未核实到）。

### 3.3 识图链路（视觉 AI + 反向图搜双路合并）

- **视觉 AI 提词**的提示词要求图内文字**逐字转写**（日文保持日文、严禁罗马字/意译）——
  图内文字往往就是商品名，是最高价值信号；图内无文字时按外观特征（发色/服装/配色）给词。
  推理模型偶发空转（返回空关键词）自动重试一次。
- **CLI 反向图搜**（下一节详述）返回候选 ID 与派生词；派生词（Bing 对图内文字的 OCR）
  并入关键词搜索队列最前。
- **合并重排**：图搜候选 + 各关键词搜索命中进同一池去重，标题含任一关键词（视觉词/派生词/
  回忆词）的置顶；`via` 字段标注来源（图搜/关键词/网络检索）。
- **知名商品回忆** `recall_products`：模型空转或图内无文字时，拿提示词/派生词当描述，
  让模型回忆最可能的具体商品名（日文原名，含作者名更好）——用模型知识补偿感知失败。

## 4. 兜底思路：每一层都有退路

设计原则：**单点失败不致命，降级要告知**。每级兜底触发时都在结果里注明退化方式
（如「AI 翻译不可用：xxx（已用原词直搜）」），用户永远知道拿到的是降级结果还是正常结果。

### 4.1 网络与解析层（booth-cli）

| 故障 | 兜底 |
|---|---|
| 429/403/5xx 限流或临时错误 | 优先读响应 `Retry-After`，无则指数退避+抖动，最多 4 次 |
| Cloudflare 盾（店铺级） | 识别 "Just a moment" 页，明确报「该店无法直接抓取，可改用 item <ID> 逐个查」 |
| 单品 JSON 被拒（403 等） | 先抓 HTML 页取 `csrf-token`，带 `X-Csrf-Token` 头重试 |
| sqlite 缓存打不开 | 降级为无缓存直连，功能不受影响 |
| 图片下载 | 校验 JPEG 魔数（`\xff\xd8`），pximg 必须带 `Referer: https://booth.pm/` |
| 搜索页语言不渲染 | 报错提示「试试 --lang ja」（ja 偶发不渲染，靠重试兜住，见 §8） |

### 4.2 搜索策略层（bot）

| 场景 | 兜底链 |
|---|---|
| 中文查询 | AI 翻译+回忆 → 分词 3 路并发 → 全空时**网络检索兜底**（DDG HTML 免 key → Exa 限定 booth.pm 域，提取商品链接 ID 后回 CLI 抓详情）→ 仍无则报准确空结果 |
| 「适用于XX素体」类兼容需求 | AI 翻译输出 desc_keywords（对应素体/功能名）→ 候选拉详情核对商品说明文 → 说明文命中置顶并注明核实结果（未命中也如实告知） |
| 日文/拉丁词直搜 | 原词直搜 → 空结果时 AI 关键词重试 → 网络检索兜底 |
| AI 翻译失败 | 退回原词直搜，结果注明「AI 翻译不可用：原因（已用原词直搜）」 |
| 翻译输出坏 JSON | 强化指令（只输出 JSON 本体）重试一次，仍失败视为该后端失败 |
| Booth AND 短语脆弱 | AI 关键词强制单词级 + 连写形变体 + 读音变体多路并发 |

### 4.3 图搜引擎链（booth-cli imgsearch）

```
bing 纯 HTTP 快路径（约 2 秒，无浏览器）
  │  multipart 上传（cbir=sbi + base64 图片）→ 302 下发 bcid →
  │  跟随 detailV2 重定向链 → 结果页派生词（图内文字 OCR）
  │  受限网络会被弹回首页（URL 含 FORM=SBIRDI/SBIHMP），检测到即抛错 ↓
  ▼
playwright 浏览器备援（有头 Chrome + 持久 profile ~/.booth-cli/pw_profile 复用通过状态，
  --disable-blink-features=AutomationControlled；服务器用 --headless）
  ▼  仍有结果则止步；否则
ascii2d 备援（色合い → 特徴 两轮）
  ▼
派生词关键词直搜兜底：图搜无 booth 直链时，用派生词走站内搜索
  （全派生词无果 → 剔除 CJK 只留拉丁词元再试——Bing 的 OCR 词常带括号注释）
  ▼
有直链时派生词关键词合并进候选池；最终排序：视觉命中前 2 → 关键词命中 → 其余视觉候选
```

关键实测结论（协议逆向自 PicImageSearch，SG 出口验证）：

- Bing knowledge API 路线**不可行**：无 `X-Image-Knowledge-Signature` 时恒返回空壳，
  而该签名只存在于 JS 渲染页面，纯 HTTP 拿不到——所以快路径改为「重定向链取派生词」方案。
- 会话 Cookie 必须跨请求保持：上传 302 下发的 Cookie 是后续 detailV2 跳转的通行证。
- Bing 即使给不出视觉直链，**派生词也足以驱动关键词搜索**——这就是最后一层兜底的依据。

### 4.4 AI 后端链（bot）

```
主 API（OpenCode Zen Go，https://opencode.ai/zen/go/v1，需 x-opencode-session 头）
  ↓ 失败/额度耗尽
兜底 API（GLM Coding Plan，https://open.bigmodel.cn/api/coding/paas/v4）
  ↓ 失败
opencode CLI（`opencode run`，mimo-v2.6-flash-free，零额度成本）
  ↓ 全部失败
BoothUnavailable → 结果里给准确原因（friendly_ai_error）
```

- `friendly_ai_error` 把异常翻译成可行动的中文：402 额度不足、429 按 5 小时/周/月窗口
  分别提示恢复时间、401 认证失败、403 权限/安全策略、5xx 上游故障、超时——
  群友看到错误就知道该等还是该换词。
- API 请求 UA 用浏览器标识：Cloudflare WAF 会拦「数据中心 IP + python 默认 UA」的大 body POST。
- 推理模型可能把内容放在 `reasoning_content`、把思考过程混进关键词——
  `_is_reasoning_prose` 按前缀/长度/结尾特征过滤泄漏文本。
- CLI 后端的坑：`opencode run` 的 message 参数必须在 `-f` 之前（yargs 会把后续位置参数吞进 `-f`），
  输出需清理 ANSI 色码。

### 4.5 输出与运维层（bot）

| 故障 | 兜底 |
|---|---|
| 合并转发发送失败 | 回退纯文本 |
| 单节点商品图下载失败 | 该节点降级为纯文本，其余节点不受影响 |
| pximg 原图过大导致 base64 发送失败/极慢 | 原图 URL 改写为 300x300 缩略图（`/c/300x300_a2_g5/` + `_base_resized.jpg`） |
| CLI 旧版本 | 首次调用时版本守卫拦截，报「请更新 booth-cli 或指定 BOOTH_CLI_PATH」 |
| 用户刷指令 | 明确返回「请 N 秒后再试」与限流规则，而非静默丢弃 |
| 缓存命中 | 标注「（缓存结果，可能非最新）」 |

## 5. 性能与额度设计

- **双层缓存**：CLI 磁盘缓存（sqlite，商品 6h/搜索页 10min，7 天自动清理）挡重复抓取；
  bot 查询缓存（sqlite，TTL 30min/上限 300 条，写时自动清过期与最旧）挡重复 AI 调用，
  实测重复查询 2.6 秒 → 0.56 秒。图片查询不走 bot 缓存（图床 URL 有时效）。
- **双层限速 + 全局并发闸**：CLI 对 booth.pm 全局 ≥1 秒间隔（线程安全，多页抓取页间隔
  1.2 秒）；bot 每用户 10 秒间隔 + 每分钟 5 次（进程内滑动窗口），另有跨用户全局并发闸
  （默认同时 2 个查询，满员直接告知稍后再试——每用户限速管不住不同用户同时各来一发）。
  这是对站点的礼貌，也是防风控需要。
- **并发控制**：所有并发抓取（分词搜索/详情补全/转发图下载）统一 Semaphore(3)，
  AI 调用靠任务级并行（翻译+回忆 gather）而不是加并发数。

## 6. 参考的开源项目

### 直接借鉴（协议与机制）

| 项目 | 借鉴点 |
|---|---|
| [kitUIN/PicImageSearch](https://github.com/kitUIN/PicImageSearch) | Bing 视觉搜索纯 HTTP 协议（multipart 上传 → bcid → detailV2 链）与 imageSignature 解密（XOR + 偏移 3） |
| [requests-cache](https://github.com/requests-cache/requests-cache) | 持久 HTTP 缓存设计：URL 为 key、sqlite 存储、按资源类型分 TTL |
| [tenacity](https://github.com/jd/tenacity) | 重试策略：优先响应 `Retry-After`，否则指数退避 + 抖动 |
| E 站（E-Hentai）AI 翻译本子类开源实践 | 设计参考（非代码）：小语种翻译的术语约束与写法规范——中文翻译提示词的结构来源 |

### 运行组件

| 组件 | 用途 |
|---|---|
| [NoneBot2](https://nonebot.dev/) + OneBot v11 适配器 | bot 框架：事件/规则/API 调用 |
| [NapCat](https://github.com/NapNeko/NapCatQQ) | QQ 协议端，反向 WebSocket 接入 |
| [playwright](https://playwright.dev/python/) | 图搜浏览器备援（持久 profile 过 Cloudflare） |
| [pykakasi](https://codeberg.org/miurahr/pykakasi) | 汉字→假名读音变体（可选依赖，未装自动跳过） |
| httpx | bot 侧异步 HTTP |

### 调研后未采用（及原因）

- **boothmate / BoothPM-SDK / gallery-dl / Booth2RSS / booth-manager**：都是给人写代码用的库
  或 RSS/下载工具，没有「给 AI agent 用的搜索 CLI/MCP」——这是自己写 booth-cli 的原因；
  但它们验证过的页面结构（`li.item-card` data 属性、`/ja/items/{id}.json`）被直接沿用。
- **Google Lens**：对自动化客户端 403 封锁结果页，绕不过。
- **SauceNAO**：匿名账户禁用 JSON API，且索引不含 booth.pm。
- **ascii2d**：保留为备援而非主路——索引偏 pixiv 同人图，booth 商品页覆盖不如 Bing。

## 7. 盲测方法论：设计决策的依据

三链路的策略不是拍脑袋，全部由盲测数据驱动：每条链路盲抽 100 个 VRC 对口样板，
目标准确率 ≥90%，按「跑测 → 改策略 → 复测」自迭代
（booth-cli `scripts/`：`samples_vrc100.py` 取样、`zhgen100.py` 生成用户口吻中文查询、
`blind_one.py` 单链路 harness，支持 `--start/--end/--dir` 切片并行；数据集在服务器 `~/vblind100/`）。

最终成绩：**JP 95% / ZH 100% / IMG 93%**。

IMG 链路是这套方法论的代表案例：一测 Top1 约 50% 就撞上瓶颈——图片里的**艺术字**
（风格化标题、店铺水印）会干扰视觉模型的 OCR；接入 **Exa 网络检索**与 **LLM 自有
知识库的知名商品回忆**（自我纠错层）后，二测突破瓶颈到 93%。

盲测推翻/确立过的决策举例：

- 中文查询含 4 字以上拉丁词时不走 AI 翻译（原词直搜 Top1 5/7 vs 翻译 0）。
- 分词搜索必须拆到单词级（Booth 短语 AND 匹配脆弱）。
- 图搜派生词必须做关键词合并（把无字图命中率从 0 拉起来）。
- Bing HTTP 快路径 knowledge API 路线不可行（双环境实测空壳）。

## 8. 已知限制与下一步

1. **IMG 极端艺术字仍会误读**：93% 之后的剩余头部集中在视觉模型对风格化字体的
   识别，换更强视觉模型或专用 OCR 可再进一步。
2. **查询延迟 1-2 分钟**：中文/识图链路的 AI 串行调用固有成本，可考虑预取或更快端点。
3. **Booth ja 搜索页偶发不渲染**：未修，靠重试兜住。
4. 个别店铺开了 Cloudflare 盾无法抓取（会精准报错）；多数商店商品列表前端加载，
   `shop` 只能取到服务端渲染的最新几件。
5. 未登录功能（收藏/购物车/已购下载）不在工具范围。

## 9. 安全边界汇总

- 出站域名白名单：booth 侧仅 `https://*.booth.pm`（重定向逐跳复验）+ 官方图床
  `booth.pximg.net`；Bing 侧主机精确匹配 `www.bing.com`、无显式端口、**DNS 解析结果
  全部为公网地址**（防 SSRF/DNS rebinding）；AI base URL 拒绝私网/保留地址。
- 凭据零入库：API key、QQ 号、access_token 只从 `.env`/环境变量读取，仓库内无字面量。
- NapCat 反向 WS 两侧配 access_token，防公网裸奔被扫。
- 测试：`python -m unittest discover -s tests`（两仓各自），覆盖解析器/安全边界/缓存/
  退避/bot 信封封装/限速/缓存清理，不联网。
