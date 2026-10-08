"""插件配置（pydantic 模型，值从 .env / 环境变量读取，仓库内不存任何凭据）。"""
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator
from typing import Literal


class Config(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    # booth CLI 位置：空则从 PATH 找 booth，也可指向 booth.py 绝对路径
    booth_cli_path: str = ""

    # 搜索行为
    booth_limit: int = 6
    booth_sort: str = "popularity"
    r18_mode: str = "include"          # include / exclude / only
    search_timeout: int = 60
    query_timeout: int = Field(default=180, ge=30, le=600)
    request_budget: int = Field(default=12, ge=6, le=100)
    retry_request_budget: int = Field(default=18, ge=6, le=100)
    search_candidate_limit: int = Field(default=60, ge=15, le=100)
    plan_cache_ttl: int = Field(default=1800, ge=0)
    run_profile: Literal["production", "benchmark"] = "production"
    benchmark_allow_ai: bool = False  # explicit opt-in; use independently provisioned quota

    # 图搜
    imgsearch_headless: bool = True    # 服务器环境务必 true
    imgsearch_timeout: int = 240
    image_allowed_hosts: list[str] = [
        "multimedia.nt.qq.com.cn", "gchat.qpic.cn", "c2cpicdw.qpic.cn",
        "booth.pximg.net", "booth.pm",
    ]

    # 默认工具模式：当前工作流 AI 做规划/判断；独立 QQ Bot 可显式选择 api/cli。
    # 存在密钥不等于启用委托，caller 不探测也不调用任何模型。
    ai_mode: Literal["caller", "api", "cli"] = "caller"
    # cli 模式
    ai_cli_bin: str = ""               # opencode 可执行文件路径（空则从 PATH 找）
    ai_cli_model: str = "opencode/mimo-v2.6-flash-free"
    ai_cli_timeout: int = 90
    # api 模式：OpenAI 兼容、Anthropic /messages、Gemini :generateContent
    # key + URL 即可自动发现模型；不提供模型列表的服务需补填 AI_MODEL
    vision_api_key: str = Field(default="", validation_alias=AliasChoices("ai_api_key", "vision_api_key"))
    vision_base_url: str = Field(default="", validation_alias=AliasChoices("ai_base_url", "vision_base_url"))
    vision_model: str = Field(default="", validation_alias=AliasChoices("ai_model", "vision_model"))
    vision_session_id: str = ""        # x-opencode-session 头；空则每请求自动生成
    vision_timeout: int = 60
    # 可选兜底 api（同样自动识别协议与模型）
    fallback_api_key: str = Field(default="", validation_alias=AliasChoices(
        "ai_fallback_api_key", "fallback_api_key"))
    fallback_base_url: str = Field(default="",
        validation_alias=AliasChoices("ai_fallback_base_url", "fallback_base_url"))
    fallback_model: str = Field(default="", validation_alias=AliasChoices(
        "ai_fallback_model", "fallback_model"))
    # 自我纠错：利用模型 VRChat 圈知识回忆知名商品名（中文查询时追加搜索）
    recall_enabled: bool = True
    # 网络检索：独立选择服务；auto 按已知域名识别，其余使用通用 JSON 协议
    websearch_fallback: bool = True
    search_api_key: str = ""
    search_base_url: str = ""
    search_provider: str = "auto"
    search_ddg_enabled: bool = True
    # 仅兼容旧配置；新 SEARCH_BASE_URL 优先，不复用旧 key
    exa_api_key: str = ""
    exa_base_url: str = "https://api.exa.ai"

    @model_validator(mode="before")
    @classmethod
    def legacy_ai_aliases(cls, data):
        if isinstance(data, dict):
            data = dict(data)
            for aliases in (
                (("ai_api_key", "vision_api_key"), ("ai_base_url", "vision_base_url"), ("ai_model", "vision_model")),
                (("ai_fallback_api_key", "fallback_api_key"), ("ai_fallback_base_url", "fallback_base_url"),
                 ("ai_fallback_model", "fallback_model")),
            ):
                new_connection = any(str(data.get(new) or "").strip() for new, _ in aliases[:2])
                for new, old in aliases:
                    if new_connection:
                        # A new endpoint/key selects a whole connection; never mix providers.
                        data[new] = data.get(new) or ""
                    elif not data.get(new) and data.get(old):
                        data[new] = data[old]
        return data

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
    # 关注清单（/vrc watch）：后台定时检查价格/补货/商品更新并推送变动到群。
    # 后台检查走 CLI 共享请求预算（每项 ≥1s 限速，单轮上限见 CLI 侧配置）；
    # 推送目标为 watch_notify_groups，留空则用群白名单
    watch_enabled: bool = False
    watch_interval: int = 3600          # 后台检查间隔（秒），最小 600
    watch_notify_groups: list[int | str] = []
