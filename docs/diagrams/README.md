# 流程图维护

仓库 Markdown 直接引用 ../images 中的 PNG，并提供 SVG 放大链接。这里的 .mmd 是维护图源，修改后需重新导出两种图片。

caller-workflow.mmd 展示默认由当前 AI 规划和判断、Booth 返回来源，以及显式选择其他模型的可选路径；两个仓库共用这张图。

使用 @mermaid-js/mermaid-cli 12.0.0，先导出 SVG 验证语法，再导出 PNG；中文渲染需系统有 Microsoft YaHei 或 Noto Sans CJK SC。

例如：mmdc -i search-flow.mmd -o ../images/search-flow.svg -b white --size 1600 --no-font-embed；验证通过后运行 mmdc -i search-flow.mmd -o ../images/search-flow.png -b white --size 1800。两仓共用的接口图与能力图同步更新，最后检查文字裁切和箭头。

若 Mermaid 的浏览器运行时尚未安装，可通过 -p 指向 Puppeteer 配置，指定本机已有 Chrome 的 executablePath；此配置属于维护环境，无需提交到仓库或要求工作流使用者安装。
