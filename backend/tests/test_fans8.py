"""Contract fixtures derived from fans8's PC client; no live credentials."""

import json
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException

from app.collectors.base import AuthExpired, CollectorError, IncompleteCollection
from app.collectors.fans8 import FANS8_DEFAULT_RULE, Fans8Collector, validate_fans8_rule
from app.collectors.session import encode_session_envelope


def row(identifier=1, **changes):
    return {"Id": identifier, "GoodsName": "contract fixture", "GoodsPrice": "0.007447",
            "GoodsStock": -1, "IsClose": 0, "CategoryId": 11, "Bs": 1000, **changes}


def source(handler, **kwargs):
    return Fans8Collector(token="fixture-access-token", transport=httpx.MockTransport(handler), **kwargs)


@pytest.mark.asyncio
async def test_observed_get_contract_bearer_decimals_categories_and_site_identity():
    requests = []

    async def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.content == b""
        assert request.headers["Authorization"] == "Bearer fixture-access-token"
        assert request.headers["Origin"] == "https://fans8.top"
        if request.url.path == "/api/userInfo":
            assert dict(request.url.params) == {"Switch": "4"}
            return httpx.Response(200, json={"Id": 88, "UserName": "fixture"})
        if request.url.path == "/api/goodsClassList":
            assert dict(request.url.params) == {"IsAll": "1"}
            return httpx.Response(200, json=[{"Id": 10, "Name": "parent", "Childern": [{"Id": 11, "Name": "leaf"}]}])
        if request.url.path == "/api/getSys":
            return httpx.Response(200, json={"Id": 1})
        if request.url.path == "/api/goodsList":
            assert dict(request.url.params) == {"Page": "1", "PageCount": "48", "Sort": "1", "AdminId": "1"}
            return httpx.Response(200, content=b'{"error":0,"code":100,"info":[{"Id":1,"GoodsName":"fixture","GoodsPrice":0.007447,"GoodsStock":-1,"IsClose":1,"CategoryId":10,"CategoryId2":11,"Bs":1000}],"count":1}')
        assert request.url.path == "/api/goodsDetail"
        assert dict(request.url.params) == {"Id": "1"}
        return httpx.Response(200, json=row(GoodsType=2, KmStock=12))

    async with source(handler) as collector:
        assert await collector.check_session()
        assert collector.account_identity == "88"
        pages = [page async for page in collector.iter_pages()]
        product = pages[0][0]
        assert product.price == Decimal("0.007447")
        assert product.raw["GoodsPrice"] == "0.007447"
        assert product.display_quantity == Decimal(1)
        assert product.unit == ""
        assert product.quality == ["unit_requires_review"]
        assert product.status == "closed"
        assert product.category_id == "11"
        assert product.category_name == "leaf"
        assert product.stock_unlimited and product.stock is None
        assert collector.expected_total == 1
        assert collector.seen_ids == {"1"}
        assert collector.categories[1]["parent_id"] == "10"
        assert collector.categories[1]["category_field"] == "CategoryId2"
        detail = await collector.get_product("1")
        assert detail.stock == Decimal(12)
        assert not detail.stock_unlimited
    assert len(requests) == 5


@pytest.mark.asyncio
async def test_paginated_complete_catalogue_checks_every_declared_count():
    pages = []

    async def handler(request):
        if request.url.path.endswith("goodsClassList"):
            return httpx.Response(200, json=[])
        page = int(request.url.params["Page"])
        pages.append(page)
        return httpx.Response(200, json={"error": 0, "info": [row(page)], "count": 2})

    async with source(handler, rule={"site_admin_id": 1, "page_size": 1}) as collector:
        actual = [product.source_id async for page in collector.iter_pages() for product in page]
        assert actual == ["1", "2"]
        assert collector.expected_total == 2
        assert not collector.errors
    assert pages == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["early_empty", "duplicate", "changed_count", "missing_count", "more_than_count"])
async def test_pagination_failure_is_never_a_complete_batch(failure):
    async def handler(request):
        if request.url.path.endswith("goodsClassList"):
            return httpx.Response(200, json=[])
        page = int(request.url.params["Page"])
        payload = {"error": 0, "info": [row(page)], "count": 2}
        if failure == "missing_count":
            payload.pop("count")
        elif failure == "more_than_count":
            payload["count"] = 0
        elif page > 1:
            if failure == "early_empty":
                payload["info"] = []
            elif failure == "duplicate":
                payload["info"] = [row(1)]
            elif failure == "changed_count":
                payload["count"] = 3
        return httpx.Response(200, json=payload)

    async with source(handler, rule={"site_admin_id": 1, "page_size": 1}) as collector:
        with pytest.raises(IncompleteCollection):
            _ = [page async for page in collector.iter_pages()]
        assert collector.errors


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,error_class", [
    ({"error": 3, "info": "登录状态失效,重新登录!!"}, AuthExpired),
    ({"error": 1, "info": "游客不支持当前操作"}, AuthExpired),
    ({"error": 1, "info": "商品不存在"}, CollectorError),
    ({"error": 4, "info": "网站已关闭"}, CollectorError),
])
async def test_source_error_contract(payload, error_class):
    async def handler(request):
        return httpx.Response(200, json=payload)

    async with source(handler) as collector:
        with pytest.raises(error_class) as raised:
            await collector.check_session()
        if error_class is CollectorError:
            assert not isinstance(raised.value, AuthExpired)


