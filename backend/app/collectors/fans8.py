"""Read-only catalogue adapter for the observed fans8.top storefront.

The operator identifies this source as the Yike series. Its public PC client
uses a distinct contract from the Yile customer API; the class retains its
source-specific name because no vendor or version marker appears in the bundle.
"""

from __future__ import annotations

import inspect
import ipaddress
import json
import re
from decimal import Decimal
from typing import Any, AsyncIterator, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError, field_validator, model_validator

from .base import AuthExpired, CollectorError, IncompleteCollection, SourceProduct
from .session import normalize_https_origin
from .xiaohong66 import Xiaohong66Collector, _at, _decimal, _json_safe


FANS8_DEFAULT_RULE: dict[str, Any] = {
    "api_prefix": "",
    "category_path": "/api/goodsClassList",
    "category_extra": {"IsAll": 1},
    "category_data_path": "",
    "list_path": "/api/goodsList",
    "list_data_path": "info",
    "list_total_path": "count",
    "list_extra": {"Sort": 1},
    "detail_path": "/api/goodsDetail",
    "detail_data_path": "",
    "detail_id_field": "Id",
    "session_path": "/api/userInfo",
    "session_extra": {"Switch": 4},
    "session_data_path": "",
    "system_path": "/api/getSys",
    "system_data_path": "",
    "page_field": "Page",
    "size_field": "PageCount",
    "page_size": 5000,
    "max_pages": 10000,
    "category_ids": [],
    "per_category": False,
    "timeout_seconds": 60,
    "max_attempts": 3,
    "field_mapping": {},
    "user_agent": "Mozilla/5.0",
    "allowed_hosts": [],
    "site_admin_id": None,
}


class Fans8Rule(BaseModel):
    """Declarative options for the five observed read-only source endpoints."""

    model_config = ConfigDict(extra="forbid", strict=True)
    api_prefix: Literal[""] = ""
    category_path: Literal["/api/goodsClassList"] = "/api/goodsClassList"
    category_extra: dict[str, Any] = Field(default_factory=lambda: {"IsAll": 1})
    category_data_path: Literal[""] = ""
    list_path: Literal["/api/goodsList"] = "/api/goodsList"
    list_data_path: Literal["info"] = "info"
    list_total_path: Literal["count"] = "count"
    list_extra: dict[str, Any] = Field(default_factory=lambda: {"Sort": 1})
    detail_path: Literal["/api/goodsDetail"] = "/api/goodsDetail"
    detail_data_path: Literal[""] = ""
    detail_id_field: Literal["Id"] = "Id"
    session_path: Literal["/api/userInfo"] = "/api/userInfo"
    session_extra: dict[str, Any] = Field(default_factory=lambda: {"Switch": 4})
    session_data_path: Literal[""] = ""
    system_path: Literal["/api/getSys"] = "/api/getSys"
    system_data_path: Literal[""] = ""
    page_field: Literal["Page"] = "Page"
    size_field: Literal["PageCount"] = "PageCount"
    # The live endpoint returned all 4,881 products with PageCount=5000.
    # Reading one response avoids shifting pages while the catalogue changes.
    page_size: StrictInt = Field(default=5000, ge=1, le=5000)
    max_pages: StrictInt = Field(default=10000, ge=1, le=10000)
    category_ids: list[StrictInt | StrictStr] = Field(default_factory=list, max_length=1000)
    per_category: bool = False
    timeout_seconds: float = Field(default=60, ge=1, le=60, allow_inf_nan=False)
    max_attempts: StrictInt = Field(default=3, ge=1, le=3)
    field_mapping: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    user_agent: StrictStr = Field(default="Mozilla/5.0", min_length=1, max_length=512)
    allowed_hosts: list[StrictStr] = Field(default_factory=list, max_length=32)
    site_admin_id: StrictInt | None = Field(default=None, ge=1)

    @model_validator(mode="before")
    @classmethod
    def json_budget(cls, value: Any) -> Any:
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            raise ValueError("规则须为有效JSON且数值必须有限") from None
        if len(encoded.encode("utf-8")) > 32000:
            raise ValueError("规则不能超过32000字节")
        return value

    @field_validator("category_extra", "list_extra", "session_extra")
    @classmethod
    def read_only_query(cls, value: dict[str, Any], info: Any) -> dict[str, Any]:
        expected = {"category_extra": {"IsAll": 1}, "list_extra": {"Sort": 1}, "session_extra": {"Switch": 4}}
        if value != expected[info.field_name] or any(isinstance(item, bool) for item in value.values()):
            raise ValueError("接口参数须保持已验证的只读查询契约")
        return value

    @field_validator("category_ids")
    @classmethod
    def category_identifiers(cls, value: list[int | str]) -> list[int | str]:
        if any(not re.fullmatch(r"[0-9]+", str(item)) for item in value):
            raise ValueError("分类编号须为非负整数")
        return value

    @field_validator("field_mapping")
    @classmethod
    def source_fields(cls, value: dict[str, str]) -> dict[str, str]:
        allowed = {"id", "name", "category_id", "category_name", "price", "display_quantity", "price_show_num", "unit", "stock", "is_close"}
        if set(value) - allowed:
            raise ValueError("字段映射包含适配器不支持的逻辑字段")
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", path) for path in value.values()):
            raise ValueError("字段路径须为非空点分对象路径")
        return value

    @field_validator("user_agent")
    @classmethod
    def printable_agent(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) >= 127 for char in value):
            raise ValueError("User-Agent须为无控制字符的ASCII文本")
        return value

    @field_validator("allowed_hosts")
    @classmethod
    def public_hostnames(cls, value: list[str]) -> list[str]:
        for hostname in value:
            host = hostname.lower().rstrip(".")
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                raise ValueError("额外采集域名不能使用IP地址")
            labels = host.split(".")
            if (len(host) > 253 or len(labels) < 2 or labels[-1].isdigit()
                    or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
                    or labels[-1] in {"localhost", "local", "internal", "lan", "test", "invalid", "example"}):
                raise ValueError("额外采集域名须为有效公共域名，不包含协议、端口或路径")
            if host == "api.zxcbug.com" or host.endswith(".api.zxcbug.com"):
                raise ValueError("此域名不能配置为采集目标")
        return value


