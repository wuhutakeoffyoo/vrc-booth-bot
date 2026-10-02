# 流程图维护

仓库 Markdown 直接引用 ../images 中的 PNG，并提供 SVG 放大链接。这里的 .mmd 是维护图源，修改后需重新导出两种图片。

使用 @mermaid-js/mermaid-cli 12.0.0，先导出 SVG 验证语法，再导出 PNG；中文渲染需系统有 Microsoft YaHei 或 Noto Sans CJK SC。

例如：mmdc -i search-flow.mmd -o ../images/search-flow.svg -b white --size 1600 --no-font-embed；验证通过后运行 mmdc -i search-flow.mmd -o ../images/search-flow.png -b white --size 1800。两仓共用的接口图与能力图同步更新，最后检查文字裁切和箭头。
