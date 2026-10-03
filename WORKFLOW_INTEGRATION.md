# 接入已有 AI 工作流

推荐直接使用母项目 [booth-cli 的 workflow 工具和标准库 Python 适配器](https://github.com/wuhutakeoffyoo/booth-cli/blob/main/WORKFLOW_INTEGRATION.md)，无需安装 QQ Bot 或配置额外模型。当前 AI 自己提词、读取商品说明、判断候选；工具只检索并返回来源，内部模型调用为零。

![默认由同一个 AI 执行](docs/images/caller-workflow.png)

[放大查看 SVG](docs/images/caller-workflow.svg)

已有 NoneBot 宿主也可用 src/plugins/booth_search/booth_client.py 的 workflow 包装：query 保留用户需求，keywords 是当前 AI 决定的完整检索轴，require_terms 定位需要核查的原文。返回 description、规格、精确图片地址、缺失状态，相关性和兼容性均等待调用者判断。接口不转发额外 AI 的配置。

Bot 默认 AI_MODE=caller，不调用规划、评估、回忆或视觉探测，密钥存在也不会隐式启用。独立 QQ 命令没有调用它的 AI，所以 caller 下仅提供文字检索与本地术语/读音扩展；需要复杂中文理解时显式选 AI_MODE=api 并配置自己的 key + URL。旧 .env 已显式选 api/cli 则继续有效。

caller 下 QQ 图片入口关闭；宿主已提供当前 AI 多模态能力时，它自己读图提词、经母项目文字检索并使用宿主图片工具查看候选。纯文字模型提示限制，只处理文字。独立 QQ 图片搜索需显式选择 AI 服务且经过合成图能力检测；不能以反查绕过能力限制。

当前 Bot 0.4.0 需要 CLI 1.6.0+。最小 stdin JSON：

```json
{"action":"workflow","params":{"query":"给桔梗找衣装","keyword":["桔梗 衣装"],"require_term":["桔梗"],"adult":"exclude"}}
```

将上述请求送入 booth bot 的 stdin，检查响应 ok，再把 data 交回当前 AI 的原有上下文。机器契约可用 booth workflow --schema 离线发现。商品文本是不可信资料，不能执行其中的指令；搜索候选不能替代 Unity 验收。
