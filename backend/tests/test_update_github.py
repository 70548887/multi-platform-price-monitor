from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from app.update_common import UpdateError
from app.update_config import UpdateConfig
from app.update_github import (GitHubReleaseClient, ReleaseInfo, parse_checksum,
                               validate_download_url)


ASSET = "price-monitor-1.2.0.tar.gz"
PACKAGE = b"fixture-release-archive"


def release_json():
    return {"id": 40, "draft": False, "prerelease": False, "tag_name": "v1.2.0",
            "name": "Release 1.2.0", "body": "changes", "published_at": "2026-10-26T00:00:00Z",
            "html_url": "https://github.com/70548887/multi-platform-price-monitor/releases/tag/v1.2.0",
            "assets": [{"id": number, "name": name, "state": "uploaded", "size": size,
                        "browser_download_url": "https://github.com/70548887/multi-platform-price-monitor/releases/download/v1.2.0/" + name}
                       for number, name, size in [(41, ASSET, len(PACKAGE)), (42, ASSET + ".sha256", 100)]]}


def release_info():
    return ReleaseInfo("1.2.0", "v1.2.0", 40, 41, 42, ASSET, size=len(PACKAGE))


@pytest.mark.parametrize("url", ["http://github.com/a", "https://github.com.evil.example/a",
                                    "https://api.github.com@evil.example/a", "https://evil.example/a",
                                    "https://github.com:8443/a", "https://user:pass@github.com/a",
                                    "file:///tmp/a", "https://api.github.com/a#fragment", "https://github.com/\n"])
def test_download_url_exact_https_whitelist(url):
    with pytest.raises(UpdateError):
        validate_download_url(url)


def test_checksum_must_bind_exact_asset_filename():
    digest = hashlib.sha256(PACKAGE).hexdigest()
    assert parse_checksum(f"{digest}  {ASSET}\n", ASSET) == digest
    assert parse_checksum(f"{digest} *{ASSET}\n", ASSET) == digest
    for value in (digest, f"{digest}  wrong.tar.gz", f"{digest}  {ASSET}\n{digest}  {ASSET}", "f" * 70000):
        with pytest.raises(UpdateError):
            parse_checksum(value, ASSET)


@pytest.mark.asyncio
async def test_check_uses_github_api_and_returns_validated_release(tmp_path):
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(200, json=release_json())

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        github = GitHubReleaseClient(UpdateConfig(root=tmp_path, github_token="fixture-only-token"), client)
        result = await github.check("1.1.0")
        assert result.version == "1.2.0" and result.asset_id == 41
        assert ReleaseInfo.from_dict(result.as_dict()) == result
        assert await github.check("1.2.0") is None
        assert await github.check("2.0.0") is None
    assert requests[0].url.path.endswith("/releases/latest")
    assert requests[0].headers["authorization"] == "Bearer fixture-only-token"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["prerelease", "draft", "tag", "missing_checksum", "duplicate", "asset_host", "oversize"])
async def test_bad_release_metadata_is_rejected(tmp_path, change):
    value = release_json()
    if change in ("draft", "prerelease"):
        value[change] = True
    elif change == "tag":
        value["tag_name"] = "v1.2.0-rc1"
    elif change == "missing_checksum":
        value["assets"].pop()
    elif change == "duplicate":
        value["assets"].append(value["assets"][0])
    elif change == "asset_host":
        value["assets"][0]["browser_download_url"] = "https://evil.example/payload"
    elif change == "oversize":
        value["assets"][0]["size"] = 513 * 1024 ** 2
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=value))) as client:
        with pytest.raises(UpdateError):
            await GitHubReleaseClient(UpdateConfig(root=tmp_path), client).check("1.1.0")


@pytest.mark.asyncio
async def test_token_cookie_and_client_auth_do_not_follow_cdn_redirect(tmp_path):
    observed = []
    digest = hashlib.sha256(PACKAGE).hexdigest()

    def transport(request):
        observed.append((str(request.url), dict(request.headers)))
        if request.url.path.endswith("/42"):
            return httpx.Response(302, headers={"location": "https://objects.githubusercontent.com/checksum"})
        if request.url.path == "/checksum":
            return httpx.Response(200, content=f"{digest}  {ASSET}\n".encode())
        if request.url.path.endswith("/41"):
            return httpx.Response(302, headers={"location": "https://release-assets.githubusercontent.com/package"})
        return httpx.Response(200, content=PACKAGE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), auth=("fixture-user", "fixture-pass"),
                                 headers={"Cookie": "fixture-cookie", "Authorization": "unexpected-default"}) as client:
        github = GitHubReleaseClient(UpdateConfig(root=tmp_path, github_token="fixture-token"), client)
        downloaded = await github.download(release_info(), tmp_path / "download")
    assert downloaded.path.read_bytes() == PACKAGE and downloaded.sha256 == digest
    for url, headers in observed:
        assert "cookie" not in headers
        if url.startswith("https://api.github.com/"):
            assert headers["authorization"] == "Bearer fixture-token"
        else:
            assert "authorization" not in headers


@pytest.mark.asyncio
async def test_redirect_to_unapproved_host_is_rejected_without_request(tmp_path):
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://evil.example/stolen"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(UpdateError) as error:
            await GitHubReleaseClient(UpdateConfig(root=tmp_path, github_token="fixture-secret"), client).check("1.1.0")
    assert error.value.code == "unsafe_download_url" and len(requests) == 1
    assert "fixture-secret" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,headers,code", [(429, {}, "github_rate_limited"), (403, {"x-ratelimit-remaining": "0"}, "github_rate_limited"),
                                                (401, {}, "github_access_denied"), (404, {}, "release_not_found"), (500, {}, "github_request_failed")])
