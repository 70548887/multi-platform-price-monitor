"""Contract tests for source reads; all HTTP uses a local MockTransport."""

import json
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest

from app.collectors import DEFAULT_RULE, AuthExpired, CollectorError, IncompleteCollection, Xiaohong66Collector
from app.collectors.session import SESSION_PREFIX, decode_session_envelope, encode_session_envelope


def product(id=9585, **changes):
    return {
        "id": id,
        "name": "精度样本",
        "category_id": 12,
        "price": "0.007486",
        "price_show_num": "1000",
        "unit": "人",
        "is_close": 0,
        "stock": -1,
        **changes,
    }


def ok(data):
    return httpx.Response(200, json={"code": 0, "data": data})


def request_fields(request, *, keep_blank_values=False):
    if request.content.lstrip().startswith(b"{"):
        return {k: [str(v)] for k, v in json.loads(request.content).items() if keep_blank_values or v != ""}
    return parse_qs(request.content.decode(), keep_blank_values=keep_blank_values)


def collector(handler, **kwargs):
    return Xiaohong66Collector(
        "https://www.xiaohong66.com",
        "test-account-token",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_precise_numeric_json_price_and_display_multiplier_are_preserved():
    requests = []

    async def handler(request):
        requests.append(request)
        if request.url.path.endswith("GoodsCategoryList"):
            return ok([{"id": 12, "name": "分类"}])
        # A JSON number, deliberately not a quoted string, verifies parse_float.
        return httpx.Response(
            200,
            content=b'{"code":0,"data":{"data":[{"id":9585,"name":"sample","category_id":12,"price":0.007486,"price_show_num":1000,"unit":"person","stock":-1,"is_close":0}],"count":1}}',
            headers={"Content-Type": "application/json"},
        )

    async with collector(handler) as source:
        pages = [page async for page in source.iter_pages()]
        result = pages[0][0]
        assert result.price == Decimal("0.007486")
        assert result.display_quantity == Decimal("1000")
        assert result.price * result.display_quantity == Decimal("7.486000")
        assert result.stock_unlimited is True
        assert result.stock is None
        assert result.category_name == "分类"
        assert result.raw["price"] == "0.007486"
        json.dumps(result.raw)
        assert source.expected_total == 1
        assert source.seen_ids == {"9585"}
        assert not source.errors
    assert all(r.headers["Authorization"] == "test-account-token" for r in requests)


@pytest.mark.asyncio
async def test_zero_price_closed_item_and_invalid_unit_remain_observations():
    async def handler(request):
        return ok(product(price="0", price_show_num="0", unit="0人", is_close=1))

    async with collector(handler) as source:
        result = await source.get_product("9585")
    assert result.status == "closed"
    assert result.price == Decimal("0")
    assert result.display_quantity == Decimal("0")
    assert set(result.quality) == {"invalid_display_quantity", "unit_requires_review"}


@pytest.mark.asyncio
async def test_readonly_form_contract_and_configurable_field_mapping():
    seen = []

    async def handler(request):
        seen.append((request.url.path, request_fields(request)))
        if request.url.path.endswith("GoodsCategoryList"):
            return ok([{"id": "12", "name": "分类"}])
        if request.url.path.endswith("GetGoods"):
            return ok(product(id="42", goods_name="名称", name=None))
        return ok({"data": [product(id="42", name="修改后名称")], "count": "1"})

    rule = {"page_field": "current", "size_field": "size", "page_size": 50,
            "list_extra": {"sort": "id"}, "category_ids": ["12"], "detail_id_field": "goods_id"}
    async with collector(handler, rule=rule) as source:
        pages = [page async for page in source.iter_pages()]
        await source.get_product("42")
    assert pages[0][0].source_id == "42"
    assert seen[1] == ("/api/customer/Home/GoodsList", {"sort": ["id"], "current": ["1"], "size": ["50"], "goods_category_id": ["12"]})
    assert seen[2] == ("/api/customer/Home/GetGoods", {"goods_id": ["42"]})


@pytest.mark.asyncio
async def test_retries_are_limited_and_shared_gate_runs_on_every_attempt():
    attempts, gates, delays = [], [], []

    async def gate():
        gates.append(True)

    async def sleep(delay):
        delays.append(delay)

    async def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ConnectError("connection failed", request=request)
        if len(attempts) == 2:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return ok({"id": 1})

    async with collector(handler, request_gate=gate, sleep=sleep) as source:
        assert await source.check_session() is True
    assert len(attempts) == len(gates) == 3
    assert delays == [1.0, 7.0]


@pytest.mark.asyncio
async def test_network_retry_exhaustion_never_yields_an_empty_catalogue():
    attempts, delays = [], []

    async def sleep(delay):
        delays.append(delay)

    async def handler(request):
        attempts.append(request)
        return httpx.Response(503)

    async with collector(handler, sleep=sleep) as source:
        with pytest.raises(CollectorError, match="503"):
            _ = [page async for page in source.iter_pages()]
        assert source.expected_total is None
        assert source.errors
    assert len(attempts) == 3
    assert len(delays) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    httpx.Response(401),
    httpx.Response(403),
    httpx.Response(200, json={"code": -1, "msg": "请重新登录", "data": []}),
    httpx.Response(200, json={"code": 401, "data": []}),
    httpx.Response(200, json={"code": 100, "data": []}),
    httpx.Response(200, json={"code": 110, "data": []}),
])
async def test_expired_login_is_distinct_from_empty_products(response):
    attempts = []

    async def handler(request):
        attempts.append(request)
        return response

    async with collector(handler) as source:
        with pytest.raises(AuthExpired):
            _ = [page async for page in source.iter_pages()]
        assert source.expected_total is None
        assert not source.seen_ids
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_missing_token_fails_before_http():
    async def handler(request):
        pytest.fail("must not make an HTTP request without a session")

    async with Xiaohong66Collector(token="", transport=httpx.MockTransport(handler)) as source:
        with pytest.raises(AuthExpired):
            await source.check_session()


