"""插件配置（pydantic 模型，值从 .env / 环境变量读取，仓库内不存任何凭据）。"""
from pydantic import BaseModel


class Config(BaseModel):
    # booth CLI 位置：空则从 PATH 找 booth，也可指向 booth.py 绝对路径
    booth_cli_path: str = ""

    # 搜索行为
    booth_limit: int = 6
    booth_sort: str = "popularity"
    r18_mode: str = "include"          # include / exclude / only
    search_timeout: int = 60

    # 图搜
    imgsearch_headless: bool = True    # 服务器环境务必 true
    imgsearch_timeout: int = 240

    # AI 后端：cli=本机 opencode CLI（Go 套餐 free 模型可用）| api=OpenAI 兼容 HTTP
    ai_mode: str = "cli"
    # cli 模式
    ai_cli_bin: str = ""               # opencode 可执行文件路径（空则从 PATH 找）
    ai_cli_model: str = "opencode/mimo-v2.6-flash-free"
    ai_cli_timeout: int = 90
    # api 模式（OpenCode Zen Go 端点 / GLM 直连等 OpenAI 兼容端点）
    # Go 端点: https://opencode.ai/zen/go/v1 （模型走套餐额度，需 session 头）
    vision_api_key: str = ""
    vision_base_url: str = "https://opencode.ai/zen/go/v1"
    vision_model: str = "glm-5.3-flash"
    vision_session_id: str = ""        # x-opencode-session 头；空则每请求自动生成
    vision_timeout: int = 60
    # 兜底 api（主 api 失败/额度耗尽时自动切换，OpenAI 兼容格式）
    # GLM Coding Plan: https://open.bigmodel.cn/api/coding/paas/v4
    fallback_api_key: str = ""
    fallback_base_url: str = "https://open.bigmodel.cn/api/coding/paas/v4"
    fallback_model: str = "glm-5.3-flash"
    # 自我纠错：利用模型 VRChat 圈知识回忆知名商品名（中文查询时追加搜索）
    recall_enabled: bool = True
    # 网络检索兜底：站内搜索无果时从 DDG/Exa 找 booth.pm 商品链接（Exa 需 key）
    websearch_fallback: bool = True
    exa_api_key: str = ""

    # 访问控制
    # 群白名单（空 = 不限制群；.env 示例: GROUP_WHITELIST='["111","222"]'，
    # JSON 数组里写纯数字 QQ/群号（int）也可以，内部统一字符串化）
    group_whitelist: list[int | str] = []
    # 管理员 QQ（可用敏感指令；.env 示例: ADMIN_USERS='["111","222"]'）
    admin_users: list[int | str] = []
    # 非管理员私聊是否可用（True=可用；False=私聊仅管理员）
    allow_private: bool = True
    # 搜索结果以 QQ 合并转发（转发消息，含商品图）发送；关闭则纯文本
    forward_messages: bool = True
    # VRC 对口：所有关键词搜索自动附加 --tag VRChat，把结果收窄到 VRChat 商品圈
    # （置空 vrc_tag 即关闭收窄）
    vrc_tag: str = "VRChat"
    # bot 侧限速：同一用户两次搜索的最小间隔（秒）与每分钟上限，防止刷指令触发风控
    user_cooldown: int = 10
    user_rate_limit: int = 5
    # 全局并发上限：同时处理的查询数（跨用户共享）。每用户限速管不住不同用户
    # 同时各来一发；满员直接告知稍后再试，保护 booth.pm 与 AI 配额
    global_concurrency: int = 2
    # 查询结果缓存：相同查询（含页码/模式）在 TTL 内直接返回缓存结果（省 AI 额度）；
    # 容量上限超出自动清理最旧。0 = 关闭缓存
    query_cache_ttl: int = 1800
    query_cache_max: int = 300
