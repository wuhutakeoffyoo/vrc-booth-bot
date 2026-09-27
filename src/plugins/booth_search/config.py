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

    # 识图 AI（OpenAI 兼容 chat/completions；留空 key 则跳过 AI 提词）
    vision_api_key: str = ""
    vision_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    vision_model: str = "glm-5.3-flash"
    vision_timeout: int = 60