@pytest.mark.asyncio
@pytest.mark.parametrize("second_page,match", [
    ({"data": [], "count": 2}, "ended before"),
    ({"data": [product()], "count": 2}, "repeated"),
    ({"data": [product(id=999)], "count": 3}, "count changed"),
    ({"data": [], "count": None}, "total count"),
])
async def test_incomplete_pages_are_reported_as_incomplete(second_page, match):
    async def handler(request):
        if request.url.path.endswith("GoodsCategoryList"):
            return ok([{"id": 12, "name": "分类"}])
        page = request_fields(request)["page"][0]
        return ok({"data": [product()], "count": 2} if page == "1" else second_page)

    async with collector(handler, rule={"page_size": 1}) as source:
        with pytest.raises(IncompleteCollection, match=match):
            _ = [page async for page in source.iter_pages()]
        assert source.expected_total == 2
        assert source.errors


@pytest.mark.asyncio
async def test_duplicate_items_within_one_page_fail_completeness():
    async def handler(request):
        if request.url.path.endswith("GoodsCategoryList"):
            return ok([])
        return ok({"data": [product(), product()], "count": 2})

    async with collector(handler) as source:
        with pytest.raises(IncompleteCollection, match="repeated"):
            _ = [page async for page in source.iter_pages()]
        assert not source.seen_ids


@pytest.mark.asyncio
async def test_complete_multipage_result_preserves_source_identifiers_and_order():
    async def handler(request):
        if request.url.path.endswith("GoodsCategoryList"):
            return ok([])
        page = int(request_fields(request)["page"][0])
        return ok({"data": [product(id=page)], "count": 3})

    async with collector(handler, rule={"page_size": 1}) as source:
        pages = [page async for page in source.iter_pages()]
        assert [[p.source_id for p in page] for page in pages] == [["1"], ["2"], ["3"]]
        assert source.expected_total == len(source.seen_ids) == 3


@pytest.mark.asyncio
async def test_redirects_are_not_followed_and_secrets_not_in_raw_data():
    called = []

    async def handler(request):
        called.append(request.url)
        if request.url.path.endswith("GetGoods"):
            return ok(product(token="hidden", nested={"Authorization": "hidden", "value": Decimal("1").to_eng_string()}, description="test-account-token"))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/admin"})

    async with collector(handler) as source:
        detail = await source.get_product("9585")
        with pytest.raises(CollectorError, match="redirected"):
            await source.check_session()
    assert "token" not in detail.raw
    assert "Authorization" not in detail.raw["nested"]
    assert detail.raw["description"] == "[redacted]"
    assert len(called) == 2


