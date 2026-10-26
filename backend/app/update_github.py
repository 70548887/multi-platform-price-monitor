"""Bounded GitHub Release access; credentials never follow a redirect."""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from .update_common import UpdateError, compare_versions, strict_semver
from .update_config import UpdateConfig

PACKAGE_LIMIT = 512 * 1024 * 1024
CHECKSUM_LIMIT = 64 * 1024
METADATA_LIMIT = 2 * 1024 * 1024
DOWNLOAD_HOSTS = frozenset({"api.github.com", "github.com", "objects.githubusercontent.com",
                            "release-assets.githubusercontent.com", "github-releases.githubusercontent.com"})


def validate_download_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme == "https" and parsed.hostname in DOWNLOAD_HOSTS and parsed.port in (None, 443) and not parsed.username and not parsed.password and not parsed.fragment
    except (ValueError, TypeError):
        valid = False
    if not valid or any(ord(c) < 32 for c in url):
        raise UpdateError("unsafe_download_url", "更新下载地址不在允许的 GitHub 域名内")
    return url


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag: str
    release_id: int
    asset_id: int
    checksum_asset_id: int
    asset_name: str
    name: str = ""
    notes: str = ""
    published_at: str = ""
    html_url: str = ""
    size: int = 0
    sha256: str = ""

    def as_dict(self) -> dict:
        return asdict(self)

    def public_dict(self) -> dict:
        return self.as_dict()

    @classmethod
    def from_dict(cls, values: dict) -> "ReleaseInfo":
        try:
            release = cls(**values)
        except (TypeError, ValueError):
            raise UpdateError("invalid_release", "更新版本记录无效") from None
        if release.tag != "v" + strict_semver(release.version) or any(type(value) is not int or value <= 0 for value in (release.release_id, release.asset_id, release.checksum_asset_id)):
            raise UpdateError("invalid_release", "更新版本标识无效")
        if type(release.size) is not int or release.size <= 0 or release.size > PACKAGE_LIMIT or not isinstance(release.asset_name, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", release.asset_name):
            raise UpdateError("invalid_release", "更新资产信息无效")
        if release.sha256 and (not isinstance(release.sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", release.sha256)):
            raise UpdateError("invalid_release", "更新资产校验值无效")
        if not all(isinstance(value, str) for value in (release.name, release.notes, release.published_at, release.html_url)):
            raise UpdateError("invalid_release", "更新版本描述无效")
        if release.html_url:
            validate_download_url(release.html_url)
            if urlsplit(release.html_url).hostname != "github.com":
                raise UpdateError("invalid_release", "更新版本页面地址无效")
        return release


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    sha256: str
    size: int


def parse_checksum(content: bytes | str, filename: str) -> str:
    try:
        text = content.decode("utf-8") if isinstance(content, bytes) else content
    except UnicodeError:
        raise UpdateError("invalid_checksum", "更新校验文件编码错误") from None
    if len(text.encode("utf-8")) > CHECKSUM_LIMIT:
        raise UpdateError("checksum_too_large", "更新校验文件过大")
    matches = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = re.fullmatch(r"([0-9A-Fa-f]{64})[ \t]+\*?([^\r\n]+)", line)
        if not match:
            raise UpdateError("invalid_checksum", "更新校验文件格式错误")
        if match[2] == filename:
            matches.append(match[1].lower())
    if len(matches) != 1:
        raise UpdateError("invalid_checksum", "更新校验值未唯一绑定目标文件名")
    return matches[0]


class GitHubReleaseClient:
    def __init__(self, config: UpdateConfig, client: httpx.AsyncClient | None = None):
        self.config = config
        self.client = client

    def _api(self, suffix: str) -> str:
        return f"https://api.github.com/repos/{self.config.github_owner}/{self.config.github_repo}/{suffix}"

    async def _read(self, url: str, limit: int, *, binary: bool = False, output=None, expected_size: int | None = None,
                    progress_callback=None):
        own_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15),
                                                  proxy=self.config.proxy or None, follow_redirects=False, trust_env=False)
        current = validate_download_url(url)
        try:
            for hop in range(6):
                headers = {"Accept": "application/octet-stream" if binary else "application/vnd.github+json",
                           "User-Agent": "price-monitor-updater/1", "X-GitHub-Api-Version": "2022-11-28",
                           "Accept-Encoding": "identity"}
                if hop == 0 and urlsplit(current).hostname == "api.github.com" and self.config.github_token:
                    headers["Authorization"] = "Bearer " + self.config.github_token
                request = client.build_request("GET", current, headers=headers)
                # A supplied test/application client may carry authentication defaults.
                if not (hop == 0 and urlsplit(current).hostname == "api.github.com" and self.config.github_token):
                    request.headers.pop("Authorization", None)
                request.headers.pop("Cookie", None)
                response = await client.send(request, auth=None, stream=True, follow_redirects=False)
                try:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise UpdateError("invalid_redirect", "GitHub 更新下载重定向无效", 502)
                        current = validate_download_url(urljoin(current, location))
                        continue
                    if response.status_code in (403, 429) and (response.status_code == 429 or response.headers.get("x-ratelimit-remaining") == "0"):
                        raise UpdateError("github_rate_limited", "GitHub 请求达到限额，请稍后重试", 429)
                    if response.status_code == 404:
                        raise UpdateError("release_not_found", "GitHub 未找到正式版本或仓库访问权限不足", 404)
                    if response.status_code in (401, 403):
                        raise UpdateError("github_access_denied", "GitHub 访问被拒绝，请检查更新凭据与仓库权限", 502)
                    if response.status_code != 200:
                        raise UpdateError("github_request_failed", "GitHub 更新请求失败", 502)
                    length = response.headers.get("content-length")
                    if length and (not length.isdigit() or int(length) > limit):
                        raise UpdateError("download_too_large", "GitHub 更新响应超过大小限制", 502)
                    chunks, size, digest = [], 0, hashlib.sha256()
                    reported_size, reported_at = 0, time.monotonic()
                    async for chunk in response.aiter_bytes(256 * 1024):
                        size += len(chunk)
                        if size > limit:
                            raise UpdateError("download_too_large", "GitHub 更新响应超过大小限制", 502)
                        digest.update(chunk)
                        if output is None:
                            chunks.append(chunk)
                        else:
                            output.write(chunk)
                        if progress_callback and (size - reported_size >= 256 * 1024 or time.monotonic() - reported_at >= 0.5):
                            result = progress_callback(size, expected_size or (int(length) if length else None))
                            if inspect.isawaitable(result):
                                await result
                            reported_size, reported_at = size, time.monotonic()
                    if expected_size is not None and size != expected_size:
                        raise UpdateError("download_size_mismatch", "更新包下载大小与版本信息不一致", 502)
                    if length and size != int(length):
                        raise UpdateError("download_size_mismatch", "GitHub 更新响应未完整下载", 502)
                    if progress_callback and size != reported_size:
                        result = progress_callback(size, expected_size or (int(length) if length else None))
                        if inspect.isawaitable(result):
                            await result
                    return b"".join(chunks) if output is None else (digest.hexdigest(), size)
                finally:
                    await response.aclose()
            raise UpdateError("too_many_redirects", "GitHub 更新下载重定向过多", 502)
        except UpdateError:
            raise
        except (httpx.HTTPError, OSError, ValueError):
            raise UpdateError("github_connection_failed", "GitHub 更新服务连接失败，请检查网络或代理配置", 502) from None
        finally:
            if own_client:
                await client.aclose()

    async def check(self, current_version: str) -> ReleaseInfo | None:
        strict_semver(current_version)
        if not self.config.enabled:
            raise UpdateError("updates_disabled", "在线更新功能已禁用", 409)
        try:
            try:
                content = await self._read(self._api("releases/latest"), METADATA_LIMIT)
            except UpdateError as error:
                if error.code != "release_not_found":
                    raise
                # /latest returns the same 404 for no release and inaccessible repositories.
                # Confirm repository visibility before treating it as an empty release list.
                repository = json.loads(await self._read(self._api(""), METADATA_LIMIT))
                expected_repository = f"{self.config.github_owner}/{self.config.github_repo}"
                if not isinstance(repository, dict) or not isinstance(repository.get("full_name"), str) or repository["full_name"].casefold() != expected_repository.casefold():
                    raise UpdateError("invalid_release", "GitHub 仓库信息与更新配置不一致", 502)
                return None
            raw = json.loads(content)
        except (json.JSONDecodeError, UnicodeError):
            raise UpdateError("invalid_release", "GitHub 版本响应格式错误", 502) from None
        if not isinstance(raw, dict) or raw.get("draft") is not False or raw.get("prerelease") is not False:
            raise UpdateError("invalid_release", "仅支持 GitHub 正式发布版本", 502)
        tag = raw.get("tag_name", "")
        if not isinstance(tag, str) or not tag.startswith("v"):
            raise UpdateError("invalid_release", "GitHub 版本标签必须使用 vX.Y.Z", 502)
        version = strict_semver(tag[1:])
        if compare_versions(version, current_version) <= 0:
            return None
        name = self.config.release_asset_pattern.replace("{version}", version)
        assets = raw.get("assets")
        if not isinstance(assets, list):
            raise UpdateError("invalid_release", "GitHub 版本缺少资产列表", 502)
        selected = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == name]
        checksums = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == name + ".sha256"]
        if len(selected) != 1 or len(checksums) != 1:
            raise UpdateError("release_asset_missing", "正式版本必须包含唯一更新包及同名 SHA256 校验文件", 502)
        asset, checksum = selected[0], checksums[0]
        for item in (asset, checksum):
            if item.get("state") != "uploaded":
                raise UpdateError("invalid_release", "GitHub 版本资产尚未完整上传", 502)
            validate_download_url(item.get("browser_download_url", ""))
        if type(checksum.get("size")) is not int or not 0 < checksum["size"] <= CHECKSUM_LIMIT:
            raise UpdateError("invalid_release", "GitHub 校验文件大小无效", 502)
        return ReleaseInfo.from_dict({
            "version": version, "tag": tag, "release_id": raw.get("id"),
            "asset_id": asset.get("id"), "checksum_asset_id": checksum.get("id"),
            "asset_name": name, "name": raw.get("name") or tag,
            "notes": (raw.get("body") or "")[:100000], "published_at": raw.get("published_at") or "",
            "html_url": raw.get("html_url") or "", "size": asset.get("size"),
        })

    async def download(self, release: ReleaseInfo, destination: Path | str, progress_callback=None) -> DownloadResult:
        if not self.config.enabled:
            raise UpdateError("updates_disabled", "在线更新功能已禁用", 409)
        release = ReleaseInfo.from_dict(release.as_dict())
        if release.asset_name != self.config.release_asset_pattern.replace("{version}", release.version):
            raise UpdateError("invalid_release", "更新包名称与配置不一致")
        checksum = await self._read(self._api(f"releases/assets/{release.checksum_asset_id}"), CHECKSUM_LIMIT, binary=True)
        expected = parse_checksum(checksum, release.asset_name)
        if release.sha256 and release.sha256 != expected:
            raise UpdateError("checksum_changed", "更新包校验值发生变化")
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            raise UpdateError("unsafe_download_target", "更新包保存路径不安全")
        target, partial = destination / release.asset_name, destination / (release.asset_name + ".partial")
        if target.exists() or partial.exists():
            raise UpdateError("download_already_exists", "更新包目录已有文件，请创建新的下载任务", 409)
        try:
            with partial.open("xb") as output:
                os.chmod(partial, 0o600)
                actual, size = await self._read(self._api(f"releases/assets/{release.asset_id}"), PACKAGE_LIMIT,
                                                binary=True, output=output, expected_size=release.size,
                                                progress_callback=progress_callback)
                output.flush()
                os.fsync(output.fileno())
            if actual != expected:
                raise UpdateError("checksum_mismatch", "更新包 SHA256 校验失败", 502)
            os.replace(partial, target)
            return DownloadResult(target, actual, size)
        finally:
            if partial.exists():
                partial.unlink()
