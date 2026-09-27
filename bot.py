"""booth-bot 入口：NoneBot2 + OneBot v11（NapCat 反向 WS 接入）。

配置见 .env.example；插件在 src/plugins/booth_search。
"""
import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)
nonebot.load_plugins("src/plugins")

if __name__ == "__main__":
    nonebot.run()
