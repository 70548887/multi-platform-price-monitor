"""Offline regression checks for the local, manually operated connector."""

import asyncio
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import sys
import types
import unittest
from unittest.mock import patch

import httpx
from cryptography.fernet import Fernet


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("platform_connector", ROOT / "scripts/connect_platform.py")
connector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(connector)
sys.path.insert(0, str(ROOT / "backend"))
import app.collectors as adapters
import app.collectors.fans8 as fans8_adapters
from app.collectors.session import SESSION_PREFIX, decode_session_envelope


class Clock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


class Page:
    url = "https://www.xiaohong66.com/"

    async def goto(self, url, **kwargs):
        self.url = url

    async def evaluate(self, code):
        return {"token": None, "has_data": True}

    async def title(self):
        return "亿乐海外运营学习站"


class Context:
    def __init__(self, settle=True):
        self.pages = [Page()]
        self.settle = settle
        self.cookie_reads = 0

    async def new_page(self):
        return self.pages[0]

    async def cookies(self, urls):
        self.cookie_reads += 1
        if self.settle and self.cookie_reads == 1:
            return []
        return [{"name": "PHPSESSID", "value": "unit-cookie-secret", "domain": ".xiaohong66.com",
                 "path": "/", "secure": True, "expires": -1}]


class Browser:
    def __init__(self, context):
        self.context = context
        self.closed = False

    def is_connected(self):
        return not self.closed

    async def new_context(self):
        return self.context

    async def close(self):
        self.closed = True


class Runtime:
    def __init__(self, playwright):
        self.playwright = playwright

    async def __aenter__(self):
        return self.playwright

    async def __aexit__(self, *args):
        pass


