"""Assert the chosen proxy reaches HTTPX; real upstream and proxy traffic is forbidden."""
import httpx
import pytest

from app.collectors.xiaohong66 import Xiaohong66Collector
from app.collectors.fans8 import Fans8Collector
from app.collectors.base import CollectorError, safe_collection_error


@pytest.mark.parametrize('collector_class', [Xiaohong66Collector, Fans8Collector])
@pytest.mark.asyncio
async def test_proxy_is_explicit_and_credentials_are_not_source_headers(monkeypatch, collector_class):
    seen = []
    options = []
    real_client = httpx.AsyncClient

    def handler(request):
        seen.append(request)
        assert 'proxy-authorization' not in request.headers
        assert 'fixture-proxy-password' not in str(request.url)
        return httpx.Response(200, json={'code': 0, 'error': 0, 'data': []})

    def factory(**kwargs):
        options.append(kwargs)
        return real_client(transport=httpx.MockTransport(handler), trust_env=False)

    monkeypatch.setattr(httpx, 'AsyncClient', factory)
    proxy = 'http://fixture-proxy-user:fixture-proxy-password@proxy.example:3128'
    base = 'https://fans8.top' if collector_class is Fans8Collector else 'https://www.xiaohong66.com'
    source = collector_class(base, 'fixture-source-token', proxy_url=proxy)
    try:
        async def validate(_):
            pass
        source._validate_host = validate
        if isinstance(source, Fans8Collector):
            await source._get('/api/goodsClassList', {'IsAll': 1})
        else:
            await source._post('/customer/Home/GoodsCategoryList', {})
        assert options[0]['proxy'] == proxy
        assert options[0]['trust_env'] is False
        assert seen
        assert proxy in source._redaction_secrets()
        assert 'fixture-proxy-password' in source._redaction_secrets()
    finally:
        await source.aclose()


@pytest.mark.parametrize('collector_class', [Xiaohong66Collector, Fans8Collector])
@pytest.mark.asyncio
async def test_proxy_errors_retry_without_leaking_credentials(monkeypatch, collector_class):
    real_client = httpx.AsyncClient
    requests = []
    sleeps = []
    def handler(request):
        requests.append(request)
        raise httpx.ProxyError('fixture-sensitive-proxy-password')
    def factory(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), trust_env=False)
    async def pause(delay):
        sleeps.append(delay)
    monkeypatch.setattr(httpx, 'AsyncClient', factory)
    base = 'https://fans8.top' if collector_class is Fans8Collector else 'https://www.xiaohong66.com'
    source = collector_class(base, 'fixture-source-token', proxy_url='http://proxy.example:3128', sleep=pause)
    async def validate(_):
        pass
    source._validate_host = validate
    try:
        with pytest.raises(CollectorError) as error:
            if isinstance(source, Fans8Collector):
                await source._get('/api/goodsClassList', {})
            else:
                await source._post('/customer/Home/GoodsCategoryList', {})
        assert len(requests) == 3 and len(sleeps) == 2
        assert safe_collection_error(error.value) == '采集代理连接失败，有限重试后仍未恢复'
        assert 'fixture-sensitive' not in str(error.value)
    finally:
        await source.aclose()


@pytest.mark.parametrize('value', ['http://proxy.example', 'socks5://proxy.example:1234',
                                 'http://proxy.example:3128/path', 'http://proxy.example:3128?secret=value',
                                 'http://proxy.example:3128\n'])
def test_invalid_proxy_is_rejected_without_input(value):
    with pytest.raises(CollectorError) as error:
        Xiaohong66Collector(token='fixture-source-token', proxy_url=value)
    assert str(error.value) == 'Source proxy configuration is invalid'


def test_injected_client_cannot_silently_bypass_selected_proxy():
    with pytest.raises(CollectorError, match='injected'):
        Xiaohong66Collector(token='fixture-source-token', proxy_url='http://proxy.example:3128', transport=httpx.MockTransport(lambda _: None))


def test_local_http_error_summary_keeps_status_but_no_exception_text():
    assert safe_collection_error(CollectorError('Source HTTP 502 after limited retries')) == '源站 HTTP 502，有限重试后仍未恢复'
    assert safe_collection_error(CollectorError('Source HTTP 502 after limited retries secret')) == '采集执行异常：CollectorError'


@pytest.mark.asyncio
async def test_fixed_fans8_origin_can_use_proxy_without_local_dns(monkeypatch):
    import asyncio
    loop = asyncio.get_running_loop()
    async def unavailable(*args, **kwargs):
        raise AssertionError('A trusted native origin must resolve at the proxy')
    monkeypatch.setattr(loop, 'getaddrinfo', unavailable)
    source = Fans8Collector(token='fixture-source-token', proxy_url='http://proxy.example:3128')
    try:
        await source._validate_host('https://fans8.top/api/goodsList')
    finally:
        await source.aclose()
