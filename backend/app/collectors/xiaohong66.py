"""Read-only adapter for the xiaohong66 customer catalogue.

Request field names and response paths are versioned rule configuration. The
adapter does not assume that a missing page means an empty or deleted catalogue.
"""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
import re
import socket
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from http.cookiejar import Cookie
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import httpx

from .base import AuthExpired, CollectorError, IncompleteCollection, SourceProduct
from .session import decode_session_envelope, normalize_https_origin


_TOKEN_KEYS = {"token", "authorization", "cookie", "cookies", "set-cookie", "password", "access_token", "refresh_token", "secret"}

# Public client contract and authenticated request encoding observed on
# 2026-10-25. Full catalogue completeness is checked independently per batch.
DEFAULT_RULE: dict[str, Any] = {
    "api_prefix": "/api",
    "category_path": "/customer/Home/GoodsCategoryList",
    "list_path": "/customer/Home/GoodsList",
    "detail_path": "/customer/Home/GetGoods",
    "session_path": "/customer/Profile/Show",
    "session_method": "GET",
    "session_data_path": "data",
    "page_field": "page",
    "size_field": "list_rows",
    "page_size": 50,
    "category_field": "goods_category_id",
    "all_category_value": 0,
    "detail_id_field": "id",
    "list_extra": {"goods_name": ""},
    "category_data_path": "data",
    "list_data_path": "data.data",
    "list_total_path": "data.count",
    "detail_data_path": "data",
    "success_codes": [0, "0"],
    "auth_expired_codes": [100, "100", 110, "110", 401, "401", 403, "403", -401, "-401"],
    "timeout_seconds": 30,
    "max_attempts": 3,
    "per_category": False,
    "request_encoding": "json",
    "request_content_type": "application/json",
    "user_agent": "Mozilla/5.0",
}


