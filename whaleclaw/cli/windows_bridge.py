"""Windows bridge commands for WhaleClaw launch/config scripts."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

CONFIG_DIR = Path.home() / ".whaleclaw"
CONFIG_FILE = CONFIG_DIR / "whaleclaw.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "gateway": {"port": 18666, "bind": "127.0.0.1", "verbose": False, "auth": {"mode": "none", "password": None}},
    "agent": {
        "model": "deepseek/deepseek-chat",
        "max_tool_rounds": 25,
        "thinking_level": "off",
        "summarizer": {"model": "zhipu/glm-4.7-flash", "enabled": True},
    },
    "models": {
        "anthropic": {"api_key": None, "base_url": None},
        "openai": {"api_key": None, "base_url": None},
        "deepseek": {"api_key": None, "base_url": None},
        "qwen": {"api_key": None, "base_url": None},
        "zhipu": {"api_key": None, "base_url": None},
        "minimax": {"api_key": None, "base_url": None},
        "moonshot": {"api_key": None, "base_url": None},
        "google": {"api_key": None, "base_url": None},
        "nvidia": {"api_key": None, "base_url": None},
    },
    "channels": {
        "feishu": {
            "mode": "ws",
            "app_id": "",
            "app_secret": "",
            "verification_token": None,
            "encrypt_key": None,
            "webhook_path": "/webhook/feishu",
            "dm_policy": "pairing",
        }
    },
    "security": {"sandbox_mode": "non-main", "dm_policy": "pairing", "audit": True},
}

MODEL_PRESETS: dict[str, list[tuple[str, str, str]]] = {
    "anthropic": [
        ("claude-sonnet-4-20250514", "Claude Sonnet 4", "off"),
        ("claude-opus-4-20250514", "Claude Opus 4", "off"),
        ("claude-sonnet-4-20250514", "Claude Sonnet 4 (思考)", "medium"),
        ("claude-opus-4-20250514", "Claude Opus 4 (思考)", "high"),
    ],
    "openai": [("gpt-5.2", "GPT-5.2", "off")],
    "deepseek": [("deepseek-chat", "DeepSeek Chat", "off"), ("deepseek-reasoner", "DeepSeek Reasoner (思考)", "high")],
    "qwen": [("qwen3.5-plus", "Qwen 3.5 Plus", "off"), ("qwen3-max", "Qwen 3 Max", "off")],
    "zhipu": [
        ("glm-4.7-flash", "GLM-4.7 Flash (免费)", "off"),
        ("glm-4.7", "GLM-4.7", "off"),
        ("glm-5", "GLM-5", "off"),
    ],
    "minimax": [("MiniMax-M2.5", "MiniMax M2.5", "off"), ("MiniMax-M2.1", "MiniMax M2.1", "off")],
    "moonshot": [("kimi-k2.5", "Kimi K2.5", "off")],
    "google": [
        ("gemini-3-flash-preview", "Gemini 3 Flash", "off"),
        ("gemini-3.1-pro-preview", "Gemini 3.1 Pro", "off"),
        ("gemini-3-flash-thinking", "Gemini 3 Flash (思考)", "medium"),
    ],
    "nvidia": [
        ("qwen/qwen3.5-397b-a17b", "Qwen 3.5 397B", "off"),
        ("z-ai/glm5", "GLM-5", "off"),
        ("z-ai/glm4.7", "GLM-4.7", "off"),
        ("minimaxai/minimax-m2.1", "MiniMax M2.1", "off"),
        ("moonshotai/kimi-k2.5", "Kimi K2.5", "off"),
        ("meta/llama-3.1-405b-instruct", "Llama 3.1 405B", "off"),
    ],
}

DEFAULT_URLS = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "minimax": "https://api.minimax.chat/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta",
    "nvidia": "https://integrate.api.nvidia.com/v1",
}


def _pause() -> None:
    input("\n  按回车键继续...")


def _yes(s: str) -> bool:
    return s.strip().lower().startswith("y")


def ensure_proxy_env() -> None:
    if os.environ.get("https_proxy"):
        return
    for port in (7897, 7890, 1087, 8080):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                proxy = f"http://127.0.0.1:{port}"
                os.environ["https_proxy"] = proxy
                os.environ["http_proxy"] = proxy
                return
        except OSError:
            continue


def ensure_config() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  已创建默认配置文件: {CONFIG_FILE}")


def load_config() -> dict[str, Any]:
    ensure_config()
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def read_status(cfg: dict[str, Any]) -> dict[str, Any]:
    gw = cfg.get("gateway", {})
    ag = cfg.get("agent", {})
    au = gw.get("auth", {})
    models = cfg.get("models", {})
    configured: list[str] = []
    for name, conf in models.items():
        key = conf.get("api_key")
        is_oauth = name == "openai" and conf.get("auth_mode") == "oauth" and conf.get("oauth_access")
        if not key and not is_oauth:
            continue
        cms = conf.get("configured_models", [])
        verified = [m for m in cms if m.get("verified")]
        total = len(cms)
        if is_oauth:
            names = ", ".join((m.get("name") or m.get("id", "")) for m in verified[:3]) if verified else ""
            configured.append(f"{name} | ChatGPT OAuth | {len(verified)}/{total} 模型 | {names}".rstrip(" |"))
        else:
            if verified:
                names = ", ".join((m.get("name") or m.get("id", "")) for m in verified[:3])
                configured.append(f"{name} | {len(verified)}/{total} 模型 | {names}")
            else:
                configured.append(f"{name} | 已配置 Key (未配置模型)")

    summ = ag.get("summarizer", {})
    summ_model = summ.get("model", "zhipu/glm-4.7-flash")
    summ_enabled = bool(summ.get("enabled", True))
    summ_provider = summ_model.split("/", 1)[0] if "/" in summ_model else ""
    summ_has_key = bool(models.get(summ_provider, {}).get("api_key")) if summ_provider else False
    summ_status = "开启" if summ_enabled else "关闭"
    if summ_enabled and not summ_has_key:
        summ_status = "⚠ 未配置 Key"

    feishu = cfg.get("channels", {}).get("feishu", {})
    app_id = feishu.get("app_id", "")
    app_secret = feishu.get("app_secret", "")
    if app_id and app_secret:
        preview = f"{app_id[:6]}...{app_id[-4:]}" if len(app_id) > 14 else app_id
        feishu_status = f"已配置 (App: {preview})"
    else:
        feishu_status = "未配置"

    return {
        "port": gw.get("port", 18666),
        "bind": gw.get("bind", "127.0.0.1"),
        "auth_mode": au.get("mode", "none"),
        "model": ag.get("model", "未设置"),
        "thinking": ag.get("thinking_level", "off"),
        "summ_model": summ_model,
        "summ_status": summ_status,
        "configured": configured,
        "feishu_status": feishu_status,
        "feishu_mode": feishu.get("mode", "ws"),
        "feishu_dm": feishu.get("dm_policy", "pairing"),
    }


def show_menu(status: dict[str, Any]) -> None:
    print("\n  ================================================")
    print("  🐋 WhaleClaw 配置管理")
    print("  🎉 B站飞翔鲸祝您马年大吉！财源广进！WhaleClaw 免费开源！")
    print("  ================================================\n")
    print(f"  📧 配置文件: {CONFIG_FILE}\n")
    print(f"  • 端口:     {status['port']}")
    print(f"  • 地址:     http://{status['bind']}:{status['port']}")
    print(f"  • 认证:     {status['auth_mode']}")
    print(f"  • 默认模型: {status['model']}")
    print(f"  • 思考深度: {status['thinking']}")
    print(f"  • 压缩模型: {status['summ_model']} ({status['summ_status']})")
    print(f"  • 已配置:   {len(status['configured'])} 个提供商")
    for line in status["configured"]:
        print(f"  •   -> {line}")
    print(f"  • 飞书渠道: {status['feishu_status']}")
    print(f"  • 连接模式: {status['feishu_mode']}")
    print(f"  • 飞书 DM:  {status['feishu_dm']}\n")
    print("  1) 配置 AI 模型")
    print("  2) 删除 AI 模型")
    print("  3) 修改 Gateway 端口")
    print("  4) 设置登录认证")
    print("  5) 配置上下文压缩")
    print("  6) 配置飞书渠道")
    print("  7) 编辑配置文件 (记事本)")
    print("  8) 运行诊断 (doctor)")
    print("  9) 查看完整配置")
    print("  0) 退出\n")


def verify_api(provider: str, model: str, api_key: str, base_url: str, quiet: bool = False) -> bool:
    cfg = load_config()
    oc = cfg.get("models", {}).get("openai", {})
    is_oauth = provider == "openai" and oc.get("auth_mode") == "oauth" and oc.get("oauth_access")
    use_key = oc.get("oauth_access", "") if is_oauth else api_key
    headers: dict[str, str]
    body: dict[str, Any]

    if provider == "anthropic":
        url = f"{base_url}/v1/messages"
        headers = {"x-api-key": use_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
        body = {"model": model, "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}
    elif provider == "google":
        url = f"{base_url}/models/{model}:generateContent?key={use_key}"
        headers = {"Content-Type": "application/json"}
        body = {"contents": [{"parts": [{"text": "hi"}]}], "generationConfig": {"maxOutputTokens": 5}}
    else:
        headers = {"Authorization": f"Bearer {use_key}", "Content-Type": "application/json"}
        if is_oauth and oc.get("oauth_account_id"):
            headers["ChatGPT-Account-Id"] = str(oc["oauth_account_id"])
        if is_oauth and provider == "openai":
            if not quiet:
                print("  ℹ OAuth 模式下 OpenAI 模型跳过验证")
            return True
        if "codex" in model:
            url = f"{base_url}/responses"
            body = {"model": model, "input": "hi", "max_output_tokens": 5}
        else:
            url = f"{base_url}/chat/completions"
            body = {"model": model, "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}

    def _try(proxy: str | None) -> httpx.Response:
        kwargs: dict[str, Any] = {"timeout": 15}
        if proxy:
            kwargs["proxy"] = proxy
        with httpx.Client(**kwargs) as client:
            return client.post(url, json=body, headers=headers)

    proxy = os.environ.get("https_proxy") or os.environ.get("HTTP_PROXY")
    needs_proxy = provider in {"anthropic", "openai", "google"}
    try:
        resp = _try(proxy if needs_proxy else None)
    except Exception:
        try:
            resp = _try(None if needs_proxy else proxy)
        except Exception:
            if not quiet:
                print(f"  ✖ 无法连接 {base_url}")
            return False

    if resp.status_code in (200, 201):
        if not quiet:
            print("  ✓ 验证成功")
        return True
    if resp.status_code == 429 and is_oauth:
        try:
            if resp.json().get("error", {}).get("code") == "insufficient_quota":
                if not quiet:
                    print("  ✓ OAuth 有效 (Plus 不含 API 额度)")
                return True
        except Exception:
            pass
    if not quiet:
        print(f"  ✖ 验证失败 ({resp.status_code})")
    return False


def save_model_entry(provider: str, model_id: str, name: str, base_url: str, verified: bool, thinking: str) -> None:
    cfg = load_config()
    prov = cfg.setdefault("models", {}).setdefault(provider, {})
    entries = prov.setdefault("configured_models", [])
    entry: dict[str, Any] = {"id": model_id, "name": name, "verified": verified, "thinking": thinking}
    if base_url and base_url != prov.get("base_url"):
        entry["base_url"] = base_url
    for i, m in enumerate(entries):
        if m.get("id") == model_id:
            entries[i] = entry
            break
    else:
        entries.append(entry)
    save_config(cfg)
    mark = "✓" if verified else "✖"
    tk = f" [thinking={thinking}]" if thinking != "off" else ""
    print(f"  {mark} {model_id} - {name}{tk}")


def configure_single_model(provider: str, model_id: str, name: str, api_key: str, base_url: str, default_think: str) -> None:
    print(f"\n  模型: {model_id} ({name})")
    print(f"  Base URL: {base_url}")
    model_url = input("  此模型使用不同 Base URL? 回车保持默认: ").strip()
    use_url = model_url or base_url
    print("\n  思考深度:")
    print("    0) off")
    print("    1) low")
    print("    2) medium")
    print("    3) high")
    tchoice = input(f"  选择 [0-3, 默认 {default_think}]: ").strip()
    thinking = {"0": "off", "1": "low", "2": "medium", "3": "high"}.get(tchoice, default_think)
    print(f"\n  正在验证 {model_id} ...")
    ok = verify_api(provider, model_id, api_key, use_url)
    save_model_entry(provider, model_id, name, use_url, ok, thinking)
    if ok and _yes(input("\n  是否设为默认模型? [y/N]: ")):
        cfg = load_config()
        cfg.setdefault("agent", {})["model"] = f"{provider}/{model_id}"
        cfg["agent"]["thinking_level"] = thinking
        save_config(cfg)
        print(f"  ✓ 默认模型: {provider}/{model_id} (thinking={thinking})")


def batch_verify_provider(provider: str, api_key: str, base_url: str) -> None:
    models = MODEL_PRESETS.get(provider, [])
    if not models:
        return
    print(f"\n  批量验证 {len(models)} 个模型...\n")
    passed = 0
    for idx, (model_id, name, think) in enumerate(models, start=1):
        print(f"  [{idx}/{len(models)}] {model_id} ...")
        ok = verify_api(provider, model_id, api_key, base_url, quiet=True)
        save_model_entry(provider, model_id, name, base_url, ok, think)
        if ok:
            passed += 1
    print(f"\n  验证完成: {passed}/{len(models)} 可用")
    if passed > 0 and _yes(input("  将第一个可用模型设为默认? [y/N]: ")):
        cfg = load_config()
        for model in cfg.get("models", {}).get(provider, {}).get("configured_models", []):
            if model.get("verified"):
                cfg.setdefault("agent", {})["model"] = f"{provider}/{model['id']}"
                cfg["agent"]["thinking_level"] = model.get("thinking", "off")
                save_config(cfg)
                print(f"  ✓ 默认模型: {provider}/{model['id']}")
                break


def provider_model_menu(provider: str, api_key: str, base_url: str) -> None:
    cfg = load_config()
    oauth_openai_only = provider == "openai" and cfg.get("models", {}).get("openai", {}).get("auth_mode") == "oauth"
    options = MODEL_PRESETS.get(provider, [])
    while True:
        print("\n  --- 可选模型 ---\n")
        for idx, (_, name, think) in enumerate(options, start=1):
            suffix = " 🧠" if think != "off" else ""
            print(f"    {idx}) {name}{suffix}")
        if not oauth_openai_only:
            print("    c) 自定义输入")
            print("    a) 批量验证以上全部")
        print("    0) 返回\n")
        choose = input(f"  选择 [1-{len(options)}{'/c/a' if not oauth_openai_only else ''}/0]: ").strip().lower()
        if choose == "0":
            return
        if choose == "a" and not oauth_openai_only:
            batch_verify_provider(provider, api_key, base_url)
            continue
        if choose == "c" and not oauth_openai_only:
            mid = input("  输入模型 ID: ").strip()
            mname = input("  显示名称: ").strip() or mid
            if mid:
                configure_single_model(provider, mid, mname, api_key, base_url, "off")
            continue
        if choose.isdigit() and 1 <= int(choose) <= len(options):
            mid, mname, think = options[int(choose) - 1]
            configure_single_model(provider, mid, mname, api_key, base_url, think)
            continue
        print("  ✖ 无效选择")


def configure_provider(provider: str, label: str) -> None:
    cfg = load_config()
    prov = cfg.setdefault("models", {}).setdefault(provider, {})
    saved_key = prov.get("api_key") or ""
    default_url = DEFAULT_URLS.get(provider, "https://api.example.com/v1")
    print(f"\n  配置 {label}\n")
    if saved_key:
        preview = f"{saved_key[:12]}...{saved_key[-4:]}" if len(saved_key) > 16 else saved_key
        print(f"  已保存 API Key: {preview}")
        new_key = input("  回车复用，或输入新 Key: ").strip()
        if new_key:
            saved_key = new_key
    else:
        saved_key = input(f"  请输入 {label} API Key: ").strip()
        if not saved_key:
            print("  ⚠ API Key 为空，已取消")
            return
    print(f"\n  默认 Base URL: {default_url}")
    custom_url = input("  如需修改请输入，回车保持默认: ").strip()
    base_url = custom_url or default_url
    prov["api_key"] = saved_key
    prov["base_url"] = base_url
    save_config(cfg)
    print("  ✓ API Key 和 Base URL 已保存")
    provider_model_menu(provider, saved_key, base_url)


def configure_openai() -> None:
    cfg = load_config()
    oc = cfg.get("models", {}).get("openai", {})
    mode = oc.get("auth_mode", "api_key")
    print("\n  配置 OpenAI\n")
    print(f"  当前模式: {'ChatGPT OAuth' if mode == 'oauth' else 'API Key'}\n")
    print("  1) API Key")
    print("  2) ChatGPT 账号登录 (OAuth)")
    print("  0) 返回\n")
    choice = input("  选择 [1/2/0]: ").strip()
    if choice == "1":
        configure_provider("openai", "OpenAI")
        cfg = load_config()
        cfg.setdefault("models", {}).setdefault("openai", {})["auth_mode"] = "api_key"
        save_config(cfg)
        return
    if choice != "2":
        return
    token = oc.get("oauth_access", "")
    expires = float(oc.get("oauth_expires", 0) or 0)
    need_login = not (token and expires > time.time())
    if not need_login:
        need_login = _yes(input("  已有有效 OAuth，是否重新登录? [y/N]: "))
    if need_login:
        try:
            from whaleclaw.utils.openai_oauth import login, save_oauth_to_config

            result = login()
            save_oauth_to_config(result)
            print(f"  ✓ ChatGPT 账号已关联: {result.account_id}")
        except Exception as exc:
            print(f"  ✖ 登录失败: {exc}")
            return
    cfg = load_config()
    oc = cfg.setdefault("models", {}).setdefault("openai", {})
    token = oc.get("oauth_access", "")
    if token:
        oc["auth_mode"] = "oauth"
        oc["base_url"] = "https://chatgpt.com/backend-api/codex"
        save_config(cfg)
        provider_model_menu("openai", token, oc["base_url"])


def configure_custom_provider() -> None:
    print("\n  自定义 AI 模型\n")
    pname = input("  提供商名称 (英文): ").strip()
    if not pname:
        print("  ⚠ 名称为空，已取消")
        return
    api_key = input("  API Key: ").strip()
    base_url = input("  Base URL: ").strip()
    model_id = input("  模型 ID: ").strip()
    name = input("  模型显示名称: ").strip() or model_id
    if not (api_key and base_url and model_id):
        print("  ⚠ 输入不完整，已取消")
        return
    cfg = load_config()
    prov = cfg.setdefault("models", {}).setdefault(pname, {})
    prov["api_key"] = api_key
    prov["base_url"] = base_url
    save_config(cfg)
    ok = verify_api(pname, model_id, api_key, base_url)
    save_model_entry(pname, model_id, name, base_url, ok, "off")
    if ok and _yes(input("  是否设为默认模型? [y/N]: ")):
        cfg = load_config()
        cfg.setdefault("agent", {})["model"] = f"{pname}/{model_id}"
        cfg["agent"]["thinking_level"] = "off"
        save_config(cfg)
        print(f"  ✓ 默认模型: {pname}/{model_id}")


def configure_summarizer() -> None:
    cfg = load_config()
    status = read_status(cfg)
    print("\n  配置上下文压缩\n")
    print(f"  当前压缩模型: {status['summ_model']}")
    print(f"  当前状态: {status['summ_status']}\n")
    print("  1) 选择压缩模型")
    print("  2) 开启/关闭压缩")
    print("  0) 返回\n")
    choice = input("  选择 [0-2]: ").strip()
    if choice == "2":
        summ = cfg.setdefault("agent", {}).setdefault("summarizer", {})
        summ["enabled"] = not bool(summ.get("enabled", True))
        save_config(cfg)
        print(f"  ✓ 上下文压缩已{'开启' if summ['enabled'] else '关闭'}")
        return
    if choice != "1":
        return
    print("\n  1) zhipu/glm-4.7-flash")
    print("  2) deepseek/deepseek-chat")
    print("  3) qwen/qwen-turbo")
    print("  4) moonshot/kimi-k2.5")
    print("  5) 自定义输入\n")
    sel = input("  选择 [1-5]: ").strip()
    preset = {
        "1": ("zhipu/glm-4.7-flash", "zhipu", "glm-4.7-flash"),
        "2": ("deepseek/deepseek-chat", "deepseek", "deepseek-chat"),
        "3": ("qwen/qwen-turbo", "qwen", "qwen-turbo"),
        "4": ("moonshot/kimi-k2.5", "moonshot", "kimi-k2.5"),
    }
    if sel in preset:
        model, provider, short_model = preset[sel]
    elif sel == "5":
        model = input("  输入模型 ID (provider/model): ").strip()
        if "/" not in model:
            print("  ✖ 格式错误")
            return
        provider, short_model = model.split("/", 1)
    else:
        print("  ✖ 无效选择")
        return
    models = cfg.setdefault("models", {})
    key = models.get(provider, {}).get("api_key") or input(f"  请输入 {provider} API Key: ").strip()
    if not key:
        print("  ✖ API Key 为空")
        return
    url = models.get(provider, {}).get("base_url") or DEFAULT_URLS.get(provider, "https://api.example.com/v1")
    custom_url = input(f"  Base URL [{url}]: ").strip()
    url = custom_url or url
    prov = models.setdefault(provider, {})
    prov["api_key"] = key
    prov["base_url"] = url
    save_config(cfg)
    ok = verify_api(provider, short_model, key, url)
    if not ok and not _yes(input("  验证失败，仍继续设置? [y/N]: ")):
        return
    cfg = load_config()
    summ = cfg.setdefault("agent", {}).setdefault("summarizer", {})
    summ["model"] = model
    summ["enabled"] = True
    save_config(cfg)
    print(f"  ✓ 压缩模型已设为 {model}")


def configure_feishu() -> None:
    cfg = load_config()
    status = read_status(cfg)
    print("\n  配置飞书渠道\n")
    print(f"  状态: {status['feishu_status']}")
    print(f"  连接模式: {status['feishu_mode']} (ws/webhook)")
    print(f"  DM 策略: {status['feishu_dm']}\n")
    print("  1) 设置 App ID 和 App Secret")
    print("  2) 设置连接模式")
    print("  3) 设置 DM 策略")
    print("  0) 返回\n")
    choice = input("  选择 [0-3]: ").strip()
    fei = cfg.setdefault("channels", {}).setdefault("feishu", {})
    if choice == "1":
        app_id = input(f"  App ID [{fei.get('app_id', '')}]: ").strip() or fei.get("app_id", "")
        app_secret = input(f"  App Secret [{fei.get('app_secret', '')}]: ").strip() or fei.get("app_secret", "")
        if app_id and app_secret:
            fei["app_id"] = app_id
            fei["app_secret"] = app_secret
            save_config(cfg)
            print("  ✓ 飞书凭证已保存")
    elif choice == "2":
        mode = input("  选择 [a=ws, b=webhook]: ").strip().lower()
        if mode in {"a", "b"}:
            fei["mode"] = "ws" if mode == "a" else "webhook"
            save_config(cfg)
            print(f"  ✓ 连接模式已设为 {fei['mode']}")
    elif choice == "3":
        dm = input("  选择 [a=pairing, b=open, c=closed]: ").strip().lower()
        mapping = {"a": "pairing", "b": "open", "c": "closed"}
        if dm in mapping:
            fei["dm_policy"] = mapping[dm]
            save_config(cfg)
            print(f"  ✓ DM 策略已设为 {fei['dm_policy']}")


def delete_models() -> None:
    cfg = load_config()
    models_cfg = cfg.get("models", {})
    entries: list[tuple[str, str | None]] = []
    i = 1
    print("\n  删除 AI 模型\n")
    for pname, pconf in models_cfg.items():
        key = pconf.get("api_key")
        is_oauth = pname == "openai" and pconf.get("auth_mode") == "oauth" and pconf.get("oauth_access")
        if not key and not is_oauth:
            continue
        cms = pconf.get("configured_models", [])
        if not cms:
            print(f"  {i}) [{pname}] (无模型，仅有 API Key)")
            entries.append((pname, None))
            i += 1
            continue
        for m in cms:
            icon = "✓" if m.get("verified") else "✖"
            print(f"  {i}) [{pname}] {icon} {m.get('id','')} - {m.get('name','')}")
            entries.append((pname, str(m.get("id"))))
            i += 1
    if not entries:
        print("  (没有已配置模型)")
        return
    print("\n  输入编号删除单个模型")
    print("  输入 p 删除整个提供商")
    print("  输入 0 返回\n")
    choice = input("  选择: ").strip()
    if choice in {"", "0"}:
        return
    if choice.lower() == "p":
        pname = input("  输入提供商名称: ").strip()
        if pname in models_cfg:
            del models_cfg[pname]
            save_config(cfg)
            print(f"  ✓ 已删除提供商 {pname}")
        return
    if not choice.isdigit() or not (1 <= int(choice) <= len(entries)):
        print("  ✖ 无效编号")
        return
    pname, mid = entries[int(choice) - 1]
    if mid is None:
        del models_cfg[pname]
    else:
        pconf = models_cfg.get(pname, {})
        pconf["configured_models"] = [m for m in pconf.get("configured_models", []) if m.get("id") != mid]
    save_config(cfg)
    print("  ✓ 删除完成")


def configure_model() -> None:
    print("\n  配置 AI 模型\n")
    print("  1) Anthropic")
    print("  2) OpenAI")
    print("  3) 通义千问")
    print("  4) 智谱 GLM")
    print("  5) MiniMax")
    print("  6) 月之暗面")
    print("  7) Google")
    print("  8) NVIDIA NIM")
    print("  9) 自定义")
    print("  0) 返回\n")
    ch = input("  选择提供商 [0-9]: ").strip()
    mapping = {
        "1": ("anthropic", "Anthropic"),
        "3": ("qwen", "通义千问"),
        "4": ("zhipu", "智谱 GLM"),
        "5": ("minimax", "MiniMax"),
        "6": ("moonshot", "月之暗面"),
        "7": ("google", "Google"),
        "8": ("nvidia", "NVIDIA NIM"),
    }
    if ch == "2":
        configure_openai()
    elif ch == "9":
        configure_custom_provider()
    elif ch in mapping:
        provider, label = mapping[ch]
        configure_provider(provider, label)


def run_config_manager() -> int:
    ensure_proxy_env()
    ensure_config()
    while True:
        cfg = load_config()
        status = read_status(cfg)
        show_menu(status)
        choice = input("  请输入选项 [0-9]: ").strip()
        if choice == "1":
            configure_model()
            _pause()
        elif choice == "2":
            delete_models()
            _pause()
        elif choice == "3":
            port = input(f"\n  当前端口: {status['port']}\n  输入新端口: ").strip()
            if port:
                try:
                    cfg.setdefault("gateway", {})["port"] = int(port)
                    save_config(cfg)
                    print(f"  ✓ Gateway 端口已设置为 {port}")
                    print("  ⚠ 需要重启 Gateway 生效")
                except ValueError:
                    print("  ✖ 端口无效")
            _pause()
        elif choice == "4":
            print(f"\n  当前认证模式: {status['auth_mode']}\n")
            print("  1) 设置密码认证")
            print("  2) 设置 Token 认证")
            print("  3) 关闭认证")
            print("  0) 返回\n")
            mode = input("  选择 [0-3]: ").strip()
            auth = cfg.setdefault("gateway", {}).setdefault("auth", {})
            if mode == "1":
                pw = input("  设置登录密码: ").strip()
                if pw:
                    auth["mode"] = "password"
                    auth["password"] = pw
                    auth.pop("token", None)
                    save_config(cfg)
                    print("  ✓ 密码认证已启用")
            elif mode == "2":
                tk = input("  设置 Token: ").strip()
                if tk:
                    auth["mode"] = "token"
                    auth["token"] = tk
                    auth.pop("password", None)
                    save_config(cfg)
                    print("  ✓ Token 认证已启用")
            elif mode == "3":
                auth["mode"] = "none"
                auth.pop("password", None)
                auth.pop("token", None)
                save_config(cfg)
                print("  ✓ 认证已关闭")
            _pause()
        elif choice == "5":
            configure_summarizer()
            _pause()
        elif choice == "6":
            configure_feishu()
            _pause()
        elif choice == "7":
            subprocess.Popen(["notepad", str(CONFIG_FILE)])
            print("  已使用系统编辑器打开配置文件")
            _pause()
        elif choice == "8":
            print()
            try:
                from whaleclaw.doctor.runner import Doctor

                async def _run() -> None:
                    doctor = Doctor()
                    results = await doctor.run_all()
                    print(doctor.format_report(results))

                asyncio.run(_run())
            except Exception as exc:
                print(f"  ✖ 运行 doctor 失败: {exc}")
            _pause()
        elif choice == "9":
            print("\n  完整配置内容:\n")
            print(json.dumps(cfg, ensure_ascii=False, indent=2))
            _pause()
        elif choice == "0":
            print("  再见")
            return 0
        else:
            print("  ✖ 无效选择")
            time.sleep(1)


def _find_listening_pid(port: int) -> int | None:
    proc = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, encoding="utf-8", errors="ignore")
    target = f":{port}"
    for line in proc.stdout.splitlines():
        row = line.strip()
        if "LISTENING" not in row or target not in row:
            continue
        parts = row.split()
        if len(parts) >= 5 and parts[-1].isdigit():
            return int(parts[-1])
    return None


def run_start_gateway() -> int:
    ensure_proxy_env()
    ensure_config()
    try:
        import whaleclaw  # noqa: F401
    except Exception:
        print("\n  📦 首次运行，正在安装依赖...")
        code = subprocess.call([sys.executable, "-m", "pip", "install", "-e", ".[dev]", "--quiet"])
        if code != 0:
            print("  ✖ 依赖安装失败")
            return code
        print("  ✓ 依赖安装完成")

    cfg = load_config()
    port = int(cfg.get("gateway", {}).get("port", 18666))
    bind = str(cfg.get("gateway", {}).get("bind", "127.0.0.1"))
    old_pid = _find_listening_pid(port)
    if old_pid:
        print(f"\n  ⚠ 端口 {port} 被占用 (PID: {old_pid})，正在释放...")
        subprocess.run(["taskkill", "/F", "/PID", str(old_pid)], capture_output=True, text=True)
        time.sleep(1)
        print("  ✓ 端口已释放")

    print("\n  🐋 WhaleClaw Gateway 正在启动...")
    print("  🎉 B站飞翔鲸祝您马年大吉！财源广进！WhaleClaw 免费开源！")
    print("  ---------------------------------")
    print(f"  🌐 WebChat:  http://{bind}:{port}")
    print(f"  📗 API:      http://{bind}:{port}/api/status")
    print(f"  📡 WS:       ws://{bind}:{port}/ws")
    print("\n  按 Ctrl+C 停止服务")
    print("  ---------------------------------\n")
    # Avoid forcing browser auto-open on Windows; some environments crash Chrome.
    return subprocess.call([sys.executable, "-m", "whaleclaw", "gateway", "run"])


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python -m whaleclaw.cli.windows_bridge [start|config]")
        return 1
    cmd = sys.argv[1].strip().lower()
    if cmd == "start":
        return run_start_gateway()
    if cmd == "config":
        return run_config_manager()
    print(f"Unknown command: {cmd}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
