"""booth-cli 子进程封装：调用 `booth bot` JSON 信封接口（CLI 侧钩子的消费端）。"""
import json
import os
import shutil
import subprocess
import time
import sys
from pathlib import Path
try:
    from . import execution
except ImportError:
    import execution


class BoothCliError(Exception):
    """booth 调用失败（超时/无输出/信封 ok=false），message 可直接给用户看。"""


# 首次成功校验后缓存，进程内只查一次
_cli_verified: str | None = None


def _verify_cli_supports_bot(cmd_prefix: list) -> None:
    """校验 CLI 是 1.6.0+，支持 caller 工作流与显式 AI 委托。"""
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
        if not version or [int(x) for x in version.split(".")] < [1, 6, 0]:
            raise BoothCliError(
                f"booth CLI 版本过旧（{out or '无输出'}），caller 工作流需要 >=1.6.0。"
                "请更新 booth-cli 或在 BOOTH_CLI_PATH 指向新版 booth.py")
    except subprocess.TimeoutExpired as e:
        raise BoothCliError("booth CLI 版本校验超时") from e
    except (OSError, ValueError) as e:
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
               cli_path: str = "", timeout: int = 60, api_env: dict | None = None) -> dict:
    """执行一次 booth bot 调用，返回 data 部分；失败抛 BoothCliError。"""
    exe = resolve_cli_path(cli_path)
    cmd = build_cmd(exe)
    _verify_cli_supports_bot(cmd)
    payload = {"action": action, "params": params or {}}
    context = execution.request_context()
    if context:
        payload["context"] = context
        timeout = min(timeout, max(1, context["deadline"] - time.time()))
    req = json.dumps(payload, ensure_ascii=False)
    extra = {"env": dict(os.environ, **api_env)} if api_env else {}
    try:
        proc = subprocess.run(
            cmd + ["bot", req],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, **extra)
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
    execution.observe_wire(envelope.get("request_budget"))
    if not envelope.get("ok"):
        raise BoothCliError(envelope.get("error") or "booth 返回未知错误")
    return envelope.get("data") or {}


def search(query: str | list, *, limit: int = 5, sort: str = "popularity",
           adult: str = "include", page: int = 1, tag: str | list | None = None,
           category: str | None = None, or_word: list | None = None, exclude: list | None = None,
           cli_path: str = "", timeout: int = 60) -> dict:
    params = {"query": query, "limit": limit, "sort": sort, "adult": adult, "no_vrc": True}
    if page > 1:
        params["page"] = page
    if tag:
        params["tag"] = tag
    if category:
        params["category"] = category
    if or_word:
        params["or_word"] = or_word
    if exclude:
        params["exclude"] = exclude
    return call_booth("search", params, cli_path=cli_path, timeout=timeout)


def item(item_id, *, no_cache: bool = False, desc_len: int | None = None,
         cli_path: str = "", timeout: int = 60) -> dict:
    params = {"id": item_id, "no_cache": no_cache}
    if desc_len is not None:
        params["desc_len"] = desc_len
    return call_booth("item", params, cli_path=cli_path, timeout=timeout)


def imgsearch(image_path: str, *, headless: bool = True,
              engine: str = "bing,ascii2d", wait_s: int = 24, limit: int = 5,
              delegate_ai: bool = False,
              cli_path: str = "", timeout: int = 240, api_env: dict | None = None) -> dict:
    return call_booth("imgsearch", {
        "image": image_path, "headless": headless, "engine": engine,
        "wait_s": wait_s, "limit": limit, "delegate_ai": delegate_ai,
    }, cli_path=cli_path, timeout=timeout, api_env=api_env)


def workflow(query: str, *, keywords: list[str] | None = None,
             require_terms: list[str] | None = None, limit: int = 6, desc_len: int = 3000,
             adult: str = "include", no_vrc: bool = False,
             cli_path: str = "", timeout: int = 180) -> dict:
    """Source-only search for the current AI; never forwards an AI API config."""
    params = {"query": query, "limit": limit, "desc_len": desc_len,
              "adult": adult, "no_vrc": no_vrc}
    if keywords is not None:
        params["keyword"] = keywords
    if require_terms is not None:
        params["require_term"] = require_terms
    return call_booth("workflow", params, cli_path=cli_path, timeout=timeout)