@pytest.mark.asyncio
async def test_bound_cookie_envelope_and_redaction_with_rotated_cookie():
    credential = encode_session_envelope("https://fans8.top", token="fixture-access-token", cookies=[{
        "name": "fixture-session", "value": "fixture-cookie", "domain": "fans8.top", "path": "/", "secure": True,
    }])

    async def handler(request):
        assert request.headers["Cookie"] == "fixture-session=fixture-cookie"
        return httpx.Response(200, json=row(description="fixture-access-token fixture-cookie fixture-rotated"),
                              headers={"Set-Cookie": "fixture-session=fixture-rotated; Path=/; Secure"})

    async with Fans8Collector(token=credential, transport=httpx.MockTransport(handler)) as collector:
        product = await collector.get_product("1")
        assert product.raw["description"] == "[redacted] [redacted] [redacted]"


@pytest.mark.asyncio
async def test_redirect_does_not_forward_session():
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://other.example/api/userInfo"})

    async with source(handler) as collector:
        with pytest.raises(CollectorError, match="redirected"):
            await collector.check_session()
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_rate_gate_applies_to_retry_and_read_only_replay():
    attempts = 0
    gates = 0
    pauses = []

    async def handler(request):
        nonlocal attempts
        attempts += 1
        assert request.method == "GET"
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=row())

    async def gate():
        nonlocal gates
        gates += 1

    async def pause(seconds):
        pauses.append(seconds)

    async with source(handler, request_gate=gate, sleep=pause) as collector:
        assert (await collector.get_product("1")).source_id == "1"
    assert attempts == gates == 2
    assert pauses == [0.0]


@pytest.mark.asyncio
async def test_explicit_category_scope_uses_its_observed_tree_level():
    async def handler(request):
        if request.url.path.endswith("goodsClassList"):
            return httpx.Response(200, json=[{"Id": 10, "Name": "root", "Childern": [{"Id": 11, "Name": "child"}]}])
        assert request.url.params["CategoryId2"] == "11"
        assert "CategoryId" not in request.url.params
        return httpx.Response(200, json={"error": 0, "info": [row()], "count": 1})

    async with source(handler, rule={"site_admin_id": 1, "category_ids": [11]}) as collector:
        assert len([page async for page in collector.iter_pages()]) == 1


@pytest.mark.asyncio
async def test_root_scope_preserves_the_products_leaf_category_name():
    async def handler(request):
        if request.url.path.endswith("goodsClassList"):
            return httpx.Response(200, json=[{"Id": 10, "Name": "root", "Childern": [{"Id": 11, "Name": "leaf"}]}])
        assert request.url.params["CategoryId"] == "10"
        return httpx.Response(200, json={"error": 0, "info": [row(CategoryId=10, CategoryId2=11)], "count": 1})

    async with source(handler, rule={"site_admin_id": 1, "per_category": True}) as collector:
        pages = [page async for page in collector.iter_pages()]
        assert pages[0][0].category_id == "11"
        assert pages[0][0].category_name == "leaf"


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", [True, None, "-1", "1.0", " 1", "١", "abc"])
async def test_invalid_product_ids_never_reach_http(identifier):
    async def handler(request):
        pytest.fail("invalid product identifiers must be rejected before HTTP")

    async with source(handler) as collector:
        with pytest.raises(CollectorError, match="nonnegative integer"):
            await collector.get_product(identifier)


@pytest.mark.parametrize("rule", [
    {"session_extra": {"Switch": 2}}, {"list_path": "/api/createOrder"}, {"session_path": "https://other.example"},
    {"list_extra": {"Sort": 1, "token": "fixture-should-not-echo"}}, {"allowed_hosts": ["127.0.0.1"]},
    {"page_size": True}, {"field_mapping": {"price": ""}}, {"unknown": "fixture-should-not-echo"},
])
def test_rule_validation_locks_read_paths_and_omits_secret_values_from_errors(rule):
    with pytest.raises(HTTPException) as raised:
        validate_fans8_rule(rule)
    assert raised.value.status_code == 422
    assert "fixture-should-not-echo" not in json.dumps(raised.value.detail)


def test_defaults_and_rule_export_are_consistent():
    assert validate_fans8_rule({}) == FANS8_DEFAULT_RULE