def test_custom_host_requires_explicit_allowlist_and_cannot_use_credentials():
    with pytest.raises(CollectorError, match="allowed_hosts"):
        Xiaohong66Collector("https://unapproved.example", "token")
    with pytest.raises(CollectorError, match="HTTPS origin"):
        Xiaohong66Collector("https://user:pass@www.xiaohong66.com", "token")


@pytest.mark.asyncio
async def test_rule_cannot_redirect_requests_to_an_unapproved_host():
    async def handler(request):
        pytest.fail("must not send the source token to an unapproved host")

    async with collector(handler, rule={"session_path": "https://evil.example/read"}) as source:
        with pytest.raises(CollectorError, match="different origin"):
            await source.check_session()


@pytest.mark.asyncio
async def test_malformed_product_price_never_becomes_zero():
    async def handler(request):
        return ok(product(price=None))

    async with collector(handler) as source:
        with pytest.raises(CollectorError, match="price"):
            await source.get_product("9585")


@pytest.mark.asyncio
async def test_page_limit_fails_instead_of_hanging():
    async def handler(request):
        if request.url.path.endswith("GoodsCategoryList"):
            return ok([])
        return ok({"data": [product()], "count": 2})

    async with collector(handler, rule={"max_pages": 1}) as source:
        with pytest.raises(IncompleteCollection, match="page limit"):
            _ = [page async for page in source.iter_pages()]


@pytest.mark.asyncio
async def test_overlapping_categories_are_checked_individually_then_deduplicated():
    async def handler(request):
        if request.url.path.endswith("GoodsCategoryList"):
            return ok([{"id": 12, "name": "分类一"}, {"id": 13, "name": "分类二"}])
        return ok({"data": [product()], "count": 1})

    async with collector(handler, rule={"per_category": True}) as source:
        pages = [page async for page in source.iter_pages()]
        assert len(pages) == len(pages[0]) == 1
        assert source.expected_total == len(source.seen_ids) == 1


@pytest.mark.asyncio
async def test_default_request_fields_match_public_frontend_and_source_category_field():
    requests = []

    async def handler(request):
        requests.append(request)
        if request.url.path.endswith("GoodsCategoryList"):
            return ok([{"id": 12, "name": "分类"}])
        row = product(goods_category_id=12)
        row.pop("category_id")
        return ok({"data": [row], "count": 1})

    async with collector(handler) as source:
        pages = [page async for page in source.iter_pages()]
    assert pages[0][0].category_id == "12"
    assert request_fields(requests[1], keep_blank_values=True) == {
        "page": ["1"], "list_rows": ["50"], "goods_category_id": ["0"], "goods_name": [""]}
    assert json.loads(requests[1].content) == {"page": 1, "list_rows": 50, "goods_category_id": 0, "goods_name": ""}
    assert requests[1].headers["Content-Type"] == "application/json"
    assert DEFAULT_RULE["request_content_type"] == "application/json"
    assert DEFAULT_RULE["detail_id_field"] == "id"


@pytest.mark.asyncio
async def test_explicit_form_encoding_is_available_for_other_compatible_sources():
    async def handler(request):
        assert request.content.startswith(b"id=")
        assert request.headers["Content-Type"].startswith("application/x-www-form-urlencoded")
        return ok(product())

    async with collector(handler, rule={"request_encoding": "form"}) as source:
        assert (await source.get_product("9585")).source_id == "9585"


@pytest.mark.asyncio
async def test_session_check_reads_account_profile_instead_of_public_categories():
    paths = []

    async def handler(request):
        paths.append(request.url.path)
        assert request.method == "GET"
        assert request.content == b""
        return ok({"id": 123})

    async with collector(handler) as source:
        assert await source.check_session() is True
        assert source.account_identity == "123"
    assert paths == ["/api/customer/Profile/Show"]


@pytest.mark.asyncio
async def test_empty_successful_profile_does_not_count_as_authenticated():
    async def handler(request):
        return ok({})

    async with collector(handler) as source:
        with pytest.raises(AuthExpired, match="authenticated account profile"):
            await source.check_session()


def session_cookie(**changes):
    return {"name": "PHPSESSID", "value": "unit-cookie-value", "domain": ".xiaohong66.com",
            "path": "/", "secure": True, "expires": -1, **changes}