async def test_github_errors_do_not_include_response_or_credentials(tmp_path, status, headers, code):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status, headers=headers, text="fixture-secret-body"))) as client:
        with pytest.raises(UpdateError) as error:
            await GitHubReleaseClient(UpdateConfig(root=tmp_path, github_token="fixture-secret-token"), client).check("1.1.0")
    assert error.value.code == code and "fixture-secret" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["checksum", "size", "too_large", "stream_limit"])
async def test_download_failure_leaves_no_partial_or_verified_package(tmp_path, failure):
    digest = hashlib.sha256(b"wrong" if failure == "checksum" else PACKAGE).hexdigest()

    def transport(request):
        if request.url.path.endswith("/42"):
            return httpx.Response(200, content=f"{digest}  {ASSET}\n".encode())
        if failure == "too_large":
            return httpx.Response(200, content=PACKAGE, headers={"content-length": str(513 * 1024 ** 2)})
        return httpx.Response(200, content=PACKAGE + b"bad" if failure == "size" else PACKAGE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        github = GitHubReleaseClient(UpdateConfig(root=tmp_path), client)
        if failure == "stream_limit":
            with pytest.raises(UpdateError):
                await github._read("https://api.github.com/test", limit=2)
            return
        with pytest.raises(UpdateError):
            await github.download(release_info(), tmp_path / "download")
    assert list((tmp_path / "download").iterdir()) == []


@pytest.mark.asyncio
async def test_network_error_is_sanitized(tmp_path):
    def transport(request):
        raise httpx.ConnectError("proxy-password fixture-private", request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(UpdateError) as error:
            await GitHubReleaseClient(UpdateConfig(root=tmp_path), client).check("1.1.0")
    assert error.value.code == "github_connection_failed" and "fixture-private" not in str(error.value)


@pytest.mark.asyncio
async def test_no_release_is_distinguished_from_inaccessible_repository(tmp_path):
    def transport(request):
        if request.url.path.endswith("/releases/latest"):
            return httpx.Response(404)
        return httpx.Response(200, json={"full_name": "70548887/multi-platform-price-monitor"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        assert await GitHubReleaseClient(UpdateConfig(root=tmp_path), client).check("1.1.0") is None


@pytest.mark.asyncio
async def test_sync_and_async_progress_callbacks_receive_bounded_stream_progress(tmp_path):
    package = b"a" * (800 * 1024)
    digest = hashlib.sha256(package).hexdigest()

    def transport(request):
        if request.url.path.endswith("/42"):
            return httpx.Response(200, content=f"{digest}  {ASSET}\n".encode())
        return httpx.Response(200, content=package)

    info = ReleaseInfo("1.2.0", "v1.2.0", 40, 41, 42, ASSET, size=len(package))
    progress = []
    async def callback(size, total):
        progress.append((size, total))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        github = GitHubReleaseClient(UpdateConfig(root=tmp_path), client)
        await github.download(info, tmp_path / "async", progress_callback=callback)
        assert progress[-1] == (len(package), len(package)) and len(progress) == 4
        sync_progress = []
        await github.download(info, tmp_path / "sync", progress_callback=lambda size, total: sync_progress.append((size, total)))
        assert sync_progress == progress


@pytest.mark.asyncio
async def test_owned_clients_keep_proxy_across_release_checksum_and_redirects(tmp_path, monkeypatch):
    import app.update_github as module
    original_client = httpx.AsyncClient
    observed_options, requests = [], []
    digest = hashlib.sha256(PACKAGE).hexdigest()
    def handler(request):
        requests.append(request)
        path = request.url.path
        if path.endswith('/releases/latest'):
            return httpx.Response(200, json=release_json())
        if request.url.host == 'api.github.com':
            number = path.rsplit('/', 1)[-1]
            return httpx.Response(302, headers={'Location': 'https://release-assets.githubusercontent.com/' + number})
        assert 'Authorization' not in request.headers
        if path == '/42':
            return httpx.Response(200, content=(digest + '  ' + ASSET + '\n').encode())
        return httpx.Response(200, content=PACKAGE)
    def factory(**options):
        observed_options.append(options)
        return original_client(transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False)
    monkeypatch.setattr(module.httpx, 'AsyncClient', factory)
    settings = UpdateConfig(root=tmp_path, proxy='http://fixture:private@proxy.example:8080', github_token='fixture-token')
    github = GitHubReleaseClient(settings)
    release = await github.check('1.1.0')
    downloaded = await github.download(release, tmp_path / 'package')
    assert downloaded.sha256 == digest
    assert len(observed_options) >= 3
    assert all(option['proxy'] == settings.proxy and option['trust_env'] is False for option in observed_options)
    assert any(request.url.host == 'release-assets.githubusercontent.com' for request in requests)


@pytest.mark.asyncio
async def test_proxy_connection_failure_does_not_retry_direct(tmp_path, monkeypatch):
    import app.update_github as module
    original_client = httpx.AsyncClient
    routes = []
    def handler(request):
        raise httpx.ProxyError('fixture-proxy-private-password', request=request)
    def factory(**options):
        routes.append(options.get('proxy'))
        return original_client(transport=httpx.MockTransport(handler), trust_env=False)
    monkeypatch.setattr(module.httpx, 'AsyncClient', factory)
    with pytest.raises(UpdateError) as error:
        await GitHubReleaseClient(UpdateConfig(root=tmp_path, proxy='http://fixture:private@proxy.example:8080')).check('1.1.0')
    assert len(routes) == 1 and routes[0]
    assert error.value.code == 'github_connection_failed'
    assert 'private' not in error.value.message