def _at(value: Any, path: str | None, default: Any = None) -> Any:
    if not path:
        return value
    current = value
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def _json_safe(value: Any, secret: str | tuple[str, ...] = "") -> Any:
    """Preserve source decimals while excluding secrets from persisted evidence."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v, secret) for k, v in value.items() if str(k).lower() not in _TOKEN_KEYS}
    if isinstance(value, (tuple, list)):
        return [_json_safe(v, secret) for v in value]
    if isinstance(value, str) and secret:
        for credential in ((secret,) if isinstance(secret, str) else secret):
            if credential:
                value = value.replace(credential, "[redacted]")
        return value
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _decimal(value: Any, field: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise CollectorError(f"Missing or invalid decimal field: {field}")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise CollectorError(f"Invalid decimal field: {field}") from exc
    if not result.is_finite():
        raise CollectorError(f"Non-finite decimal field: {field}")
    return result


class Xiaohong66Collector:
    """Fetch one account's catalogue without placing orders or changing it.

    ``request_gate`` is invoked before *every* HTTP attempt, including retries.
    The worker supplies the shared platform rate limiter through that callback.
    Tests can inject an HTTPX client or transport without reaching a real source.
    """

    def __init__(
        self,
        base_url: str = "https://www.xiaohong66.com",
        token: str = "",
        rule: dict[str, Any] | None = None,
        request_gate: Callable[[], Awaitable[None]] | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.rule = {**DEFAULT_RULE, **dict(rule or {})}
        self.base_url = base_url.rstrip("/")
        self._session = decode_session_envelope(token.strip(), self.base_url)
        self._request_origin = self._session.origin
        self._token = self._session.token
        self._secrets = tuple(x for x in (self._token, *(cookie["value"] for cookie in self._session.cookies)) if x)
        self._request_gate = request_gate
        self._sleep = sleep
        self._validate_url_shape(self.base_url)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(float(self.rule.get("timeout_seconds", 30))),
            follow_redirects=False,
            transport=transport,
            trust_env=False,
        )
        for cookie in self._session.cookies:
            self._client.cookies.jar.set_cookie(Cookie(
                version=0, name=cookie["name"], value=cookie["value"], port=None, port_specified=False,
                domain=cookie["domain"], domain_specified=cookie["domain"].startswith("."),
                domain_initial_dot=cookie["domain"].startswith("."), path=cookie["path"], path_specified=True,
                secure=cookie["secure"], expires=int(cookie["expires"]) if cookie["expires"] > 0 else None,
                discard=cookie["expires"] <= 0, comment=None, comment_url=None, rest={}, rfc2109=False,
            ))
        self._mock_transport = isinstance(transport, httpx.MockTransport)
        self.expected_total: int | None = None
        self.seen_ids: set[str] = set()
        self.categories: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.account_identity: str | None = None
        self._validated_hosts: set[str] = set()

    def _validate_url_shape(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise CollectorError("Source URL must be an absolute HTTPS URL")
        if parts.username or parts.password or parts.fragment:
            raise CollectorError("Source URL cannot contain credentials or a fragment")
        host = parts.hostname.lower().rstrip(".")
        native = host == "xiaohong66.com" or host.endswith(".xiaohong66.com")
        explicit = {str(x).lower().rstrip(".") for x in self.rule.get("allowed_hosts", [])}
        if not native and host not in explicit:
            raise CollectorError("Source host requires an explicit allowed_hosts entry")
        if parts.port not in {None, 80, 443}:
            raise CollectorError("Source URL uses a nonstandard port")

    async def _validate_host(self, url: str) -> None:
        self._validate_url_shape(url)
        host = str(urlsplit(url).hostname).lower().rstrip(".")
        # The source is the pre-authorized adapter origin. Custom hosts require
        # public DNS validation; redirects are never followed by this adapter.
        if host in self._validated_hosts or host == "xiaohong66.com" or host.endswith(".xiaohong66.com"):
            return
        if self._mock_transport:
            return
        loop = asyncio.get_running_loop()
        try:
            addresses = await loop.getaddrinfo(host, urlsplit(url).port or 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise CollectorError("Source host DNS lookup failed") from exc
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise CollectorError("Custom source host must resolve only to public addresses")
        self._validated_hosts.add(host)

    def _url(self, path: str) -> str:
        if urlsplit(path).scheme or path.startswith("//"):
            return urljoin(self._request_origin + "/", path)
        prefix = str(self.rule.get("api_prefix", "/api")).strip("/")
        path = path.lstrip("/")
        if prefix and not path.startswith(prefix + "/"):
            path = prefix + "/" + path
        return self._request_origin + "/" + path

    def _retry_delay(self, response: httpx.Response | None, attempt: int) -> float:
        fallback = min(2 ** attempt, 8)
        if response is None:
            return float(fallback)
        raw = response.headers.get("Retry-After", "").strip()
        if raw:
            try:
                delay = float(raw)
                if delay >= 0:
                    return delay
            except ValueError:
                try:
                    date = parsedate_to_datetime(raw)
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    return max(0, (date - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    pass
        return float(fallback)

    async def _request(self, path: str, data: dict[str, Any], method: str = "POST") -> Any:
        method = method.upper()
        if method not in {"GET", "POST"}:
            raise CollectorError("Source requests support only GET or POST")
        if not self._token and not self._session.cookies:
            raise AuthExpired("Source account requires a login session")
        url = self._url(path)
        parts = urlsplit(url)
        if normalize_https_origin(f"{parts.scheme}://{parts.netloc}") != self._request_origin:
            raise CollectorError("Source credentials cannot be forwarded to a different origin")
        await self._validate_host(url)
        attempts = min(max(int(self.rule.get("max_attempts", 3)), 1), 3)
        encoding = self.rule.get("request_encoding", "json")
        headers = {"Accept": "application/json", "User-Agent": str(self.rule.get("user_agent", "Mozilla/5.0")),
                   "Origin": self._request_origin, "Referer": self._request_origin + "/"}
        if self._token:
            headers["Authorization"] = self._token
        if method == "GET":
            request_body = {"params": {str(k): str(v) if v is not None else "" for k, v in data.items()}}
        elif encoding == "json":
            # Authenticated replay accepts standard JSON. The old client
            # advertises a composite form header that produces HTTP 500 here.
            headers["Content-Type"] = str(self.rule.get("request_content_type", "application/json"))
            request_body = {"content": json.dumps(_json_safe(data), ensure_ascii=False, separators=(",", ":")).encode("utf-8")}
        elif encoding == "form":
            request_body = {"data": {str(k): str(v) if v is not None else "" for k, v in data.items()}}
        else:
            raise CollectorError("Unsupported source request_encoding")
        for attempt in range(attempts):
            if self._request_gate is not None:
                result = self._request_gate()
                if inspect.isawaitable(result):
                    await result
            response: httpx.Response | None = None
            try:
                response = await self._client.request(
                    method,
                    url,
                    **request_body,
                    headers=headers,
                    follow_redirects=False,
                    timeout=float(self.rule.get("timeout_seconds", 30)),
                )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
                if attempt + 1 >= attempts:
                    raise CollectorError("Source network request failed after limited retries") from None
                await self._sleep(self._retry_delay(None, attempt))
                continue
            if response.status_code in {401, 403}:
                raise AuthExpired("Source account login has expired or lacks access")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 >= attempts:
                    raise CollectorError(f"Source HTTP {response.status_code} after limited retries")
                await self._sleep(self._retry_delay(response, attempt))
                continue
            if response.is_redirect:
                raise CollectorError("Source redirected; verify its address or reconnect the account")
            if not response.is_success:
                raise CollectorError(f"Source returned HTTP {response.status_code}")
            try:
                payload = json.loads(response.content, parse_float=Decimal, parse_constant=lambda _: None)
            except (ValueError, UnicodeError):
                raise CollectorError("Source returned an invalid JSON response") from None
            if not isinstance(payload, dict):
                raise CollectorError("Source JSON response must be an object")
            code = payload.get("code")
            success_codes = self.rule.get("success_codes", [0, "0"])
            if code not in success_codes:
                auth_codes = self.rule.get("auth_expired_codes", [401, "401", 403, "403", -401, "-401"])
                message = str(payload.get("msg", payload.get("message", ""))).lower()
                if code in auth_codes or any(term in message for term in ("登录", "登陆", "token", "unauthorized", "not login")):
                    raise AuthExpired("Source account login has expired")
                raise CollectorError("Source reported an unsuccessful result")
            return payload
        raise CollectorError("Source request exhausted its retry limit")

    async def _post(self, path: str, data: dict[str, Any]) -> Any:
        return await self._request(path, data, "POST")

    async def check_session(self) -> bool:
        # Category visibility is not sufficient proof of an authenticated
        # account. Profile/Show is the source's own account-profile endpoint.
        self.account_identity = None
        payload = await self._request(str(self.rule.get("session_path", "/customer/Profile/Show")), {},
                                      str(self.rule.get("session_method", "GET")))
        profile = _at(payload, self.rule.get("session_data_path", "data"))
        if not isinstance(profile, dict) or not profile:
            raise AuthExpired("Source did not return an authenticated account profile")
        for key in ("id", "Id", "ID", "uid", "UID", "user_id", "userId", "member_id", "memberId", "account", "username", "user_name"):
            value = profile.get(key)
            if isinstance(value, (str, int, Decimal)) and not isinstance(value, bool) and str(value).strip():
                self.account_identity = str(value).strip()
                break
        return True

    async def list_categories(self) -> list[dict[str, Any]]:
        payload = await self._post(
            str(self.rule.get("category_path", "/customer/Home/GoodsCategoryList")),
            dict(self.rule.get("category_extra", {})),
        )
        categories = _at(payload, self.rule.get("category_data_path", "data"))
        if isinstance(categories, dict):
            categories = categories.get("data", categories.get("list"))
        if not isinstance(categories, list) or any(not isinstance(x, dict) for x in categories):
            raise IncompleteCollection("Source category list is missing or invalid")
        self.categories = [_json_safe(x, self._redaction_secrets()) for x in categories]
        return self.categories

    def _redaction_secrets(self) -> tuple[str, ...]:
        # Include source-rotated cookies received during this batch as well as
        # the original encrypted credentials.
        return tuple(dict.fromkeys((*self._secrets, *(cookie.value for cookie in self._client.cookies.jar if cookie.value))))

    def _product(self, source: dict[str, Any], category: dict[str, Any] | None = None) -> SourceProduct:
        fields = dict(self.rule.get("field_mapping", {}))

        def get(name: str, default: Any = None) -> Any:
            return _at(source, str(fields.get(name, name)), default)

        source_id = get("id")
        if source_id is None or isinstance(source_id, (dict, list, bool)) or str(source_id).strip() == "":
            raise CollectorError("Source product has no stable product identifier")
        source_id = str(source_id)
        price = _decimal(get("price"), "price")
        if price < 0:
            raise CollectorError("Source product has a negative price")
        quantity = _decimal(get("price_show_num", 1), "price_show_num")
        unit = str(get("unit", "") or "")
        quality: list[str] = []
        if quantity < 0 or (quantity == 0 and unit.strip()):
            quality.append("invalid_display_quantity")
        if not unit.strip() or re.match(r"^/?\s*0(?:\D|$)", unit):
            quality.append("unit_requires_review")
        stock_value = get("stock")
        stock = _decimal(stock_value, "stock") if stock_value not in (None, "") else None
        unlimited = stock == Decimal("-1")
        if unlimited:
            stock = None
        elif stock is not None and stock < 0:
            quality.append("invalid_stock")
        close = get("is_close", 0)
        status = "closed" if str(close).lower() in {"1", "true", "closed"} else "active"
        category = category or {}
        category_id = get("category_id", source.get("goods_category_id", category.get("id", "")))
        category_name = get("category_name", source.get("goods_category_name", category.get("name", "")))
        if not category_name and category_id is not None:
            for item in self.categories:
                if str(item.get("id", "")) == str(category_id):
                    category_name = item.get("name", "")
                    break
        return SourceProduct(
            source_id=source_id,
            name=str(get("name", get("goods_name", source_id)) or source_id),
            category_id=str(category_id or ""),
            category_name=str(category_name or ""),
            price=price,
            unit=unit,
            display_quantity=quantity,
            status=status,
            stock=stock,
            stock_unlimited=unlimited,
            raw=_json_safe(source, self._redaction_secrets()),
            quality=quality,
        )

    async def iter_pages(self) -> AsyncIterator[list[SourceProduct]]:
        self.expected_total = None
        self.seen_ids.clear()
        self.errors.clear()
        try:
            if not self.categories:
                await self.list_categories()
            ids = self.rule.get("category_ids")
            scopes: list[dict[str, Any] | None] = [None]
            if ids:
                by_id = {str(x.get("id")): x for x in self.categories}
                if any(str(x) not in by_id for x in ids):
                    raise IncompleteCollection("A configured source category is missing")
                scopes = [by_id[str(x)] for x in ids]
            elif self.rule.get("per_category", False):
                scopes = list(self.categories)
                if not scopes:
                    raise IncompleteCollection("Source returned no categories for a per-category collection")
            page_size = int(self.rule.get("page_size", 50))
            if page_size < 1 or page_size > 1000:
                raise CollectorError("page_size must be between 1 and 1000")
            max_pages = int(self.rule.get("max_pages", 10000))
            total_expected = 0
            for category in scopes:
                page = int(self.rule.get("start_page", 1))
                scope_expected: int | None = None
                scope_ids: set[str] = set()
                for _ in range(max_pages):
                    data = {"goods_name": "", **dict(self.rule.get("list_extra", {}))}
                    data[str(self.rule.get("page_field", "page"))] = page
                    data[str(self.rule.get("size_field", "list_rows"))] = page_size
                    if category is not None:
                        data[str(self.rule.get("category_field", "goods_category_id"))] = category["id"]
                    else:
                        data[str(self.rule.get("category_field", "goods_category_id"))] = self.rule.get("all_category_value", 0)
                    payload = await self._post(str(self.rule.get("list_path", "/customer/Home/GoodsList")), data)
                    rows = _at(payload, self.rule.get("list_data_path", "data.data"))
                    count = _at(payload, self.rule.get("list_total_path", "data.count"))
                    if not isinstance(rows, list) or any(not isinstance(x, dict) for x in rows):
                        raise IncompleteCollection("Source product page is missing or invalid")
                    if isinstance(count, bool) or count is None:
                        raise IncompleteCollection("Source product page has no trustworthy total count")
                    try:
                        total = int(count)
                    except (ValueError, TypeError, OverflowError):
                        raise IncompleteCollection("Source product total count is invalid") from None
                    if total < 0 or str(total) != str(count):
                        raise IncompleteCollection("Source product total count is not a nonnegative integer")
                    if scope_expected is None:
                        scope_expected = total
                        total_expected += total
                        self.expected_total = total_expected
                    elif total != scope_expected:
                        raise IncompleteCollection("Source product count changed during pagination")
                    products = [self._product(row, category) for row in rows]
                    page_ids = [p.source_id for p in products]
                    if len(set(page_ids)) != len(page_ids) or scope_ids.intersection(page_ids):
                        raise IncompleteCollection("Source pagination repeated product identifiers")
                    scope_ids.update(page_ids)
                    if len(scope_ids) > scope_expected:
                        raise IncompleteCollection("Source returned more products than its declared count")
                    new_products = [p for p in products if p.source_id not in self.seen_ids]
                    self.seen_ids.update(page_ids)
                    if new_products:
                        yield new_products
                    if len(scope_ids) == scope_expected:
                        break
                    if not rows:
                        raise IncompleteCollection("Source pagination ended before its declared count")
                    page += 1
                else:
                    raise IncompleteCollection("Source pagination exceeded its configured page limit")
            # Each category's declared count was verified independently above.
            # A product may belong to several categories; the worker receives
            # it once, and the completed batch total is the verified union.
            if len(scopes) > 1:
                self.expected_total = len(self.seen_ids)
        except CollectorError as exc:
            self.errors.append(str(exc))
            raise

    async def get_product(self, id: str) -> SourceProduct:
        if isinstance(id, bool) or not re.fullmatch(r"[0-9]+", str(id)):
            raise CollectorError("Source product detail requires a nonnegative integer identifier")
        data = dict(self.rule.get("detail_extra", {}))
        data[str(self.rule.get("detail_id_field", "id"))] = int(id)
        payload = await self._post(str(self.rule.get("detail_path", "/customer/Home/GetGoods")), data)
        product = _at(payload, self.rule.get("detail_data_path", "data"))
        if not isinstance(product, dict):
            raise CollectorError("Source product detail is missing or invalid")
        return self._product(product)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Xiaohong66Collector:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()