@pytest.mark.asyncio
async def test_cookie_only_session_authenticates_profile_without_forwarding_envelope():
    credential = encode_session_envelope("https://www.xiaohong66.com", cookies=[session_cookie()])

    async def handler(request):
        assert request.headers["Cookie"] == "PHPSESSID=unit-cookie-value"
        assert "Authorization" not in request.headers
        assert SESSION_PREFIX not in str(request.headers)
        return ok({"id": 123})

    async with Xiaohong66Collector(token=credential, transport=httpx.MockTransport(handler)) as source:
        assert await source.check_session() is True


@pytest.mark.asyncio
async def test_session_envelope_only_forwards_actual_authorization_and_bound_cookies():
    credential = encode_session_envelope("https://www.xiaohong66.com", token="unit-auth-value", cookies=[session_cookie(path="/api")])

    async def handler(request):
        assert request.headers["Authorization"] == "unit-auth-value"
        assert request.headers["Cookie"] == "PHPSESSID=unit-cookie-value"
        return ok(product(description="unit-auth-value unit-cookie-value", nested={"text": "unit-cookie-value"}, cookies=["unit-cookie-value"]))

    async with Xiaohong66Collector(token=credential, transport=httpx.MockTransport(handler)) as source:
        result = await source.get_product("9585")
    assert result.raw["description"] == "[redacted] [redacted]"
    assert result.raw["nested"]["text"] == "[redacted]"
    assert "cookies" not in result.raw


@pytest.mark.parametrize("domain", ["evil.example", ".com", ".www.xiaohong66.com.evil.example"])
def test_cookie_envelope_rejects_foreign_and_public_suffix_domains(domain):
    with pytest.raises(CollectorError):
        encode_session_envelope("https://www.xiaohong66.com", cookies=[session_cookie(domain=domain)])


def test_cookie_envelope_rejects_insecure_origins_foreign_origin_and_header_injection():
    with pytest.raises(CollectorError, match="HTTPS"):
        encode_session_envelope("http://www.xiaohong66.com", cookies=[session_cookie()])
    credential = encode_session_envelope("https://other.example", cookies=[session_cookie(domain="other.example")])
    with pytest.raises(CollectorError, match="different origin"):
        decode_session_envelope(credential, "https://www.xiaohong66.com")
    with pytest.raises(CollectorError, match="cookie value"):
        encode_session_envelope("https://www.xiaohong66.com", cookies=[session_cookie(value="bad\r\nheader")])


@pytest.mark.asyncio
async def test_cookie_session_cannot_cross_origin_even_when_extra_host_is_allowlisted():
    credential = encode_session_envelope("https://www.xiaohong66.com", cookies=[session_cookie()])

    async def handler(request):
        pytest.fail("cookie session must not reach another origin")

    rule = {"allowed_hosts": ["other.example"], "session_path": "https://other.example/profile"}
    async with Xiaohong66Collector(token=credential, rule=rule, transport=httpx.MockTransport(handler)) as source:
        with pytest.raises(CollectorError, match="different origin"):
            await source.check_session()


@pytest.mark.asyncio
async def test_native_apex_config_uses_canonical_login_origin_without_cookie_leaks():
    credential = encode_session_envelope("https://www.xiaohong66.com", cookies=[session_cookie(domain="www.xiaohong66.com")])

    async def handler(request):
        assert request.url.host == "www.xiaohong66.com"
        assert request.headers["Cookie"] == "PHPSESSID=unit-cookie-value"
        return ok({"id": 123})

    async with Xiaohong66Collector("https://xiaohong66.com", credential, transport=httpx.MockTransport(handler)) as source:
        assert await source.check_session() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("profile,identity", [
    ({"uid": "source-uid"}, "source-uid"),
    ({"account": "source-account"}, "source-account"),
    ({"username": "source-user"}, "source-user"),
    ({"id": 123, "account": "source-account"}, "123"),
    ({"display_name": "unknown identity"}, None),
])
async def test_profile_identity_is_stable_without_exposing_the_profile(profile, identity):
    async def handler(request):
        return ok(profile)

    async with collector(handler) as source:
        assert await source.check_session() is True
        assert source.account_identity == identity
        assert not hasattr(source, "profile")


@pytest.mark.asyncio
async def test_rotated_cookie_values_are_also_redacted_from_raw_products():
    credential = encode_session_envelope("https://www.xiaohong66.com", cookies=[session_cookie()])

    async def handler(request):
        return httpx.Response(200, json={"code": 0, "data": product(description="unit-rotated-cookie")},
                              headers={"Set-Cookie": "PHPSESSID=unit-rotated-cookie; Path=/; Secure"})

    async with Xiaohong66Collector(token=credential, transport=httpx.MockTransport(handler)) as source:
        result = await source.get_product("9585")
    assert result.raw["description"] == "[redacted]"


