"""Typed configuration accepted by the catalogue adapter's public API.

Only declarative collector options belong here. Credentials are stored through
the encrypted session API and never become readable rule configuration.
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Annotated, Any, Literal

from fastapi import HTTPException
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)


_SECRET_KEYS = {
    'token', 'authorization', 'cookie', 'cookies', 'setcookie', 'password',
    'passwd', 'accesstoken', 'refreshtoken', 'secret', 'apikey', 'clientsecret',
    'sessiontoken', 'sessioncookie',
}
_MAPPING_FIELDS = {
    'id', 'name', 'goods_name', 'category_id', 'category_name', 'price',
    'price_show_num', 'unit', 'stock', 'is_close',
}
_DOTTED_PATH = re.compile(r'^[^\s.\\/?#\x00-\x1f\x7f]+(?:\.[^\s.\\/?#\x00-\x1f\x7f]+)*$')
_DOMAIN_LABEL = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$')

ParameterName = Annotated[StrictStr, StringConstraints(min_length=1, max_length=80, pattern=r'^[A-Za-z_][A-Za-z0-9_]*$')]
DataPath = Annotated[StrictStr, StringConstraints(min_length=1, max_length=256)]
SourceIdentifier = StrictInt | Annotated[StrictStr, StringConstraints(min_length=1, max_length=100)]
ResultCode = StrictInt | Annotated[StrictStr, StringConstraints(min_length=1, max_length=64)]


def _normalized_key(value: str) -> str:
    return re.sub(r'[^a-z0-9]', '', value.lower())


def _validate_extra(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    def visit(item: Any, depth: int = 0) -> None:
        if depth > 12:
            raise ValueError('请求参数嵌套层级过深')
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or not key or len(key) > 120:
                    raise ValueError('请求参数键名须为非空字符串且不超过120字符')
                if _normalized_key(key) in _SECRET_KEYS:
                    raise ValueError('认证字段不能保存在规则中，请使用加密会话配置')
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)

    visit(value)
    return value


class RuleConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    api_prefix: Literal['/api'] = '/api'
    category_path: Literal['/customer/Home/GoodsCategoryList'] = '/customer/Home/GoodsCategoryList'
    list_path: Literal['/customer/Home/GoodsList'] = '/customer/Home/GoodsList'
    detail_path: Literal['/customer/Home/GetGoods'] = '/customer/Home/GetGoods'
    session_path: Literal['/customer/Profile/Show'] = '/customer/Profile/Show'
    session_method: Literal['GET', 'POST'] = 'GET'
    session_data_path: DataPath = 'data'
    category_data_path: DataPath = 'data'
    list_data_path: DataPath = 'data.data'
    list_total_path: DataPath = 'data.count'
    detail_data_path: DataPath = 'data'

    page_field: ParameterName = 'page'
    size_field: ParameterName = 'list_rows'
    category_field: ParameterName = 'goods_category_id'
    detail_id_field: ParameterName = 'id'
    page_size: Annotated[StrictInt, Field(ge=1, le=200)] = 50
    start_page: Annotated[StrictInt, Field(ge=1)] = 1
    max_pages: Annotated[StrictInt, Field(ge=1, le=10000)] = 10000
    timeout_seconds: Annotated[float, Field(ge=1, le=60, allow_inf_nan=False)] = 30
    max_attempts: Annotated[StrictInt, Field(ge=1, le=3)] = 3

    all_category_value: SourceIdentifier = 0
    category_ids: Annotated[list[SourceIdentifier], Field(max_length=1000)] = Field(default_factory=list)
    per_category: StrictBool = False
    category_extra: dict[str, JsonValue] = Field(default_factory=dict)
    list_extra: dict[str, JsonValue] = Field(default_factory=lambda: {'goods_name': ''})
    detail_extra: dict[str, JsonValue] = Field(default_factory=dict)
    field_mapping: dict[StrictStr, DataPath] = Field(default_factory=dict)
    success_codes: Annotated[list[ResultCode], Field(min_length=1, max_length=64)] = Field(default_factory=lambda: [0, '0'])
    auth_expired_codes: Annotated[list[ResultCode], Field(max_length=64)] = Field(default_factory=lambda: [100, '100', 110, '110', 401, '401', 403, '403', -401, '-401'])
    request_encoding: Literal['json', 'form'] = 'json'
    request_content_type: Literal['application/json', 'application/x-www-form-urlencoded;charset=UTF-8;multipart/form-data'] = 'application/json'
    user_agent: Annotated[StrictStr, StringConstraints(min_length=1, max_length=512)] = 'Mozilla/5.0'
    allowed_hosts: Annotated[list[StrictStr], Field(max_length=32)] = Field(default_factory=list)

    @model_validator(mode='before')
    @classmethod
    def enforce_json_budget(cls, value: Any) -> Any:
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            raise ValueError('规则须为有效JSON且数值必须有限') from None
        if len(encoded.encode('utf-8')) > 32000:
            raise ValueError('规则不能超过32000字节')
        return value

    @field_validator('category_extra', 'list_extra', 'detail_extra')
    @classmethod
    def reject_nested_credentials(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _validate_extra(value)

    @field_validator('page_field', 'size_field', 'category_field', 'detail_id_field')
    @classmethod
    def reject_credential_parameter_names(cls, value: str) -> str:
        if _normalized_key(value) in _SECRET_KEYS:
            raise ValueError('分页和商品参数不能使用认证字段名称')
        return value

    @field_validator('session_data_path', 'category_data_path', 'list_data_path', 'list_total_path', 'detail_data_path')
    @classmethod
    def validate_data_path(cls, value: str) -> str:
        if not _DOTTED_PATH.fullmatch(value):
            raise ValueError('字段路径须为非空点分对象路径')
        return value

    @field_validator('field_mapping')
    @classmethod
    def validate_field_mapping(cls, value: dict[str, str]) -> dict[str, str]:
        if set(value) - _MAPPING_FIELDS:
            raise ValueError('字段映射包含适配器不支持的逻辑字段')
        for path in value.values():
            cls.validate_data_path(path)
        return value

    @field_validator('user_agent')
    @classmethod
    def validate_user_agent(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) >= 127 for char in value):
            raise ValueError('User-Agent须为无控制字符的ASCII文本')
        return value

    @field_validator('allowed_hosts')
    @classmethod
    def validate_allowed_hosts(cls, value: list[str]) -> list[str]:
        for hostname in value:
            host = hostname.lower().rstrip('.')
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                raise ValueError('额外采集域名不能使用IP地址')
            labels = host.split('.')
            if (not hostname or len(host) > 253 or len(labels) < 2
                    or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels)
                    or labels[-1].isdigit()
                    or labels[-1] in {'localhost', 'local', 'internal', 'lan', 'test', 'invalid', 'example'}):
                raise ValueError('额外采集域名须为有效公共域名，不包含协议、端口或路径')
            if host == 'api.zxcbug.com' or host.endswith('.api.zxcbug.com'):
                raise ValueError('此域名不能配置为采集目标')
        return value


def validate_rule_config(value: Any) -> RuleConfig:
    """Return an API-safe 422 without echoing submitted credential values."""
    try:
        return RuleConfig.model_validate(value)
    except ValidationError as exc:
        details = exc.errors(include_input=False, include_context=False, include_url=False)
        for detail in details:
            detail['loc'] = ['body', 'config', *detail['loc']]
        raise HTTPException(status_code=422, detail=details) from None
