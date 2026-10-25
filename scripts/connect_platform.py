#!/usr/bin/env python3
"""Connect a platform account through a fresh, visible local browser.

Examples:
    python scripts/connect_platform.py --platform-id 1
    python scripts/connect_platform.py --platform-id 1 --monitor-url https://monitor.example --username admin
    python scripts/connect_platform.py --platform-id 1 --resume-pending
    python scripts/connect_platform.py --dry-run

The source account is logged in manually. Credentials are read only from this
process's browser context. Local recovery files contain Fernet ciphertext only.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import importlib
import inspect
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ADAPTERS = {
    "xiaohong66": {"login_path": "/", "token_storage_key": "token", "session_path": "/api/customer/Profile/Show",
                   "native_hosts": {"xiaohong66.com", "www.xiaohong66.com"}},
    "fans8": {"login_path": "/indexPc.html#/login", "token_storage_key": "ACCESS_TOKEN", "session_path": "/api/userInfo",
              "native_hosts": {"fans8.top"}},
}


class ConnectionFailure(Exception):
    """A user-facing failure whose message contains no secrets."""

    def __init__(self, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


def _safe_error_summary(exc: Exception) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "", type(exc).__name__)[:80] or "ConnectionError"
    status = getattr(exc, "http_status", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is None:
        match = re.search(r"\bHTTP\s+([1-5]\d{2})\b", str(exc))
        status = int(match.group(1)) if match else None
    return name + (f" / HTTP {status}" if isinstance(status, int) and 100 <= status <= 599 else "")


def _private_permissions(path: Path) -> None:
    """Restrict a recovery file before replacing the managed ciphertext."""
    if os.name != "nt":
        path.chmod(0o600)
        return
    system32 = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    identity = subprocess.run([str(system32 / "whoami.exe"), "/user", "/fo", "csv", "/nh"],
                              capture_output=True, text=True, check=True, creationflags=flags)
    sid = re.search(r"\bS-1-\d+(?:-\d+)+\b", identity.stdout)
    if sid is None:
        raise ConnectionFailure("无法设置会话恢复文件权限，候选会话未保存。")
    subprocess.run([str(system32 / "icacls.exe"), str(path), "/inheritance:r", "/grant:r", "*" + sid.group(0) + ":(F)"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, creationflags=flags)


class PendingSessionStore:
    """Atomic, owner-readable encrypted snapshot; the key never leaves here."""

    def __init__(self, path: Path, key: str):
        try:
            fernet = importlib.import_module("cryptography.fernet")
            self._cipher = fernet.Fernet(key.encode("ascii"))
        except Exception:
            raise ConnectionFailure("会话恢复加密密钥无效。") from None
        self.path = path.resolve()
        self._last_digest: bytes | None = None

    def save(self, session: str) -> bool:
        digest = hashlib.sha256(session.encode("utf-8")).digest()
        if digest == self._last_digest and self.path.is_file():
            return False
        temporary: Path | None = None
        try:
            ciphertext = self._cipher.encrypt(session.encode("utf-8"))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, filename = tempfile.mkstemp(prefix=".pending-session-", suffix=".tmp", dir=self.path.parent)
            temporary = Path(filename)
            with os.fdopen(fd, "wb") as output:
                _private_permissions(temporary)
                output.write(ciphertext)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except Exception:
            raise ConnectionFailure("加密会话恢复文件保存失败，当前登录窗口会继续保留。") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self._last_digest = digest
        return True

    def load(self) -> str:
        try:
            if not self.path.is_file() or self.path.stat().st_size > 65536:
                raise ValueError
            return self._cipher.decrypt(self.path.read_bytes()).decode("utf-8")
        except Exception:
            raise ConnectionFailure("加密会话恢复文件不存在、无效或与当前密钥不匹配。") from None

    def remove(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise ConnectionFailure("平台已连接，但会话恢复文件清理失败。") from None


def pending_session_store(args: argparse.Namespace) -> PendingSessionStore | None:
    local = _loopback(str(urlsplit(args.monitor_url).hostname))
    explicit = getattr(args, "pending_file", None)
    key_env = getattr(args, "pending_key_env", None)
    resume = bool(getattr(args, "resume_pending", False))
    if not local:
        if not explicit and not key_env and not resume:
            return None
        if not explicit or not key_env or key_env == "ENCRYPTION_KEY":
            raise ConnectionFailure("远程后台恢复需显式指定 --pending-file 和独立的 --pending-key-env。")
    key_name = key_env or "ENCRYPTION_KEY"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_name):
        raise ConnectionFailure("会话恢复密钥环境变量名称无效。")
    key = os.getenv(key_name, "")
    if not key:
        if explicit or key_env or resume:
            raise ConnectionFailure("未设置会话恢复加密密钥。")
        return None
    path = Path(explicit) if explicit else ROOT / ".runtime" / f"pending-session-platform-{args.platform_id}.enc"
    if path.is_symlink():
        raise ConnectionFailure("会话恢复文件不能是符号链接。")
    return PendingSessionStore(path, key)


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _protected_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return host == "api.zxcbug.com" or host.endswith(".api.zxcbug.com")


def validate_monitor_url(value: str) -> str:
    parts = urlsplit(value)
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment
            or parts.path not in {"", "/"}):
        raise ConnectionFailure("监控后台地址应填写完整根网址，不包含账号、路径或查询参数。")
    if _protected_host(parts.hostname):
        raise ConnectionFailure("此地址不能用作监控后台。")
    if parts.scheme != "https" and not _loopback(parts.hostname):
        raise ConnectionFailure("远程监控后台需要使用 HTTPS 地址。")
    try:
        parts.port
    except ValueError:
        raise ConnectionFailure("监控后台端口无效。") from None
    return value.rstrip("/")


def platform_origin(platform: dict[str, Any]) -> str:
    if platform.get("adapter") not in SOURCE_ADAPTERS or platform.get("adapter_supported") is False:
        raise ConnectionFailure("当前平台版本尚未接入连接工具，请选择已支持的采集版本。")
    value = str(platform.get("base_url", ""))
    parts = urlsplit(value)
    if (parts.scheme != "https" or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment
            or parts.path not in {"", "/"}):
        raise ConnectionFailure("平台配置的根网址无效，请先在后台修正平台地址。")
    if _protected_host(parts.hostname):
        raise ConnectionFailure("此地址不能用作采集平台。")
    try:
        if parts.port not in {None, 443}:
            raise ConnectionFailure("平台网址需要使用标准 HTTPS 端口。")
    except ValueError:
        raise ConnectionFailure("平台网址端口无效。") from None
    return value.rstrip("/")


def source_login_settings(platform: dict[str, Any]) -> tuple[str, str]:
    """Use the API's adapter metadata while accepting older platform responses."""
    origin = platform_origin(platform)
    defaults = SOURCE_ADAPTERS[str(platform["adapter"])]
    login_path = platform.get("login_path", defaults["login_path"])
    storage_key = platform.get("token_storage_key", defaults["token_storage_key"])
    if (not isinstance(login_path, str) or not login_path.startswith("/") or login_path.startswith("//")
            or any(ord(char) < 32 for char in login_path) or "\\" in login_path):
        raise ConnectionFailure("平台登录入口配置无效，请先修正平台元数据。")
    parts = urlsplit(login_path)
    if parts.scheme or parts.netloc or parts.query:
        raise ConnectionFailure("平台登录入口必须是同源路径。")
    if not isinstance(storage_key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", storage_key):
        raise ConnectionFailure("平台会话存储字段配置无效。")
    return origin + login_path, storage_key


def _source_options(adapter: str, rule: dict[str, Any] | None = None) -> dict[str, Any]:
    # Keep the historical two-argument validator and waiter calls compatible
    # for the established native adapter, including locally patched helpers.
    options: dict[str, Any] = {"adapter": adapter} if adapter != "xiaohong66" else {}
    if rule is not None:
        options["rule"] = rule
    return options


async def source_rule(client: Any, platform: dict[str, Any], origin: str) -> dict[str, Any] | None:
    """Load an explicit allowlist from the selected platform's configured rule."""
    adapter = str(platform["adapter"])
    if str(urlsplit(origin).hostname).lower().rstrip(".") in SOURCE_ADAPTERS[adapter]["native_hosts"]:
        return None
    response = await api_json(client, "GET", f"/api/platforms/{platform['id']}/rules", action="读取平台采集规则")
    rules = response.get("items", []) if isinstance(response, dict) else []
    rules = [item for item in rules if isinstance(item, dict) and isinstance(item.get("config"), dict)]
    active_id = platform.get("active_rule_id")
    selected = next((item for item in rules if active_id is not None and item.get("id") == active_id), None)
    if selected is None:
        selected = max(rules, key=lambda item: item.get("version", 0) if isinstance(item.get("version"), int) else 0, default=None)
    if selected is None:
        raise ConnectionFailure("自定义平台域名需要先配置采集规则和 allowed_hosts。")
    return dict(selected["config"])


def installed_channels() -> list[str]:
    found: list[str] = []
    if sys.platform == "win32":
        roots = [Path(value) for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA") if (value := os.getenv(name))]
        if any((root / "Google/Chrome/Application/chrome.exe").is_file() for root in roots):
            found.append("chrome")
        if any((root / "Microsoft/Edge/Application/msedge.exe").is_file() for root in roots):
            found.append("msedge")
    elif sys.platform == "darwin":
        if Path("/Applications/Google Chrome.app").exists():
            found.append("chrome")
        if Path("/Applications/Microsoft Edge.app").exists():
            found.append("msedge")
    else:
        if shutil.which("google-chrome") or shutil.which("google-chrome-stable"):
            found.append("chrome")
        if shutil.which("microsoft-edge") or shutil.which("microsoft-edge-stable"):
            found.append("msedge")
    return found


def load_dependencies() -> tuple[Any, Any]:
    try:
        dotenv = importlib.import_module("dotenv")
        httpx = importlib.import_module("httpx")
        playwright = importlib.import_module("playwright.async_api")
    except ImportError:
        raise ConnectionFailure("缺少运行依赖，请执行 python -m pip install -r backend/requirements.txt。") from None
    dotenv.load_dotenv(ROOT / ".env", override=False)
    return httpx, playwright


def credentials(args: argparse.Namespace) -> tuple[str, str]:
    # A remote server or an explicitly selected user should never silently
    # receive the local deployment's administrator password from .env.
    local = _loopback(str(urlsplit(args.monitor_url).hostname))
    username = args.username or (os.getenv("ADMIN_USERNAME", "") if local else "") or input("监控后台管理员用户名：").strip()
    password = os.getenv("ADMIN_PASSWORD", "") if local and not args.username else ""
    if not password:
        password = getpass.getpass("监控后台管理员密码：")
    if not username or not password:
        raise ConnectionFailure("监控后台用户名和密码不能为空。")
    return username, password


async def api_json(client: Any, method: str, path: str, *, action: str, **kwargs: Any) -> Any:
    try:
        response = await client.request(method, path, **kwargs)
    except Exception as exc:
        raise ConnectionFailure(f"{action}失败（{_safe_error_summary(exc)}）。") from None
    if response.status_code < 200 or response.status_code >= 300:
        messages = {
            400: "请求或平台会话校验未通过",
            401: "管理员登录失效或账号密码错误",
            403: "需要管理员权限或请求校验未通过",
            404: "平台或接口不存在",
            429: "请求过于频繁，请稍后重试",
        }
        reason = messages.get(response.status_code, f"后台返回 HTTP {response.status_code}")
        raise ConnectionFailure(f"{action}失败：{reason}。", http_status=response.status_code)
    try:
        return response.json()
    except ValueError:
        raise ConnectionFailure(f"{action}失败：监控后台返回的数据无效。") from None


async def launch_visible_browser(playwright: Any, channel: str) -> Any:
    channels = [channel] if channel != "auto" else [*installed_channels(), "chrome", "msedge"]
    attempted: set[str] = set()
    for candidate in channels:
        if candidate in attempted:
            continue
        attempted.add(candidate)
        try:
            return await playwright.chromium.launch(channel=candidate, headless=False)
        except Exception:
            continue
    raise ConnectionFailure("浏览器启动失败，请安装 Chrome 或 Edge，或使用 --browser 指定已安装的浏览器。")


async def validate_source_session(origin: str, session: str, *, adapter: str = "xiaohong66", rule: dict[str, Any] | None = None) -> None:
    backend_path = str(ROOT / "backend")
    if backend_path not in sys.path:
        sys.path.insert(0, backend_path)
    from app.collectors import Xiaohong66Collector
    from app.collectors.fans8 import Fans8Collector
    from app.collectors.session import decode_session_envelope

    decode_session_envelope(session, origin)
    collectors = {"xiaohong66": Xiaohong66Collector, "fans8": Fans8Collector}
    source_class = collectors.get(adapter)
    if source_class is None:
        raise ConnectionFailure("当前平台版本尚未接入会话校验。")
    async with source_class(origin, session, rule={**dict(rule or {}), "max_attempts": 1}) as source:
        await source.check_session()


async def wait_for_source_token(browser: Any, context: Any, origin: str, timeout: int, capture_callback: Any = None,
                                *, adapter: str = "xiaohong66", token_storage_key: str | None = None,
                                rule: dict[str, Any] | None = None) -> str:
    backend_path = str(ROOT / "backend")
    if backend_path not in sys.path:
        sys.path.insert(0, backend_path)
    from app.collectors.session import encode_session_envelope

    defaults = SOURCE_ADAPTERS.get(adapter)
    if defaults is None:
        raise ConnectionFailure("当前平台版本尚未接入会话读取。")
    storage_key = token_storage_key or str(defaults["token_storage_key"])
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", storage_key):
        raise ConnectionFailure("平台会话存储字段配置无效。")
    storage_marker = "data" if adapter == "xiaohong66" else storage_key
    read_state = "() => ({token: window.localStorage.getItem(" + json.dumps(storage_key) + "), has_data: !!window.localStorage.getItem(" + json.dumps(storage_marker) + ")})"
    deadline = time.monotonic() + timeout
    next_diagnostic = 0.0
    next_validation = 0.0
    next_error_diagnostic = 0.0
    validation_notice = False
    while time.monotonic() < deadline:
        if not browser.is_connected():
            raise ConnectionFailure("登录浏览器已关闭，平台会话尚未连接。")
        if not context.pages:
            raise ConnectionFailure("登录页面已关闭，平台会话尚未连接。")
        diagnostic_page = context.pages[-1] if context.pages else None
        diagnostic_match, has_token, has_data = False, False, False
        for page in context.pages:
            if not _matches_source_origin(page.url, origin):
                continue
            diagnostic_page, diagnostic_match = page, True
            try:
                state = await page.evaluate(read_state)
            except Exception:
                # Manual login may navigate while the read is in flight.
                continue
            token = state.get("token") if isinstance(state, dict) else state
            has_data = bool(state.get("has_data")) if isinstance(state, dict) else False
            has_token = isinstance(token, str) and bool(token.strip())
            token = token.strip() if isinstance(token, str) and 10 <= len(token.strip()) <= 16384 else ""
            if (token or has_data) and time.monotonic() >= next_validation:
                try:
                    parts = urlsplit(page.url)
                    credential_origin = f"{parts.scheme}://{parts.netloc}"
                    cookies = await context.cookies([credential_origin, credential_origin + str(defaults["session_path"])])
                    session = encode_session_envelope(credential_origin, token=token, cookies=cookies)
                    if capture_callback is not None:
                        captured = capture_callback(session)
                        if inspect.isawaitable(captured):
                            await captured
                    await validate_source_session(origin, session, **_source_options(adapter, rule))
                    print("平台账户资料校验通过，正在连接监控后台。", flush=True)
                    return session
                except Exception as exc:
                    # Cookies may be committed just after the UI login marker;
                    # never discard the browser or declare success too early.
                    if not validation_notice:
                        print("已发现登录标记，平台会话尚未就绪，保留窗口继续校验。", flush=True)
                        validation_notice = True
                    if time.monotonic() >= next_error_diagnostic:
                        print("平台会话校验暂未通过（" + _safe_error_summary(exc) + "），30 秒后重试。", flush=True)
                        next_error_diagnostic = time.monotonic() + 30
                finally:
                    next_validation = time.monotonic() + 30
        if time.monotonic() >= next_diagnostic:
            title = "无页面"
            if diagnostic_page is not None:
                try:
                    title = _safe_diagnostic_title(await diagnostic_page.title())
                except Exception:
                    title = "页面导航中"
            print(f"登录等待：目标源匹配={diagnostic_match}；页面标题={title}；发现会话={has_token}；发现登录标记={has_data}", flush=True)
            next_diagnostic = time.monotonic() + 30
        await asyncio.sleep(0.5)
    raise ConnectionFailure("等待人工登录超时，平台会话尚未连接。")


def _same_origin(url: str, expected_url: str) -> bool:
    try:
        current, expected = urlsplit(url), urlsplit(expected_url)
        return (current.scheme, current.hostname, current.port or 443) == (expected.scheme, expected.hostname, expected.port or 443)
    except ValueError:
        return False


def _matches_source_origin(url: str, expected_url: str) -> bool:
    if _same_origin(url, expected_url):
        return True
    # The same source may canonically redirect its apex address to www.
    # This exception is limited to the adapter's two established hostnames.
    try:
        current, expected = urlsplit(url), urlsplit(expected_url)
        native = {"xiaohong66.com", "www.xiaohong66.com"}
        return (current.scheme == expected.scheme == "https"
                and current.hostname in native and expected.hostname in native
                and current.port in {None, 443} and expected.port in {None, 443})
    except ValueError:
        return False


def _safe_diagnostic_title(value: str) -> str:
    # Titles can include account names. Only known static labels are emitted;
    # arbitrary titles are replaced instead of attempting to guess a username.
    title = re.sub(r"\s+", " ", str(value)).strip()[:80]
    known = {"", "登录", "首页", "商品详情", "注册", "重置密码", "亿乐海外运营学习站",
             "Just a moment...", "安全验证", "安全检查", "请稍候", "Forbidden", "Access denied",
             "正在检查您的浏览器", "无法访问此网站", "xiaohong66.com", "www.xiaohong66.com"}
    return title if title in known else "[动态标题已隐藏]"


def _navigation_diagnostic(exc: Exception) -> str:
    """Expose only an exception class and a Chromium network error code."""
    name = re.sub(r"[^A-Za-z0-9_]", "", type(exc).__name__)[:80] or "NavigationError"
    code = re.search(r"\bnet::ERR_[A-Z0-9_]+\b", str(exc))
    return name + (" / " + code.group(0) if code else "")


async def connect(args: argparse.Namespace, httpx: Any, playwright_module: Any) -> None:
    store = pending_session_store(args)
    pending = store.load() if getattr(args, "resume_pending", False) and store is not None else None
    username, password = credentials(args)
    async with httpx.AsyncClient(base_url=args.monitor_url, timeout=30, follow_redirects=False, trust_env=False) as client:
        login = await api_json(client, "POST", "/api/auth/login", action="后台登录", json={"username": username, "password": password})
        password = ""
        if not isinstance(login, dict) or login.get("role") != "admin" or not login.get("csrf_token"):
            raise ConnectionFailure("后台登录失败：连接平台需要管理员账号。")
        csrf = str(login["csrf_token"])
        platforms = await api_json(client, "GET", "/api/platforms", action="读取平台配置")
        rows = platforms.get("items", []) if isinstance(platforms, dict) else []
        platform = next((row for row in rows if isinstance(row, dict) and row.get("id") == args.platform_id), None)
        if platform is None:
            raise ConnectionFailure("指定平台不存在，请先在监控后台创建平台。")
        origin = platform_origin(platform)
        adapter = str(platform["adapter"])
        login_url, storage_key = source_login_settings(platform)
        rule = await source_rule(client, platform, origin)
        if pending is not None:
            try:
                await validate_source_session(origin, pending, **_source_options(adapter, rule))
                await api_json(client, "PUT", f"/api/platforms/{args.platform_id}/session", action="恢复平台会话", json={"token": pending}, headers={"X-CSRF-Token": csrf})
            except Exception as exc:
                raise ConnectionFailure("恢复会话未完成（" + _safe_error_summary(exc) + "），加密文件已保留。") from None
            finally:
                pending = None
            store.remove()
            print(f"平台 {args.platform_id} 会话恢复连接成功，加密恢复文件已删除。", flush=True)
            return

        capture_notice = False

        def capture_candidate(session: str) -> None:
            nonlocal capture_notice
            if store is not None:
                store.save(session)
                if not capture_notice:
                    print("已加密保存候选会话，校验失败时可使用 --resume-pending 恢复。", flush=True)
                    capture_notice = True

        async with playwright_module.async_playwright() as playwright:
            browser = await launch_visible_browser(playwright, args.browser)
            try:
                context = await browser.new_context()
                page = await context.new_page()
                print("请在已打开的独立浏览器中手动登录平台账号；完成登录后将自动连接。", flush=True)
                try:
                    await page.goto(login_url, wait_until="domcontentloaded", timeout=args.navigation_timeout * 1000)
                except playwright_module.TimeoutError as exc:
                    if _same_origin(page.url, origin):
                        print("平台页面加载较慢，继续等待人工登录。", flush=True)
                    else:
                        raise ConnectionFailure("平台页面加载失败（" + _navigation_diagnostic(exc) + "）。") from None
                except Exception as exc:
                    raise ConnectionFailure("平台页面加载失败（" + _navigation_diagnostic(exc) + "）。") from None
                connection_deadline = time.monotonic() + args.timeout
                next_upload_diagnostic = 0.0
                while time.monotonic() < connection_deadline:
                    wait_options = _source_options(adapter, rule)
                    if storage_key != SOURCE_ADAPTERS["xiaohong66"]["token_storage_key"] or adapter != "xiaohong66":
                        wait_options["token_storage_key"] = storage_key
                    session = await wait_for_source_token(browser, context, origin, max(1, int(connection_deadline - time.monotonic())),
                                                          capture_callback=capture_candidate, **wait_options)
                    try:
                        await api_json(client, "PUT", f"/api/platforms/{args.platform_id}/session", action="连接平台会话", json={"token": session}, headers={"X-CSRF-Token": csrf})
                    except ConnectionFailure as exc:
                        if time.monotonic() >= next_upload_diagnostic:
                            print("后台暂未接受平台会话（" + _safe_error_summary(exc) + "），保留当前窗口，30 秒后重试。", flush=True)
                            next_upload_diagnostic = time.monotonic() + 30
                        await asyncio.sleep(30)
                    else:
                        if store is not None:
                            store.remove()
                        print(f"平台 {args.platform_id} 会话连接成功，登录浏览器即将关闭。", flush=True)
                        break
                    finally:
                        session = ""
                else:
                    raise ConnectionFailure("等待平台连接超时，平台会话尚未连接。")
            finally:
                await browser.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="通过独立 Chrome/Edge 人工登录并连接采集平台账号。")
    parser.add_argument("--platform-id", type=int, help="监控后台中的平台编号")
    parser.add_argument("--monitor-url", default="http://127.0.0.1:28671", help="监控后台根网址，默认 http://127.0.0.1:28671")
    parser.add_argument("--username", help="监控后台管理员用户名；指定后通过隐藏输入读取密码")
    parser.add_argument("--timeout", type=int, default=1200, help="等待人工登录的秒数，默认 1200")
    parser.add_argument("--navigation-timeout", type=int, default=60, help="等待页面导航的秒数，默认 60；同源慢页面继续等待人工登录")
    parser.add_argument("--browser", choices=("auto", "chrome", "msedge"), default="auto")
    parser.add_argument("--resume-pending", action="store_true", help="恢复加密候选会话，不启动浏览器；失败时保留恢复文件")
    parser.add_argument("--pending-file", type=Path, help="显式指定加密候选会话文件；本机默认保存在 .runtime")
    parser.add_argument("--pending-key-env", help="加密恢复文件使用的密钥环境变量名；远程后台需使用独立密钥")
    parser.add_argument("--dry-run", action="store_true", help="只检查参数和依赖，不发起连接或打开浏览器")
    args = parser.parse_args(argv)
    if not args.dry_run and (args.platform_id is None or args.platform_id < 1):
        parser.error("运行连接时需要 --platform-id 正整数")
    if args.timeout < 1:
        parser.error("--timeout 需要为正整数")
    if args.navigation_timeout < 1:
        parser.error("--navigation-timeout 需要为正整数")
    args.monitor_url = validate_monitor_url(args.monitor_url)
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        httpx, playwright_module = load_dependencies()
        if args.dry_run:
            browsers = installed_channels()
            suffix = "、".join(browsers) if browsers else "未检测到标准安装位置，实际运行时将尝试 Chrome/Edge 通道"
            print(f"依赖检查通过：httpx、python-dotenv、Playwright；浏览器：{suffix}；未启动浏览器。")
            return 0
        asyncio.run(connect(args, httpx, playwright_module))
        return 0
    except ConnectionFailure as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("连接已取消。", file=sys.stderr)
        return 130
    except Exception:
        # Avoid traceback/HTTP payload diagnostics that could reveal a token.
        print("连接未完成，请检查监控后台和本机浏览器是否正常运行。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