class ConnectorTests(unittest.IsolatedAsyncioTestCase):
    def test_default_monitor_uses_the_local_test_port(self):
        args = connector.parse_args(["--dry-run"])
        self.assertEqual(args.monitor_url, "http://127.0.0.1:28671")

    async def test_empty_pages_fail_even_if_browser_driver_remains_connected(self):
        context = Context()
        context.pages = []
        browser = Browser(context)
        with self.assertRaisesRegex(connector.ConnectionFailure, "登录页面已关闭"):
            await connector.wait_for_source_token(browser, context, "https://www.xiaohong66.com", 7200)

    async def test_disconnected_browser_fails_immediately(self):
        context = Context()
        browser = Browser(context)
        browser.closed = True
        with self.assertRaisesRegex(connector.ConnectionFailure, "登录浏览器已关闭"):
            await connector.wait_for_source_token(browser, context, "https://www.xiaohong66.com", 7200)

    def test_diagnostic_titles_hide_unknown_account_text(self):
        self.assertEqual(connector._safe_diagnostic_title("用户 private-account 的首页"), "[动态标题已隐藏]")
        self.assertEqual(connector._safe_diagnostic_title("亿乐海外运营学习站"), "亿乐海外运营学习站")

    def test_source_origin_aliases_are_limited_to_apex_and_www(self):
        self.assertTrue(connector._matches_source_origin("https://xiaohong66.com/login", "https://www.xiaohong66.com"))
        self.assertFalse(connector._matches_source_origin("https://other.xiaohong66.com/login", "https://www.xiaohong66.com"))

    def test_fans8_login_metadata_and_older_response_defaults(self):
        platform = {"id": 2, "adapter": "fans8", "base_url": "https://fans8.top"}
        self.assertEqual(connector.platform_origin(platform), "https://fans8.top")
        self.assertEqual(connector.source_login_settings(platform), ("https://fans8.top/indexPc.html#/login", "ACCESS_TOKEN"))
        platform.update(login_path="/indexPc.html#/login", token_storage_key="ACCESS_TOKEN", adapter_supported=True)
        self.assertEqual(connector.source_login_settings(platform), ("https://fans8.top/indexPc.html#/login", "ACCESS_TOKEN"))

    def test_login_metadata_rejects_foreign_urls_and_unverified_adapters(self):
        platform = {"id": 2, "adapter": "fans8", "base_url": "https://fans8.top"}
        for path in ("https://other.example/login", "//other.example/login", "/\\other.example/login", "/login?token=private"):
            with self.subTest(path=path), self.assertRaises(connector.ConnectionFailure):
                connector.source_login_settings({**platform, "login_path": path})
        with self.assertRaises(connector.ConnectionFailure):
            connector.platform_origin({**platform, "adapter": "yike"})
        with self.assertRaises(connector.ConnectionFailure):
            connector.platform_origin({**platform, "adapter_supported": False})
        self.assertFalse(connector._matches_source_origin("https://www.fans8.top", "https://fans8.top"))

    async def test_fans8_session_validation_dispatches_with_rule_and_one_attempt(self):
        from app.collectors.session import encode_session_envelope
        credential = encode_session_envelope("https://fans8.top", token="fixture-fans8-token")
        seen = []

        class Source:
            def __init__(self, origin, session, rule=None):
                seen.append((origin, session, rule))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def check_session(self):
                return True

        with patch.object(fans8_adapters, "Fans8Collector", Source):
            await connector.validate_source_session("https://fans8.top", credential, adapter="fans8", rule={"max_attempts": 3, "site_admin_id": 1})
        self.assertEqual(seen, [("https://fans8.top", credential, {"max_attempts": 1, "site_admin_id": 1})])

    async def test_custom_source_domain_uses_active_rule_allowlist(self):
        platform = {"id": 2, "adapter": "fans8", "base_url": "https://custom-source.net", "active_rule_id": 4}

        async def handler(request):
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.url.path, "/api/platforms/2/rules")
            return httpx.Response(200, json={"items": [
                {"id": 5, "version": 2, "config": {"allowed_hosts": ["different-source.net"]}},
                {"id": 4, "version": 1, "config": {"allowed_hosts": ["custom-source.net"]}},
            ]})

        async with httpx.AsyncClient(base_url="http://127.0.0.1", transport=httpx.MockTransport(handler)) as client:
            self.assertEqual(await connector.source_rule(client, platform, "https://custom-source.net"), {"allowed_hosts": ["custom-source.net"]})

    async def test_fans8_login_navigation_storage_capture_validation_upload_order(self):
        output, events = io.StringIO(), []

        class Fans8Page(Page):
            async def goto(self, url, **kwargs):
                self.url = url
                events.append(("navigate", url))

            async def evaluate(self, code):
                self_test.assertIn('getItem("ACCESS_TOKEN")', code)
                return {"token": "fixture-fans8-token", "has_data": True}

        class Fans8Context(Context):
            def __init__(self):
                self.pages = [Fans8Page()]

            async def cookies(self, urls):
                self_test.assertEqual(urls, ["https://fans8.top", "https://fans8.top/api/userInfo"])
                return [{"name": "source-session-name", "value": "fixture-fans8-cookie", "domain": "fans8.top", "path": "/", "secure": True}]

        class Store:
            def save(self, session):
                events.append(("capture", session))

            def remove(self):
                events.append(("remove", None))

        context = Fans8Context()
        browser = Browser(context)
        self_test = self

        class Chromium:
            async def launch(self, **kwargs):
                return browser

        async def validation(origin, session, **kwargs):
            self.assertEqual(events[-1], ("capture", session))
            decoded = decode_session_envelope(session, origin)
            self.assertEqual(decoded.token, "fixture-fans8-token")
            self.assertEqual(decoded.cookies[0]["name"], "source-session-name")
            self.assertEqual(kwargs, {"adapter": "fans8"})
            events.append(("validate", None))

        async def handler(request):
            if request.url.path == "/api/auth/login":
                return httpx.Response(200, json={"role": "admin", "csrf_token": "fixture-csrf"})
            if request.url.path == "/api/platforms":
                return httpx.Response(200, json={"items": [{"id": 2, "adapter": "fans8", "base_url": "https://fans8.top",
                                                            "login_path": "/indexPc.html#/login", "token_storage_key": "ACCESS_TOKEN"}]})
            self.assertEqual(request.url.path, "/api/platforms/2/session")
            self.assertEqual(request.method, "PUT")
            self.assertEqual(events[-1][0], "validate")
            events.append(("upload", None))
            return httpx.Response(200, json={"ok": True})

        fake_http = types.SimpleNamespace(AsyncClient=lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs))
        fake_playwright = types.SimpleNamespace(TimeoutError=TimeoutError, async_playwright=lambda: Runtime(types.SimpleNamespace(chromium=Chromium())))
        args = connector.parse_args(["--platform-id", "2"])
        with patch.object(connector, "pending_session_store", return_value=Store()), patch.object(connector, "credentials", return_value=("fixture-admin", "fixture-password")), patch.object(connector, "validate_source_session", validation), contextlib.redirect_stdout(output):
            await connector.connect(args, fake_http, fake_playwright)
        self.assertEqual([event[0] for event in events], ["navigate", "capture", "validate", "upload", "remove"])
        self.assertEqual(events[0][1], "https://fans8.top/indexPc.html#/login")
        self.assertTrue(browser.closed)
        self.assertNotIn("fixture-fans8-token", output.getvalue())
        self.assertNotIn("fixture-fans8-cookie", output.getvalue())

    async def test_cookie_settlement_and_profile_retry_preserve_the_browser(self):
        context = Context()
        browser = Browser(context)
        clock = Clock()
        output = io.StringIO()
        attempts = []

        class Source:
            def __init__(self, origin, credential, rule=None):
                decoded = decode_session_envelope(credential, origin)
                assert decoded.cookies and not decoded.token
                assert rule == {"max_attempts": 1}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def check_session(self):
                attempts.append(True)
                if len(attempts) == 1:
                    raise adapters.AuthExpired("cookie not settled")
                return True

        with patch.object(adapters, "Xiaohong66Collector", Source), patch.object(connector.time, "monotonic", clock.monotonic), patch.object(connector.asyncio, "sleep", clock.sleep), contextlib.redirect_stdout(output):
            credential = await connector.wait_for_source_token(browser, context, "https://www.xiaohong66.com", 100)
        self.assertTrue(credential.startswith(SESSION_PREFIX))
        self.assertGreaterEqual(context.cookie_reads, 3)
        self.assertEqual(len(attempts), 2)
        self.assertFalse(browser.closed)
        self.assertNotIn("unit-cookie-secret", output.getvalue())

    async def test_backend_rejection_retries_before_closing_the_authenticated_browser(self):
        context = Context(settle=False)
        browser = Browser(context)
        clock = Clock()
        output = io.StringIO()
        uploads = []

        class Chromium:
            async def launch(self, **kwargs):
                return browser

        async def source_session(*args, capture_callback=None):
            from app.collectors.session import encode_session_envelope
            return encode_session_envelope("https://www.xiaohong66.com", cookies=await context.cookies([]))

        async def handler(request):
            if request.url.path == "/api/auth/login":
                return httpx.Response(200, json={"role": "admin", "csrf_token": "unit-csrf"})
            if request.url.path == "/api/platforms":
                return httpx.Response(200, json={"items": [{"id": 1, "adapter": "xiaohong66", "base_url": "https://www.xiaohong66.com"}]})
            self.assertFalse(browser.closed)
            self.assertEqual(request.headers["X-CSRF-Token"], "unit-csrf")
            uploads.append(json.loads(request.content)["token"])
            return httpx.Response(400 if len(uploads) == 1 else 200, json={"ok": True})

        fake_http = types.SimpleNamespace(AsyncClient=lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs))
        fake_playwright = types.SimpleNamespace(TimeoutError=TimeoutError, async_playwright=lambda: Runtime(types.SimpleNamespace(chromium=Chromium())))
        args = connector.parse_args(["--platform-id", "1", "--timeout", "100"])
        with patch.object(connector, "pending_session_store", return_value=None), patch.object(connector, "credentials", return_value=("unit-admin", "unit-password")), patch.object(connector, "wait_for_source_token", source_session), patch.object(connector.time, "monotonic", clock.monotonic), patch.object(connector.asyncio, "sleep", clock.sleep), contextlib.redirect_stdout(output):
            await connector.connect(args, fake_http, fake_playwright)
        self.assertEqual(len(uploads), 2)
        self.assertTrue(all(value.startswith(SESSION_PREFIX) for value in uploads))
        self.assertTrue(browser.closed)
        self.assertNotIn("unit-cookie-secret", output.getvalue())
        self.assertIn("连接成功", output.getvalue())

    def test_pending_file_contains_only_ciphertext_and_replaces_atomically(self):
        key = Fernet.generate_key().decode("ascii")
        candidate = SESSION_PREFIX + '{"cookies":[{"value":"unit-private-cookie"}]}'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pending.enc"
            store = connector.PendingSessionStore(path, key)
            self.assertTrue(store.save(candidate))
            self.assertNotIn(b"unit-private-cookie", path.read_bytes())
            self.assertEqual(store.load(), candidate)
            first_ciphertext = path.read_bytes()
            self.assertFalse(store.save(candidate))
            self.assertEqual(path.read_bytes(), first_ciphertext)
            self.assertTrue(store.save(candidate + " "))
            self.assertEqual(store.load(), candidate + " ")
            self.assertEqual([entry.name for entry in Path(directory).iterdir()], ["pending.enc"])
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            store.remove()
            self.assertFalse(path.exists())

    def test_pending_file_wrong_key_is_rejected_without_deleting_ciphertext(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pending.enc"
            first = connector.PendingSessionStore(path, Fernet.generate_key().decode("ascii"))
            first.save("unit-private-cookie")
            wrong = connector.PendingSessionStore(path, Fernet.generate_key().decode("ascii"))
            with self.assertRaisesRegex(connector.ConnectionFailure, "不匹配") as error:
                wrong.load()
            self.assertNotIn("unit-private-cookie", str(error.exception))
            self.assertTrue(path.is_file())

    def test_pending_permissions_failure_does_not_replace_prior_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pending.enc"
            store = connector.PendingSessionStore(path, Fernet.generate_key().decode("ascii"))
            store.save("unit-original-session")
            with patch.object(connector, "_private_permissions", side_effect=PermissionError("private detail")):
                with self.assertRaises(connector.ConnectionFailure):
                    store.save("unit-new-session")
            self.assertEqual(store.load(), "unit-original-session")
            self.assertEqual([entry.name for entry in Path(directory).iterdir()], ["pending.enc"])

    def test_remote_backend_never_implicitly_uses_root_encryption_key(self):
        key = Fernet.generate_key().decode("ascii")
        args = connector.parse_args(["--platform-id", "1", "--monitor-url", "https://monitor.example"])
        with patch.dict(os.environ, {"ENCRYPTION_KEY": key}, clear=False):
            self.assertIsNone(connector.pending_session_store(args))
            args.pending_file = Path("remote.enc")
            with self.assertRaisesRegex(connector.ConnectionFailure, "独立"):
                connector.pending_session_store(args)
            args.pending_key_env = "ENCRYPTION_KEY"
            with self.assertRaisesRegex(connector.ConnectionFailure, "独立"):
                connector.pending_session_store(args)

    def test_remote_credentials_do_not_use_local_dotenv_admin_account(self):
        args = connector.parse_args(["--platform-id", "1", "--monitor-url", "https://monitor.example"])
        with patch.dict(os.environ, {"ADMIN_USERNAME": "local-private-user", "ADMIN_PASSWORD": "local-private-password"}, clear=False), patch("builtins.input", return_value="remote-user") as username_input, patch.object(connector.getpass, "getpass", return_value="remote-password") as password_input:
            self.assertEqual(connector.credentials(args), ("remote-user", "remote-password"))
        username_input.assert_called_once()
        password_input.assert_called_once()

    def test_remote_explicit_pending_uses_only_separate_local_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "remote.enc"
            args = connector.parse_args(["--platform-id", "1", "--monitor-url", "https://monitor.example", "--pending-file", str(path), "--pending-key-env", "CONNECTOR_PRIVATE_KEY"])
            with patch.dict(os.environ, {"ENCRYPTION_KEY": "invalid-root-key", "CONNECTOR_PRIVATE_KEY": Fernet.generate_key().decode("ascii")}, clear=False):
                store = connector.pending_session_store(args)
                store.save("private-candidate")
                self.assertEqual(store.load(), "private-candidate")

    async def test_failed_validation_captures_before_request_and_is_rate_limited(self):
        context, clock, output = Context(settle=False), Clock(), io.StringIO()
        browser = Browser(context)
        captured, validations = [], []

        def capture(value):
            captured.append((clock.now, value))

        async def fail_validation(origin, session):
            self.assertTrue(captured)
            self.assertEqual(captured[-1][1], session)
            validations.append(clock.now)
            raise httpx.HTTPStatusError("private-cookie-must-not-leak", request=httpx.Request("GET", "https://www.xiaohong66.com"), response=httpx.Response(500))

        with patch.object(connector, "validate_source_session", fail_validation), patch.object(connector.time, "monotonic", clock.monotonic), patch.object(connector.asyncio, "sleep", clock.sleep), contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(connector.ConnectionFailure, "超时"):
                await connector.wait_for_source_token(browser, context, "https://www.xiaohong66.com", 61, capture_callback=capture)
        self.assertEqual(validations, [0, 30.0, 60.0])
        self.assertFalse(browser.closed)
        self.assertEqual(output.getvalue().count("HTTP 500"), 3)
        self.assertNotIn("private-cookie-must-not-leak", output.getvalue())

    async def _resume_case(self, *, validation_failure=False, upload_failure=False):
        from app.collectors.session import encode_session_envelope
        output, calls, validated = io.StringIO(), [], []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pending.enc"
            store = connector.PendingSessionStore(path, Fernet.generate_key().decode("ascii"))
            candidate = encode_session_envelope("https://www.xiaohong66.com", cookies=await Context(settle=False).cookies([]))
            store.save(candidate)

            async def validation(origin, session):
                validated.append((origin, session))
                if validation_failure:
                    raise adapters.AuthExpired("unit-private-cookie")

            async def handler(request):
                calls.append(request)
                if request.url.path == "/api/auth/login":
                    return httpx.Response(200, json={"role": "admin", "csrf_token": "unit-csrf"})
                if request.url.path == "/api/platforms":
                    return httpx.Response(200, json={"items": [{"id": 1, "adapter": "xiaohong66", "base_url": "https://www.xiaohong66.com"}]})
                self.assertEqual(json.loads(request.content), {"token": candidate})
                return httpx.Response(400 if upload_failure else 200, json={"detail": "unit-private-cookie"})

            fake_http = types.SimpleNamespace(AsyncClient=lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs))
            fake_playwright = types.SimpleNamespace(async_playwright=lambda: self.fail("resume must not launch or initialize any browser"))
            args = connector.parse_args(["--platform-id", "1", "--resume-pending"])
            error = None
            with patch.object(connector, "pending_session_store", return_value=store), patch.object(connector, "credentials", return_value=("unit-admin", "unit-password")), patch.object(connector, "validate_source_session", validation), contextlib.redirect_stdout(output):
                try:
                    await connector.connect(args, fake_http, fake_playwright)
                except connector.ConnectionFailure as exc:
                    error = exc
            self.assertEqual(len(validated), 1)
            self.assertNotIn("unit-private-cookie", output.getvalue())
            if validation_failure or upload_failure:
                self.assertIsNotNone(error)
                self.assertNotIn("unit-private-cookie", str(error))
                self.assertTrue(path.exists())
                self.assertEqual(store.load(), candidate)
            else:
                self.assertIsNone(error)
                self.assertFalse(path.exists())
                self.assertIn("恢复连接成功", output.getvalue())
            return calls

    async def test_resume_uploads_without_browser_then_deletes_snapshot(self):
        calls = await self._resume_case()
        self.assertEqual([request.method for request in calls], ["POST", "GET", "PUT"])

    async def test_resume_validation_failure_retains_encrypted_snapshot(self):
        calls = await self._resume_case(validation_failure=True)
        self.assertEqual([request.method for request in calls], ["POST", "GET"])

    async def test_resume_upload_failure_retains_encrypted_snapshot(self):
        await self._resume_case(upload_failure=True)


if __name__ == "__main__":
    unittest.main()