@pytest.mark.asyncio
async def test_session_post_remains_configurable_and_browser_headers_stay_origin_bound():
    async def handler(request):
        assert request.method == "POST"
        assert json.loads(request.content) == {}
        assert request.headers["User-Agent"] == "Mozilla/5.0 UnitBrowser/1.0"
        assert request.headers["Origin"] == "https://www.xiaohong66.com"
        assert request.headers["Referer"] == "https://www.xiaohong66.com/"
        return ok({"id": 123})

    async with collector(handler, rule={"session_method": "POST", "user_agent": "Mozilla/5.0 UnitBrowser/1.0"}) as source:
        assert await source.check_session() is True


@pytest.mark.asyncio
async def test_invalid_session_method_fails_before_sending_credentials():
    async def handler(request):
        pytest.fail("unsupported method must not make a request")

    async with collector(handler, rule={"session_method": "DELETE"}) as source:
        with pytest.raises(CollectorError, match="GET or POST"):
            await source.check_session()


@pytest.mark.asyncio
async def test_per_category_collection_without_categories_is_incomplete():
    async def handler(request):
        assert request.url.path.endswith("GoodsCategoryList")
        return ok([])

    async with collector(handler, rule={"per_category": True}) as source:
        with pytest.raises(IncompleteCollection, match="no categories"):
            _ = [page async for page in source.iter_pages()]
        assert source.expected_total is None
        assert source.errors


@pytest.mark.asyncio
async def test_all_catalogue_declared_zero_count_is_a_verified_empty_catalogue():
    async def handler(request):
        if request.url.path.endswith("GoodsCategoryList"):
            return ok([])
        return ok({"data": [], "count": 0})

    async with collector(handler) as source:
        assert [page async for page in source.iter_pages()] == []
        assert source.expected_total == 0
        assert not source.errors


@pytest.mark.asyncio
async def test_category_and_detail_json_reads_use_the_verified_standard_content_type():
    paths = []

    async def handler(request):
        paths.append(request.url.path)
        assert request.method == "POST"
        assert request.headers["Content-Type"] == "application/json"
        body = json.loads(request.content)
        if request.url.path.endswith("GoodsCategoryList"):
            assert body == {}
            return ok([{"id": 12, "name": "分类"}])
        assert body == {"id": 9585}
        return ok(product())

    async with collector(handler) as source:
        assert len(await source.list_categories()) == 1
        assert (await source.get_product("9585")).source_id == "9585"
    assert paths == ["/api/customer/Home/GoodsCategoryList", "/api/customer/Home/GetGoods"]


@pytest.mark.asyncio
async def test_actual_detail_sample_zero_quantity_without_unit_keeps_its_exact_price():
    async def handler(request):
        assert json.loads(request.content) == {"id": 9585}
        return httpx.Response(200, content=b'{"code":0,"data":{"id":9585,"name":"sample","goods_category_id":12,"goods_category_name":"category","price":0.007447,"price_show_num":0,"unit":"","is_close":2}}')

    async with collector(handler) as source:
        result = await source.get_product("9585")
    assert result.price == Decimal("0.007447")
    assert result.display_quantity == Decimal("0")
    assert result.unit == ""
    assert result.status == "active"
    assert result.category_id == "12"
    assert result.category_name == "category"
    assert result.quality == ["unit_requires_review"]


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity,unit,critical", [("0", "", False), ("0", "人", True), ("-1", "", True), ("1", "", False)])
async def test_quantity_quality_matches_source_display_semantics(quantity, unit, critical):
    async def handler(request):
        return ok(product(price_show_num=quantity, unit=unit))

    async with collector(handler) as source:
        result = await source.get_product("9585")
    assert ("invalid_display_quantity" in result.quality) is critical


@pytest.mark.asyncio
@pytest.mark.parametrize("source_id", ["-1", "+1", "1.0", " 9585", "９５８５", "١٢", "abc", "", True, None])
async def test_detail_identifier_rejects_non_ascii_or_negative_values_before_http(source_id):
    async def handler(request):
        pytest.fail("invalid product identifier must not reach the source")

    async with collector(handler) as source:
        with pytest.raises(CollectorError, match="nonnegative integer"):
            await source.get_product(source_id)
