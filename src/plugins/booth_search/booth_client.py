"""booth-cli 子进程封装：调用 `booth bot` JSON 信封接口（CLI 侧钩子的消费端）。"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


class BoothCliError(Exception):
    """booth 调用失败（超时/无输出/信封 ok=false），message 可直接给用户看。"""


# 首次成功校验后缓存，进程内只查一次
_cli_verified: str | None = None


def _verify_cli_supports_bot(cmd_prefix: list) -> None:
    """校验 CLI 是 1.2.0+（带 bot 钩子）。旧版 booth 是最常见的部署坑。"""
    global _cli_verified
    exe_key = " ".join(cmd_prefix)
    if _cli_verified == exe_key:
        return
    try:
        proc = subprocess.run(cmd_prefix + ["--version"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=15)
        out = (proc.stdout or "").strip()
        version = out.split()[-1] if out and proc.returncode == 0 else ""
        if not version or [int(x) for x in version.split(".")] < [1, 2, 0]:
            raise BoothCliError(
                f"booth CLI 版本过旧（{out or '无输出'}），bot 钩子需要 >=1.2.0。"
                "请更新 booth-cli 或在 BOOTH_CLI_PATH 指向新版 booth.py")
    except (OSError, subprocess.TimeoutExpired) as e:
        raise BoothCliError(f"booth CLI 不可执行（{exe_key}）: {e}")
    _cli_verified = exe_key


def resolve_cli_path(configured: str = "") -> str:
    if configured:
        if Path(configured).is_file():
            return configured
        raise BoothCliError(f"BOOTH_CLI_PATH 指向的文件不存在: {configured}")
    found = shutil.which("booth")
    if found:
        return found
    raise BoothCliError("找不到 booth CLI：请配置 BOOTH_CLI_PATH，或将 booth 加入 PATH")


def build_cmd(cli_path: str) -> list:
    if cli_path.lower().endswith(".py"):
        return [sys.executable, "-X", "utf8", cli_path]
    return [cli_path]


def call_booth(action: str, params: dict | None = None,
               cli_path: str = "", timeout: int = 60) -> dict:
    """执行一次 booth bot 调用，返回 data 部分；失败抛 BoothCliError。"""
    exe = resolve_cli_path(cli_path)
    cmd = build_cmd(exe)
    _verify_cli_supports_bot(cmd)
    req = json.dumps({"action": action, "params": params or {}}, ensure_ascii=False)
    try:
        proc = subprocess.run(
            cmd + ["bot", req],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise BoothCliError(f"booth {action} 超时（>{timeout}s）")
    except OSError as e:
        raise BoothCliError(f"无法启动 booth CLI: {e}")

    if not proc.stdout.strip():
        raise BoothCliError(f"booth 无输出（stderr: {proc.stderr.strip()[:200]}）")
    last = proc.stdout.strip().splitlines()[-1]
    try:
        envelope = json.loads(last)
    except json.JSONDecodeError:
        raise BoothCliError(f"booth 输出不是合法 JSON: {proc.stdout[:200]}")
    if not envelope.get("ok"):
        raise BoothCliError(envelope.get("error") or "booth 返回未知错误")
    return envelope.get("data") or {}


def search(query: str | list, *, limit: int = 5, sort: str = "popularity",
           adult: str = "include", page: int = 1, cli_path: str = "",
           timeout: int = 60) -> dict:
    params = {"query": query, "limit": limit, "sort": sort, "adult": adult}
    if page > 1:
        params["page"] = page
    return call_booth("search", params, cli_path=cli_path, timeout=timeout)


def item(item_id, *, no_cache: bool = False, cli_path: str = "",
         timeout: int = 60) -> dict:
    return call_booth("item", {"id": item_id, "no_cache": no_cache},
                      cli_path=cli_path, timeout=timeout)


def imgsearch(image_path: str, *, headless: bool = True,
              engine: str = "bing,ascii2d", wait_s: int = 24, limit: int = 5,
              cli_path: str = "", timeout: int = 240) -> dict:
    return call_booth("imgsearch", {
        "image": image_path, "headless": headless, "engine": engine,
        "wait_s": wait_s, "limit": limit,
    }, cli_path=cli_path, timeout=timeout)