def validate_fans8_rule(value: Any) -> dict[str, Any]:
    """Return full validated defaults, reporting API errors without input secrets."""
    try:
        return Fans8Rule.model_validate(value).model_dump()
    except ValidationError as exc:
        details = exc.errors(include_input=False, include_context=False, include_url=False)
        for detail in details:
            detail["loc"] = ["body", "config", *detail["loc"]]
        raise HTTPException(status_code=422, detail=details) from None


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        raise CollectorError(f"Source {field} must be a nonnegative integer")
    return int(value)


class Fans8Collector(Xiaohong66Collector):
    """Collect the observed bearer-authenticated catalogue with bound sessions.

    HTTP client lifetime, source-bound cookies, rate limiting, retry delays and
    evidence redaction reuse the existing collector infrastructure. All source
    operations implemented here are GET requests from the public PC client.
    """

    def __init__(self, base_url: str = "https://fans8.top", token: str = "", rule: dict[str, Any] | None = None,
                 request_gate: Any = None, **kwargs: Any) -> None:
        config = validate_fans8_rule(dict(rule or {}))
        super().__init__(base_url, token, config, request_gate, **kwargs)
        self._admin_id: int | None = config["site_admin_id"]
        self._system_loaded = self._admin_id is not None

    def _validate_url_shape(self, url: str) -> None:
        try:
            parts = urlsplit(url)
            host = str(parts.hostname or "").lower().rstrip(".")
            if (parts.scheme != "https" or not host or parts.username or parts.password
                    or parts.fragment or parts.port not in {None, 443}):
                raise ValueError
        except ValueError:
            raise CollectorError("Source URL must use HTTPS without credentials, fragments or nonstandard ports") from None
        explicit = {str(item).lower().rstrip(".") for item in self.rule.get("allowed_hosts", [])}
        if host != "fans8.top" and host not in explicit:
            raise CollectorError("Source host requires an explicit allowed_hosts entry")

    async def _get(self, path: str, data: dict[str, Any]) -> Any:
        if not self._token and not self._session.cookies:
            raise AuthExpired("Source account requires a login session")
        url = self._url(path)
        parts = urlsplit(url)
        if normalize_https_origin(f"{parts.scheme}://{parts.netloc}") != self._request_origin:
            raise CollectorError("Source credentials cannot be forwarded to a different origin")
        await self._validate_host(url)
        headers = {
            "Accept": "application/json", "User-Agent": str(self.rule.get("user_agent", "Mozilla/5.0")),
            "Origin": self._request_origin, "Referer": self._request_origin + "/indexPc.html",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        }
        if self._token:
            headers["Authorization"] = self._token if self._token.startswith("Bearer ") else "Bearer " + self._token
        attempts = min(max(int(self.rule.get("max_attempts", 3)), 1), 3)
        for attempt in range(attempts):
            if self._request_gate is not None:
                pending = self._request_gate()
                if inspect.isawaitable(pending):
                    await pending
            try:
                response = await self._client.get(url, params=data, headers=headers, follow_redirects=False,
                                                  timeout=float(self.rule.get("timeout_seconds", 30)))
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
            if not isinstance(payload, (dict, list)):
                raise CollectorError("Source JSON response must be an object or array")
            if isinstance(payload, dict) and "error" in payload:
                error = payload["error"]
                message = payload.get("info")
                if error in (3, "3") or (error in (1, "1") and message == "游客不支持当前操作"):
                    raise AuthExpired("Source account login has expired")
                if isinstance(error, bool) or error not in (0, "0"):
                    raise CollectorError("Source reported an unsuccessful result")
            return payload
        raise CollectorError("Source request exhausted its retry limit")

    async def check_session(self) -> bool:
        self.account_identity = None
        payload = await self._get(str(self.rule["session_path"]), dict(self.rule["session_extra"]))
        profile = _at(payload, self.rule["session_data_path"])
        if not isinstance(profile, dict) or "Id" not in profile:
            raise AuthExpired("Source did not return an authenticated account profile")
        identifier = profile["Id"]
        if isinstance(identifier, bool) or not re.fullmatch(r"[0-9]+", str(identifier)):
            raise AuthExpired("Source account profile has no stable identifier")
        self.account_identity = str(identifier)
        return True

    async def _load_system(self) -> None:
        if self._system_loaded:
            return
        payload = await self._get(str(self.rule["system_path"]), {})
        system = _at(payload, self.rule["system_data_path"])
        if not isinstance(system, dict) or "Id" not in system:
            raise IncompleteCollection("Source site identity is missing or invalid")
        identifier = _integer(system["Id"], "site identifier")
        self._admin_id = identifier if identifier else None
        self._system_loaded = True

    async def list_categories(self) -> list[dict[str, Any]]:
        payload = await self._get(str(self.rule["category_path"]), dict(self.rule["category_extra"]))
        tree = _at(payload, self.rule["category_data_path"])
        if not isinstance(tree, list):
            raise IncompleteCollection("Source category tree is missing or invalid")
        categories: list[dict[str, Any]] = []
        seen: set[str] = set()

        def visit(nodes: list[Any], parent: dict[str, Any] | None = None, depth: int = 1) -> None:
            if depth > 3:
                raise IncompleteCollection("Source category tree exceeds the observed three levels")
            for node in nodes:
                if not isinstance(node, dict) or "Id" not in node or not isinstance(node.get("Name"), str):
                    raise IncompleteCollection("Source category is missing its identifier or name")
                identifier = str(_integer(node["Id"], "category identifier"))
                if identifier in seen:
                    raise IncompleteCollection("Source category tree repeated an identifier")
                seen.add(identifier)
                category = {"id": identifier, "name": node["Name"], "parent_id": parent["id"] if parent else "",
                            "category_field": "CategoryId" + (str(depth) if depth > 1 else ""),
                            "source": _json_safe(node, self._redaction_secrets())}
                categories.append(category)
                children = node.get("Childern")
                if children:
                    if not isinstance(children, list):
                        raise IncompleteCollection("Source category children are invalid")
                    visit(children, category, depth + 1)

        visit(tree)
        self.categories = categories
        return categories

    def _product(self, source: dict[str, Any], category: dict[str, Any] | None = None) -> SourceProduct:
        mapping = dict(self.rule.get("field_mapping", {}))

        def value(name: str, default_field: str, default: Any = None) -> Any:
            return _at(source, str(mapping.get(name, default_field)), default)

        identifier = str(_integer(value("id", "Id"), "product identifier"))
        price = _decimal(value("price", "GoodsPrice"), "GoodsPrice")
        if price < 0:
            raise CollectorError("Source product has a negative price")
        name = value("name", "GoodsName", identifier)
        if not isinstance(name, str) or not name.strip():
            raise CollectorError("Source product has no valid name")
        # Bs is the minimum order step. The PC client does not present it as a
        # price multiplier, and it must not be used for price normalization.
        unit = str(_at(source, str(mapping["unit"]), "") or "") if "unit" in mapping else ""
        quantity_path = mapping.get("display_quantity", mapping.get("price_show_num"))
        quantity = _decimal(_at(source, str(quantity_path), 1), "display_quantity") if quantity_path else Decimal(1)
        quality = [] if unit.strip() else ["unit_requires_review"]
        if quantity < 0 or (quantity == 0 and unit.strip()):
            quality.append("invalid_display_quantity")
        if "stock" in mapping:
            stock_value = _at(source, str(mapping["stock"]))
        else:
            stock_value = source.get("KmStock") if str(source.get("GoodsType")) == "2" else source.get("GoodsStock")
        stock = _decimal(stock_value, "stock") if stock_value not in (None, "") else None
        unlimited = stock == Decimal(-1)
        if unlimited:
            stock = None
        elif stock is not None and stock < 0:
            quality.append("invalid_stock")
        category_id = ""
        for field in ("CategoryId3", "CategoryId2", "CategoryId"):
            if source.get(field) not in (None, "", 0, "0"):
                category_id = str(_integer(source[field], "product category identifier"))
                break
        category_id = str(value("category_id", "CategoryId", category_id)) if "category_id" in mapping else category_id
        category = next((item for item in self.categories if str(item["id"]) == category_id), category or {})
        category_name = str(_at(source, str(mapping["category_name"]), "")) if "category_name" in mapping else str(category.get("name", ""))
        return SourceProduct(
            source_id=identifier, name=name, category_id=category_id or str(category.get("id", "")),
            category_name=category_name, price=price, unit=unit, display_quantity=quantity,
            status="closed" if str(value("is_close", "IsClose", 0)) == "1" else "active",
            stock=stock, stock_unlimited=unlimited, raw=_json_safe(source, self._redaction_secrets()), quality=quality,
        )

    async def iter_pages(self) -> AsyncIterator[list[SourceProduct]]:
        self.expected_total = None
        self.seen_ids.clear()
        self.errors.clear()
        try:
            if not self.categories:
                await self.list_categories()
            await self._load_system()
            by_id = {str(item["id"]): item for item in self.categories}
            configured_ids = self.rule.get("category_ids")
            if configured_ids:
                if any(str(identifier) not in by_id for identifier in configured_ids):
                    raise IncompleteCollection("A configured source category is missing")
                scopes: list[dict[str, Any] | None] = [by_id[str(identifier)] for identifier in configured_ids]
            elif self.rule.get("per_category"):
                # Root scopes already include their descendants. Querying all
                # levels would count the same products several times.
                scopes = [item for item in self.categories if not item["parent_id"]]
                if not scopes:
                    raise IncompleteCollection("Source returned no categories for a per-category collection")
            else:
                scopes = [None]
            page_size = _integer(self.rule["page_size"], "page size")
            if not 1 <= page_size <= 5000:
                raise CollectorError("page_size must be between 1 and 5000")
            max_pages = _integer(self.rule.get("max_pages", 10000), "page limit")
            if not max_pages:
                raise CollectorError("max_pages must be positive")
            expected_union = 0
            for category in scopes:
                scope_total: int | None = None
                scope_seen: set[str] = set()
                for page in range(1, max_pages + 1):
                    data = {**dict(self.rule["list_extra"]), str(self.rule["page_field"]): page,
                            str(self.rule["size_field"]): page_size}
                    if self._admin_id is not None:
                        data["AdminId"] = self._admin_id
                    if category is not None:
                        data[category["category_field"]] = int(category["id"])
                    payload = await self._get(str(self.rule["list_path"]), data)
                    rows = _at(payload, self.rule["list_data_path"])
                    count = _at(payload, self.rule["list_total_path"])
                    if not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows):
                        raise IncompleteCollection("Source product page is missing or invalid")
                    try:
                        total = _integer(count, "product total count")
                    except CollectorError as exc:
                        raise IncompleteCollection("Source product page has no trustworthy total count") from exc
                    if scope_total is None:
                        scope_total = total
                        expected_union += total
                        self.expected_total = expected_union
                    elif total != scope_total:
                        raise IncompleteCollection("Source product count changed during pagination")
                    products = [self._product(item, category) for item in rows]
                    page_ids = [item.source_id for item in products]
                    if len(set(page_ids)) != len(page_ids) or scope_seen.intersection(page_ids):
                        raise IncompleteCollection("Source pagination repeated product identifiers")
                    scope_seen.update(page_ids)
                    if len(scope_seen) > total:
                        raise IncompleteCollection("Source returned more products than its declared count")
                    fresh = [item for item in products if item.source_id not in self.seen_ids]
                    self.seen_ids.update(page_ids)
                    if fresh:
                        yield fresh
                    if len(scope_seen) == total:
                        break
                    if not rows:
                        raise IncompleteCollection("Source pagination ended before its declared count")
                else:
                    raise IncompleteCollection("Source pagination exceeded its configured page limit")
            if len(scopes) > 1:
                self.expected_total = len(self.seen_ids)
        except CollectorError as exc:
            self.errors.append(str(exc))
            raise

    async def get_product(self, id: str) -> SourceProduct:
        identifier = _integer(id, "product detail identifier")
        payload = await self._get(str(self.rule["detail_path"]), {str(self.rule["detail_id_field"]): identifier})
        product = _at(payload, self.rule["detail_data_path"])
        if not isinstance(product, dict):
            raise CollectorError("Source product detail is missing or invalid")
        result = self._product(product)
        if result.source_id != str(identifier):
            raise CollectorError("Source product detail returned a different identifier")
        return result
