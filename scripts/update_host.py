"""Privileged host-side release installer; never runs inside the API container.

The API places HMAC-authenticated jobs in a local spool. This daemon accepts
only that protocol, not shell commands, arbitrary download URLs or paths.
All application changes happen after draining collection/AI work and stopping
the application services declared by that version. PostgreSQL, Redis and user data stay in place.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import time
from typing import Callable
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid
import yaml


SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS.parent / "backend"))
sys.path.insert(0, str(SCRIPTS))
from app.update_common import (  # noqa: E402
    UpdateError, atomic_write, compare_versions, read_signed_json, safe_extract_tar,
    sign_document, strict_semver, validate_manifest, validate_relative_path, write_signed_json,
)
from package_release import managed_path, collect_files, version_from_source  # noqa: E402


APP_SERVICES = ("api", "worker", "beat", "web")
AI_APP_SERVICES = (*APP_SERVICES, 'ai-worker')
TERMINAL_STATES = {"succeeded", "rolled_back", "failed", "applied"}
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
UPDATER_CODE = ('scripts/update_host.py', 'scripts/package_release.py',
                'backend/app/update_common.py', 'backend/app/update_config.py')


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def code_stamp(root: Path) -> tuple:
    """Fingerprint only updater implementation files, never local settings."""
    result = []
    for relative in UPDATER_CODE:
        path = root / relative
        if path.is_symlink():
            raise UpdateError('UNSAFE_UPDATER_CODE', '更新器实现路径不能包含链接')
        if not path.is_file():
            result.append((relative, None))
            continue
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        result.append((relative, digest.hexdigest()))
    return tuple(result)


def unfinished_journal(data_dir: Path) -> bool:
    return any(re.fullmatch(r'[1-9][0-9]*\.json', path.name)
               for path in (data_dir / 'journal').glob('[0-9]*.json'))


def read_env(path: Path) -> dict[str, str]:
    """Minimal .env reader for generated deployment files, without logging values."""
    result: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name.strip()):
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                    value = value[1:-1]
                result[name.strip()] = value
    return result


def reject_links(path: Path, boundary: Path) -> None:
    boundary = boundary.resolve()
    if not path.absolute().is_relative_to(boundary):
        raise UpdateError("UNSAFE_PATH", "更新路径超出受管目录")
    current = path
    while current != boundary:
        if current.exists() or current.is_symlink():
            stat = current.lstat()
            if current.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
                raise UpdateError("UNSAFE_PATH", "更新路径包含链接或重解析点")
        if current == current.parent:
            raise UpdateError("UNSAFE_PATH", "更新路径超出受管目录")
        current = current.parent


def safe_child(root: Path, name: str) -> Path:
    relative = validate_relative_path(name)
    candidate = root.joinpath(*relative.parts)
    reject_links(candidate, root)
    return candidate


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)


@contextmanager
def process_lock(path: Path):
    """An OS-held lock is released on a crash; a stale PID file is not a lock."""
    private_directory(path.parent)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise UpdateError("HOST_ALREADY_RUNNING", "宿主机更新器已在运行") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class _WindowsCommandJob:
    """A private kernel-owned process tree; no PID enumeration or taskkill."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                        ('flags', wintypes.DWORD), ('minimum_working_set', ctypes.c_size_t),
                        ('maximum_working_set', ctypes.c_size_t), ('active_processes', wintypes.DWORD),
                        ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                        ('scheduling', wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ('read_operations', 'write_operations', 'other_operations',
                         'read_bytes', 'write_bytes', 'other_bytes')]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [('basic', BasicLimits), ('io', IoCounters),
                        ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                        ('peak_process_memory', ctypes.c_size_t), ('peak_job_memory', ctypes.c_size_t)]

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel = kernel
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), 'Could not create command job')
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, without breakaway.
        self.limits = limits
        if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise OSError(ctypes.get_last_error(), 'Could not protect command job')

    def assign(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise OSError('Could not assign the command to its job')

    def terminate(self):
        if not self.kernel.TerminateJobObject(self.handle, 1):
            raise OSError('Could not terminate command job')

    def release(self):
        # A successful native restart intentionally launches long-lived API,
        # Worker, Beat and web children. Keep them running after the command.
        import ctypes
        self.limits.basic.flags = 0
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(self.limits), ctypes.sizeof(self.limits)):
            raise OSError('Could not release the completed command job')

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class CommandRunner:
    """Subprocess output is private in memory and never enters public results."""
    def __init__(self, heartbeat: Callable[[], None]):
        self.heartbeat = heartbeat

    def __call__(self, arguments: list[str], *, cwd: Path, timeout: int = 600) -> str:
        env = os.environ.copy()
        env.update(read_env(cwd / ".env"))
        # The host's own executable must remain independent of the application venv.
        job = None
        process = None
        command, process_options, initial_input = arguments, {}, None
        try:
            if os.name == 'nt':
                job = _WindowsCommandJob()
                # The isolated interpreter waits on stdin until it belongs to
                # the job. Only then may it launch the actual command, so even
                # fast-spawning npm/pip descendants cannot escape containment.
                gate = 'import json,subprocess,sys; args=json.loads(sys.stdin.buffer.read()); raise SystemExit(subprocess.call(args,stdin=subprocess.DEVNULL))'
                command = [getattr(sys, '_base_executable', sys.executable), '-I', '-S', '-c', gate]
                process_options = {'creationflags': subprocess.CREATE_NO_WINDOW, 'stdin': subprocess.PIPE}
                initial_input = json.dumps(arguments).encode('utf-8')
            else:
                process_options = {'start_new_session': True}
            process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, **process_options)
            if job:
                job.assign(process)
        except OSError as exc:
            if process:
                process.kill()
                process.wait(timeout=2)
            if job:
                job.close()
            raise UpdateError("COMMAND_UNAVAILABLE", "所需的宿主机运行工具不可用") from exc
        started = time.monotonic()
        # communicate drains the pipe, avoiding a deadlock on package-manager output.
        try:
            while True:
                try:
                    remaining = max(0.01, timeout - (time.monotonic() - started))
                    output, _ = process.communicate(input=initial_input, timeout=min(4, remaining))
                    break
                except subprocess.TimeoutExpired:
                    initial_input = None
                    self.heartbeat()
                    if time.monotonic() - started >= timeout:
                        if job:
                            job.terminate()
                        else:
                            # The leader has not been reaped while communicate
                            # is waiting, so this group still belongs to us.
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        try:
                            process.communicate(timeout=2)
                        except subprocess.TimeoutExpired:
                            # Cleanup must remain bounded even if a descendant
                            # deliberately detached from its Unix session.
                            pass
                        raise UpdateError("COMMAND_TIMEOUT", "宿主机步骤执行超时")
        finally:
            if job:
                try:
                    if process.returncode == 0:
                        job.release()
                finally:
                    job.close()
        self.heartbeat()
        if process.returncode:
            raise UpdateError("COMMAND_FAILED", "宿主机步骤执行失败，请检查本机运行环境")
        if len(output) > 4 * 1024 * 1024:
            raise UpdateError("COMMAND_OUTPUT_LIMIT", "宿主机步骤输出超出上限")
        return output.decode("utf-8", errors="replace").strip()


class HostProbe:
    def __init__(self, health_url: str, secret: str):
        parts = urlsplit(health_url)
        if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost", "::1"} or parts.username or parts.password or parts.query or parts.fragment:
            raise UpdateError("INVALID_HEALTH_URL", "健康检查只允许本机 HTTP 地址")
        self.origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        self.secret = secret
        # Never send a host proof, administrator state or loopback traffic to a proxy.
        self.opener = build_opener(ProxyHandler({}), NoHostRedirect())

    def __call__(self, purpose: str, run_id: int) -> dict:
        payload = {"purpose": purpose, "run_id": run_id, "at": utcnow()}
        proof = base64.urlsafe_b64encode(json.dumps(sign_document(payload, self.secret), separators=(",", ":")).encode()).decode()
        request = Request(f"{self.origin}/api/update/host/{purpose}", headers={"X-Update-Host-Proof": proof})
        with self.opener.open(request, timeout=8) as response:
            if response.status != 200:
                raise UpdateError("HOST_PROBE_FAILED", "应用健康检查尚未通过")
            raw = response.read(65537)
            if len(raw) > 65536:
                raise UpdateError("HOST_PROBE_FAILED", "应用健康检查响应异常")
            data = json.loads(raw)
        if not isinstance(data, dict):
            raise UpdateError("HOST_PROBE_FAILED", "应用健康检查响应异常")
        return data


class NoHostRedirect(HTTPRedirectHandler):
    """A signed local proof must never leave the configured loopback origin."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HostUpdater:
    def __init__(self, root: Path, data_dir: Path, backup_dir: Path, *, mode: str,
                 secret: str, runner=None, probe=None, drain_timeout: int = 900,
                 health_timeout: int = 240, sleep=time.sleep):
        self.root = root.resolve()
        self.data_dir = data_dir.resolve()
        self.backup_dir = backup_dir.resolve()
        if self.data_dir == self.root or self.backup_dir == self.root or self.data_dir == self.backup_dir:
            raise UpdateError("INVALID_RUNTIME_PATH", "更新运行目录与备份目录必须独立")
        # Runtime and backups are local-only. Restricting them to root/data also
        # prevents signed backup paths ever becoming an arbitrary-write channel.
        if not self.data_dir.is_relative_to(self.root / "data") or not self.backup_dir.is_relative_to(self.root / "data"):
            raise UpdateError("INVALID_RUNTIME_PATH", "更新运行目录与备份目录须位于项目 data 内")
        if self.data_dir.is_relative_to(self.backup_dir) or self.backup_dir.is_relative_to(self.data_dir):
            raise UpdateError("INVALID_RUNTIME_PATH", "更新与备份目录不能互相包含")
        if mode not in {"native", "compose"} or not isinstance(secret, str) or len(secret) < 32:
            raise UpdateError("HOST_CONFIGURATION", "宿主机更新器配置不完整")
        reject_links(self.root / "data", self.root)
        self.mode, self.secret = mode, secret
        self.drain_timeout, self.health_timeout, self.sleep = drain_timeout, health_timeout, sleep
        self.active_run_id = None
        self.phase = "idle"
        self.started_at = utcnow()
        for name in ("jobs", "results", "packages", "journal", "staging", "envs", "backups"):
            reject_links(self.data_dir / name, self.root)
            private_directory(self.data_dir / name)
        private_directory(self.backup_dir)
        self.runner = runner or CommandRunner(self.heartbeat)
        self.probe = probe or HostProbe("http://127.0.0.1:28671/api/health", secret)

    def heartbeat(self) -> None:
        write_signed_json(self.data_dir / "host.json", {
            "protocol": 1, "at": utcnow(), "started_at": self.started_at,
            "pid": os.getpid(), "mode": self.mode,
            "active_run_id": self.active_run_id, "phase": self.phase,
        }, self.secret)

    def result(self, job: dict, status: str, step: str, percentage: int, message: str, **extra) -> dict:
        value = {"protocol": 1, "run_id": job["run_id"], "status": status,
                 "step": step, "progress": {"percentage": percentage}, "message": message,
                 "target_version": job["target_version"], "version": job["target_version"],
                 "updated_at": utcnow(), **extra}
        if status in TERMINAL_STATES:
            value["finished_at"] = utcnow()
            value["completed_at"] = value["finished_at"]
        write_signed_json(self.data_dir / "results" / f"{job['run_id']}.json", value, self.secret)
        self.phase = step
        self.heartbeat()
        return value

    def journal_path(self, run_id: int) -> Path:
        return self.data_dir / "journal" / f"{run_id}.json"

    def journal(self, job: dict, phase: str, snapshot: dict, **extra) -> dict:
        value = {"protocol": 1, "run_id": job["run_id"], "job": job, "phase": phase,
                 "snapshot": snapshot, "updated_at": utcnow(), **extra}
        write_signed_json(self.journal_path(job["run_id"]), value, self.secret)
        return value

    def validate_job(self, job: dict, filename: str) -> dict:
        if not isinstance(job, dict) or type(job.get("run_id")) is not int or job["run_id"] < 1 or filename != f"{job['run_id']}.json":
            raise UpdateError("INVALID_HOST_JOB", "更新任务编号无效")
        if job.get("action") not in {"apply", "rollback"} or type(job.get("auto_restart")) is not bool:
            raise UpdateError("INVALID_HOST_JOB", "更新任务类型无效")
        for name in ("current_version", "target_version"):
            strict_semver(job.get(name, ""))
        if job.get("protocol", 1) != 1:
            raise UpdateError("INVALID_HOST_JOB", "更新任务协议不兼容")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", job.get("repository", "")):
            raise UpdateError("INVALID_HOST_JOB", "更新仓库无效")
        if job["action"] == "apply" and job.get("tag") != "v" + job["target_version"]:
            raise UpdateError("INVALID_HOST_JOB", "更新版本标签无效")
        if job["action"] == "apply":
            path = validate_relative_path(job.get("package", ""))
            if len(path.parts) != 3 or path.parts[0] != "packages" or path.parts[1] != str(job["run_id"]) or not path.name.endswith(".tar.gz"):
                raise UpdateError("INVALID_HOST_JOB", "更新包位置无效")
            if not SHA256.fullmatch(job.get("sha256", "")):
                raise UpdateError("INVALID_HOST_JOB", "更新包校验值无效")
            if compare_versions(job["target_version"], job["current_version"]) <= 0:
                raise UpdateError("VERSION_DOWNGRADE", "更新必须使用更高正式版本")
        elif not SAFE_ID.fullmatch(job.get("backup_id", "")):
            raise UpdateError("INVALID_HOST_JOB", "回滚备份编号无效")
        return job

    def _maintain(self, job: dict) -> None:
        write_signed_json(self.data_dir / "maintenance.json", {"protocol": 1, "run_id": job["run_id"], "at": utcnow()}, self.secret)

    def _clear_maintenance(self, job: dict) -> None:
        path = self.data_dir / "maintenance.json"
        if path.is_file():
            marker = read_signed_json(path, self.secret)
            if marker.get("run_id") != job["run_id"]:
                raise UpdateError("MAINTENANCE_CONFLICT", "其他升级任务持有维护状态")
            path.unlink()

    def _wait_drain(self, job: dict) -> None:
        self._maintain(job)
        deadline = time.monotonic() + self.drain_timeout
        while time.monotonic() < deadline:
            self.heartbeat()
            try:
                state = self.probe("drain", job["run_id"])
                if type(state.get("running")) is int and state["running"] == 0 and state.get("ready") is True:
                    return
            except Exception:
                pass
            self.sleep(2)
        raise UpdateError("COLLECTION_DRAIN_TIMEOUT", "现有采集或 AI 请求未在维护时间内结束，升级尚未应用")

    def _app_services(self, directory: Path | None = None) -> tuple[str, ...]:
        """Old releases/backups have four services; newer ones include AI.

        Discover from the version being prepared or activated, rather than
        requiring an AI service from a pre-feature rollback target.
        """
        directory = directory or self.root
        if self.mode == 'native':
            path = directory / 'scripts/native.ps1'
            source = path.read_text(encoding='utf-8') if path.is_file() else ''
            has_ai = "Start-Owned 'ai-worker'" in source
        else:
            path = directory / 'docker-compose.yml'
            if not path.is_file():
                return APP_SERVICES
            try:
                document = yaml.safe_load(path.read_text(encoding='utf-8'))
                if not isinstance(document, dict) or not isinstance(document.get('services'), dict):
                    raise ValueError
                has_ai = 'ai-worker' in document['services']
            except (ValueError, yaml.YAMLError):
                raise UpdateError('INVALID_COMPOSE_SERVICES', '部署文件中的应用服务定义无效') from None
        return AI_APP_SERVICES if has_ai else APP_SERVICES

    def _healthy(self, job: dict, version: str) -> bool:
        try:
            health = self.probe("health", job["run_id"])
            required = ('worker', 'beat', 'database', 'redis')
            if 'ai-worker' in self._app_services():
                required += ('ai_worker',)
            return (health.get("status") == "ok" and health.get("version") == version
                    and all(health.get(key) is True for key in required)
                    and self._services_healthy())
        except Exception:
            return False

    def _services_healthy(self) -> bool:
        """Check actual managed processes/containers, not a stale Redis timestamp."""
        services = self._app_services()
        if self.mode == "native":
            path = str(self.root / 'scripts/native.ps1').replace("'", "''")
            command = f"$ErrorActionPreference='Stop'; $WarningPreference='SilentlyContinue'; & '{path}' -Action status | ConvertTo-Json -Compress"
            output = self.runner(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                 cwd=self.root, timeout=30)
            rows = json.loads(output)
            if not isinstance(rows, list):
                return False
            by_service = {row.get('service'): row for row in rows if isinstance(row, dict)}
            return all(by_service.get(name, {}).get('running') is True
                       and type(by_service[name].get('pid')) is int and by_service[name]['pid'] > 0
                       for name in services)
        output = self.runner(self._compose(self.root) + ["ps", "--all", "--format", "json", *services],
                             cwd=self.root, timeout=30)
        try:
            rows = json.loads(output)
            if isinstance(rows, dict):
                rows = [rows]
        except json.JSONDecodeError:
            rows = [json.loads(line) for line in output.splitlines() if line.strip()]
        if not isinstance(rows, list) or len(rows) != len(services):
            return False
        return {row.get('Service') for row in rows if isinstance(row, dict)} == set(services) and all(
            isinstance(row, dict) and row.get('State') == 'running' and row.get('Health') == 'healthy'
            for row in rows)

    def _wait_health(self, job: dict, version: str) -> None:
        deadline = time.monotonic() + self.health_timeout
        while time.monotonic() < deadline:
            self.heartbeat()
            if self._healthy(job, version):
                return
            self.sleep(3)
        raise UpdateError("APPLICATION_HEALTH_FAILED", "目标版本或应用服务健康检查未通过")

    def _activation(self) -> dict:
        path = self.data_dir / "activation.json"
        if not path.is_file():
            return {"python": ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"}
        activation = read_signed_json(path, self.secret)
        self._validate_python(activation.get("python", ""))
        return activation

    def _validate_python(self, relative: str) -> Path:
        path = safe_child(self.root, relative)
        relative_parts = validate_relative_path(relative).parts
        if not (relative_parts[0] == ".venv" or path.is_relative_to(self.data_dir / "envs")):
            raise UpdateError("INVALID_ACTIVATION", "应用 Python 路径不在受管虚拟环境内")
        return path

    def _compose(self, directory: Path, override: Path | None = None) -> list[str]:
        command = ["docker", "compose", "--project-directory", str(directory), "--env-file", str(self.root / ".env"), "-f", str(directory / "docker-compose.yml")]
        if override:
            command.extend(["-f", str(override)])
        return command

    def _image_override(self, run_id: int, images: dict[str, str], suffix: str) -> Path:
        if set(images) not in (set(APP_SERVICES), set(AI_APP_SERVICES)) or not all(re.fullmatch(r"[a-z0-9][a-z0-9_.:/@-]{0,199}", value) for value in images.values()):
            raise UpdateError("INVALID_IMAGE_REFERENCE", "应用镜像引用无效")
        path = self.data_dir / "journal" / f"{run_id}-{suffix}.json"
        atomic_write(path, json.dumps({"services": {name: {"image": image} for name, image in images.items()}}))
        return path

    def _current_images(self, job: dict) -> dict[str, str]:
        images = {}
        for service in self._app_services():
            containers = self.runner(self._compose(self.root) + ["ps", "-q", service], cwd=self.root, timeout=30).splitlines()
            if len(containers) != 1 or not re.fullmatch(r"[a-f0-9]{12,64}", containers[0]):
                raise UpdateError("COMPOSE_SERVICE_MISSING", "应用容器未就绪，升级尚未应用")
            image_id = self.runner(["docker", "inspect", "--format", "{{.Image}}", containers[0]], cwd=self.root, timeout=30)
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
                raise UpdateError("INVALID_IMAGE_REFERENCE", "当前应用镜像标识异常")
            tag = f"price-monitor-backup-{job['run_id']}-{service}:saved"
            self.runner(["docker", "tag", image_id, tag], cwd=self.root, timeout=30)
            images[service] = tag
        return images

    def _prepare_native(self, job: dict, stage: Path) -> dict:
        if os.name != "nt":
            raise UpdateError("NATIVE_PLATFORM_UNSUPPORTED", "本机原生升级当前使用 Windows，Linux 请使用 compose 模式")
        environment = self.data_dir / "envs" / f"{job['target_version']}-{job['run_id']}"
        if environment.exists():
            raise UpdateError("STAGING_CONFLICT", "目标虚拟环境已存在，需核对中断的升级任务")
        self.runner([sys.executable, "-m", "venv", str(environment)], cwd=self.root, timeout=180)
        python = environment / "Scripts/python.exe"
        self.runner([str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(stage / "backend/requirements.txt")], cwd=self.root, timeout=1800)
        npm = shutil.which("npm.cmd") or shutil.which("npm")
        if not npm:
            raise UpdateError("COMMAND_UNAVAILABLE", "本机升级需要 Node.js 与 npm")
        self.runner([npm, "ci", "--prefix", str(stage / "frontend"), "--no-audit", "--no-fund"], cwd=self.root, timeout=1200)
        return {"python": python.relative_to(self.root).as_posix()}

    def _prepare_compose(self, job: dict, stage: Path) -> dict:
        services = self._app_services(stage)
        images = {service: f"price-monitor-update-{job['run_id']}-{service}:prepared" for service in services}
        override = self._image_override(job["run_id"], images, "target-images")
        self.runner(self._compose(stage, override) + ["build", *services], cwd=self.root, timeout=2400)
        return {"images": images}

    def _snapshot(self, job: dict, target_files: set[str]) -> dict:
        backup_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{job['run_id']}-{uuid.uuid4().hex[:8]}"
        destination = self.backup_dir / backup_id
        private_directory(destination / "files")
        manifest_path = self.root / "release-manifest.json"
        # An installed checkout also contains preserved runtime files. The strict
        # package validator deliberately rejects those, so use its installed
        # file list as a boundary and hash actual files into our own signed
        # backup instead of validating the entire live directory as an archive.
        old_files = set(collect_files(self.root, require_dist=False))
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("product") != "price-monitor" or not isinstance(manifest.get("files"), dict):
                raise UpdateError("INVALID_INSTALLED_MANIFEST", "当前部署的文件清单无效")
            old_files.update(manifest["files"])
            old_files.add("release-manifest.json")
        managed = old_files | target_files | {"release-manifest.json"}
        if not all(managed_path(name) for name in managed):
            raise UpdateError("UNMANAGED_RELEASE_PATH", "发布包包含非应用文件")
        hashes, absent = {}, []
        for name in sorted(managed):
            source = safe_child(self.root, name)
            if source.is_file():
                content = source.read_bytes()
                output = safe_child(destination / "files", name)
                output.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(output, content)
                hashes[name] = hashlib.sha256(content).hexdigest()
            elif source.exists():
                raise UpdateError("UNMANAGED_RELEASE_PATH", "应用文件位置被目录占用")
            else:
                absent.append(name)
        metadata = {"protocol": 1, "backup_id": backup_id, "version": job["current_version"],
                    "created_at": utcnow(), "mode": self.mode, "files": hashes,
                    "absent": absent, "activation": self._activation() if self.mode == "native" else {},
                    "images": self._current_images(job) if self.mode == "compose" else {},
                    "node_modules_saved": False}
        write_signed_json(self.data_dir / "backups" / f"{backup_id}.json", metadata, self.secret)
        return metadata

    def _backup(self, backup_id: str, version: str | None = None) -> dict:
        if not SAFE_ID.fullmatch(backup_id):
            raise UpdateError("INVALID_BACKUP", "回滚备份编号无效")
        backup = read_signed_json(self.data_dir / "backups" / f"{backup_id}.json", self.secret)
        if backup.get("backup_id") != backup_id or backup.get("mode") != self.mode or (version and backup.get("version") != version):
            raise UpdateError("INVALID_BACKUP", "回滚备份与目标版本不匹配")
        strict_semver(backup.get("version", ""))
        for name, expected_hash in backup.get("files", {}).items():
            if not managed_path(name) or not SHA256.fullmatch(expected_hash):
                raise UpdateError("INVALID_BACKUP", "备份清单无效")
            source = safe_child(self.backup_dir / backup_id / "files", name)
            if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != expected_hash:
                raise UpdateError("BACKUP_HASH_MISMATCH", "旧版本备份校验失败")
        if not all(managed_path(name) for name in backup.get("absent", [])):
            raise UpdateError("INVALID_BACKUP", "备份清单无效")
        return backup

    def _stop(self) -> None:
        if self.mode == "native":
            self.runner(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(self.root / "scripts/native.ps1"), "-Action", "stop-app"], cwd=self.root, timeout=180)
        else:
            services = self._app_services()
            order = [name for name in ('beat', 'ai-worker', 'worker', 'api', 'web') if name in services]
            self.runner(self._compose(self.root) + ["stop", "--timeout", "150", *order], cwd=self.root, timeout=210)

    def _start(self, job: dict, activation: dict) -> None:
        if self.mode == "native":
            python = self._validate_python(activation["python"])
            self.runner(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(self.root / "scripts/native.ps1"), "-Action", "restart-app", "-PythonExe", str(python)], cwd=self.root, timeout=240)
        else:
            services = self._app_services()
            if set(activation['images']) != set(services):
                raise UpdateError('INVALID_IMAGE_REFERENCE', '镜像备份与该版本的应用服务不匹配')
            override = self._image_override(job["run_id"], activation["images"], "activate-images")
            self.runner(self._compose(self.root, override) + ["up", "-d", "--no-build", "--no-deps", "--wait", "--wait-timeout", "240", *services], cwd=self.root, timeout=300)

    def _replace_files(self, source_root: Path, files: set[str], *, delete: set[str] = frozenset()) -> None:
        for name in sorted(files):
            if not managed_path(name):
                raise UpdateError("UNMANAGED_RELEASE_PATH", "发布包包含非应用文件")
            source, destination = safe_child(source_root, name), safe_child(self.root, name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(destination, source.read_bytes())
            if name.endswith(".sh") and os.name != "nt":
                destination.chmod(0o755)
        for name in sorted(delete - files):
            if not managed_path(name):
                raise UpdateError("UNMANAGED_RELEASE_PATH", "发布包包含非应用文件")
            destination = safe_child(self.root, name)
            if destination.is_file():
                destination.unlink()
            elif destination.exists():
                raise UpdateError("UNMANAGED_RELEASE_PATH", "待清理文件位置被目录占用")

    def _save_node_modules(self, snapshot: dict) -> None:
        current = self.root / "frontend/node_modules"
        destination = self.backup_dir / snapshot["backup_id"] / "node_modules"
        reject_links(current, self.root)
        if current.exists():
            if destination.exists():
                raise UpdateError("BACKUP_CONFLICT", "前端依赖备份位置已存在")
            snapshot["node_modules_saved"] = True
            write_signed_json(self.data_dir / "backups" / f"{snapshot['backup_id']}.json", snapshot, self.secret)
            # Persist the intention before the atomic directory rename. Recovery
            # also accepts the pre-rename state if the process dies in between.
            current.rename(destination)

    def _restore(self, job: dict, snapshot: dict) -> None:
        snapshot = self._backup(snapshot["backup_id"])
        self._replace_files(self.backup_dir / snapshot["backup_id"] / "files", set(snapshot["files"]), delete=set(snapshot["absent"]))
        if self.mode == "native":
            stored = self.backup_dir / snapshot["backup_id"] / "node_modules"
            current = self.root / "frontend/node_modules"
            if snapshot.get("node_modules_saved") and stored.is_dir():
                discard = self.data_dir / "staging" / f"discard-nodes-{job['run_id']}-{uuid.uuid4().hex[:8]}"
                if current.exists():
                    reject_links(current, self.root)
                    current.rename(discard)
                stored.rename(current)
                # Retain a reusable copy for explicit future rollback. Files are
                # copied only from the verified local dependency directory.
                shutil.copytree(current, stored, symlinks=True)
            elif snapshot.get("node_modules_saved") and not current.is_dir():
                raise UpdateError("BACKUP_DEPENDENCIES_MISSING", "旧版本前端依赖备份缺失")
            write_signed_json(self.data_dir / "activation.json", snapshot["activation"], self.secret)

    def _recover_previous(self, job: dict, snapshot: dict, *, message: str) -> dict:
        self.result(job, "rolling_back", "rollback", 90, "升级未通过验证，正在恢复旧版本", backup_id=snapshot["backup_id"], backup_version=snapshot["version"])
        self._stop()
        self._restore(job, snapshot)
        self._start(job, snapshot["activation"] if self.mode == "native" else {"images": snapshot["images"]})
        self._wait_health(job, snapshot["version"])
        return self._complete(job, snapshot, "rolled_back", message, snapshot["version"])

    def _finish(self, job: dict, snapshot: dict, activation: dict) -> dict:
        if self.mode == "native":
            write_signed_json(self.data_dir / "activation.json", activation, self.secret)
        final_status = "rolled_back" if job["action"] == "rollback" else "succeeded"
        return self._complete(job, snapshot, final_status, "指定版本已启动且全部健康检查通过", job["target_version"])

    def _complete(self, job: dict, snapshot: dict, status: str, message: str, version: str) -> dict:
        # Record verified completion before dropping the recovery journal. A
        # crash at any later instruction may only finish cleanup, never apply
        # this job again or mark the already healthy version as failed.
        completion = {"status": status, "message": message, "version": version}
        self.journal(job, "completed", snapshot, completion=completion)
        return self._publish_completion(job, snapshot, completion)

    def _publish_completion(self, job: dict, snapshot: dict, completion: dict) -> dict:
        status, version = completion.get('status'), completion.get('version')
        if status not in {'succeeded', 'rolled_back'}:
            raise UpdateError('INVALID_COMPLETION', '更新终态记录无效')
        strict_semver(version)
        if status == 'succeeded' and (job['action'] != 'apply' or version != job['target_version']):
            raise UpdateError('INVALID_COMPLETION', '更新终态版本与任务不一致')
        if status == 'rolled_back' and version not in {job['current_version'], job['target_version']}:
            raise UpdateError('INVALID_COMPLETION', '回滚终态版本与任务不一致')
        output = self.result(job, status, "complete", 100, "指定版本已启动且全部健康检查通过",
                             backup_id=snapshot["backup_id"], backup_version=snapshot["version"], version=version)
        self._clear_maintenance(job)
        self.journal_path(job["run_id"]).unlink(missing_ok=True)
        return output

    def _finished_result(self, job: dict) -> dict | None:
        path = self.data_dir / 'results' / f"{job['run_id']}.json"
        if not path.is_file():
            return None
        output = read_signed_json(path, self.secret)
        if output.get('run_id') != job['run_id'] or output.get('target_version') != job['target_version']:
            return None
        state, version = output.get('status'), output.get('version')
        if state == 'succeeded' and job['action'] == 'apply' and version == job['target_version']:
            return output
        if state == 'rolled_back' and version in {job['current_version'], job['target_version']}:
            return output
        return None

    def _resume(self, record: dict) -> dict:
        job, snapshot = record["job"], record["snapshot"]
        self.validate_job(job, f"{job['run_id']}.json")
        self.active_run_id = job["run_id"]
        finished = self._finished_result(job)
        if finished:
            self._clear_maintenance(job)
            self.journal_path(job['run_id']).unlink(missing_ok=True)
            return finished
        if record['phase'] == 'completed':
            return self._publish_completion(job, snapshot, record['completion'])
        if record["phase"] == "applied" and not job["auto_restart"]:
            if self._healthy(job, job["target_version"]):
                return self._finish(job, snapshot, record["activation"])
            return self.result(job, "applied", "manual_restart", 85, "文件已应用，等待管理员手动启动目标版本", backup_id=snapshot["backup_id"], backup_version=snapshot["version"])
        if record["phase"] == "applied":
            try:
                self._start(job, record["activation"])
                self._wait_health(job, job["target_version"])
                return self._finish(job, snapshot, record["activation"])
            except Exception:
                return self._recover_previous(job, snapshot, message="中断的升级未通过验证，旧版本已恢复")
        return self._recover_previous(job, snapshot, message="更新器中断后已恢复原版本，未重放文件替换")

    def process_job(self, job: dict) -> dict:
        job = self.validate_job(job, f"{job['run_id']}.json")
        self.active_run_id = job["run_id"]
        snapshot = None
        mutation_started = False
        try:
            if version_from_source(self.root) != job["current_version"]:
                raise UpdateError("CURRENT_VERSION_CHANGED", "当前版本已变化，请重新检测更新")
            self.result(job, "applying", "validate", 5, "正在校验本机更新任务与发布包")
            if job["action"] == "apply":
                package = safe_child(self.data_dir, job["package"])
                if not package.is_file() or hashlib.sha256(package.read_bytes()).hexdigest() != job["sha256"]:
                    raise UpdateError("PACKAGE_HASH_MISMATCH", "更新包 SHA256 校验失败")
                stage = self.data_dir / "staging" / f"{job['run_id']}-{uuid.uuid4().hex[:8]}"
                manifest = safe_extract_tar(package, stage)
                validate_manifest(stage, expected_version=job["target_version"])
                if not all(managed_path(name) for name in manifest["files"]):
                    raise UpdateError("UNMANAGED_RELEASE_PATH", "发布包包含非应用文件")
                self.result(job, "applying", "dependencies", 20, "正在为目标版本准备独立依赖")
                activation = self._prepare_native(job, stage) if self.mode == "native" else self._prepare_compose(job, stage)
                target_files = set(manifest["files"]) | {"release-manifest.json"}
                source_root = stage
            else:
                target_backup = self._backup(job["backup_id"], job["target_version"])
                target_files = set(target_backup["files"])
                source_root = self.backup_dir / target_backup["backup_id"] / "files"
                activation = target_backup["activation"] if self.mode == "native" else {"images": target_backup["images"]}
            self.result(job, "applying", "drain", 40, "正在等待运行中的采集完成")
            self._wait_drain(job)
            self.result(job, "applying", "backup", 50, "正在备份当前版本")
            snapshot = self._snapshot(job, target_files)
            self.journal(job, "prepared", snapshot, activation=activation)
            self.result(job, "applying", "replace", 65, "正在应用目标版本", backup_id=snapshot["backup_id"], backup_version=snapshot["version"])
            mutation_started = True
            self._stop()
            self.journal(job, "mutating", snapshot, activation=activation)
            if self.mode == "native":
                self._save_node_modules(snapshot)
                self.journal(job, "mutating", snapshot, activation=activation)
            previous_files = set(snapshot["files"])
            self._replace_files(source_root, target_files, delete=previous_files - target_files)
            if self.mode == "native":
                target_nodes = (stage / "frontend/node_modules") if job["action"] == "apply" else (self.backup_dir / target_backup["backup_id"] / "node_modules")
                if not target_nodes.is_dir():
                    raise UpdateError("FRONTEND_DEPENDENCIES_MISSING", "目标版本前端依赖缺失")
                if job["action"] == "apply":
                    target_nodes.rename(self.root / "frontend/node_modules")
                else:
                    shutil.copytree(target_nodes, self.root / "frontend/node_modules", symlinks=True)
                write_signed_json(self.data_dir / "activation.json", activation, self.secret)
            self.journal(job, "applied", snapshot, activation=activation)
            if not job["auto_restart"]:
                return self.result(job, "applied", "manual_restart", 85, "文件已应用，等待管理员手动启动目标版本", backup_id=snapshot["backup_id"], backup_version=snapshot["version"])
            self.result(job, "restarting", "restart", 85, "正在启动并检查目标版本", backup_id=snapshot["backup_id"], backup_version=snapshot["version"])
            self._start(job, activation)
            self._wait_health(job, job["target_version"])
            return self._finish(job, snapshot, activation)
        except Exception as exc:
            if snapshot and mutation_started:
                try:
                    return self._recover_previous(job, snapshot, message="目标版本未通过验证，已自动恢复旧版本")
                except Exception:
                    # The signed journal and maintenance marker are intentionally
                    # retained so a subsequent daemon run can recover safely.
                    return self.result(job, "failed", "recovery_required", 90, "自动恢复未通过健康检查，需要管理员处理", error_code="ROLLBACK_HEALTH_FAILED", backup_id=snapshot["backup_id"], backup_version=snapshot["version"])
            self._clear_maintenance(job)
            if snapshot:
                self.journal_path(job["run_id"]).unlink(missing_ok=True)
            code = exc.code if isinstance(exc, UpdateError) else "HOST_UPDATE_FAILED"
            message = exc.message if isinstance(exc, UpdateError) else "宿主机更新步骤失败，当前版本尚未替换"
            return self.result(job, "failed", self.phase, 0, message, error_code=code)
        finally:
            self.active_run_id = None
            self.phase = "idle"
            self.heartbeat()

    def run_once(self) -> dict | None:
        self.heartbeat()
        journals = sorted((self.data_dir / "journal").glob("[0-9]*.json"))
        for path in journals:
            if not re.fullmatch(r"[1-9][0-9]*\.json", path.name):
                continue
            reject_links(path, self.root)
            record = read_signed_json(path, self.secret)
            try:
                return self._resume(record)
            finally:
                self.active_run_id = None
                self.phase = "idle"
                self.heartbeat()
        for path in sorted((self.data_dir / "jobs").glob("[0-9]*.json")):
            try:
                reject_links(path, self.root)
                job = self.validate_job(read_signed_json(path, self.secret), path.name)
            except (UpdateError, OSError, ValueError, TypeError):
                # Bad queue input must not stop the daemon or starve valid
                # later jobs. Do not trust its payload or echo its contents.
                rejected = self.data_dir / 'rejected_jobs'
                reject_links(rejected, self.root)
                private_directory(rejected)
                path.rename(rejected / (path.name + '.' + uuid.uuid4().hex))
                if re.fullmatch(r'[1-9][0-9]{0,17}\.json', path.name):
                    write_signed_json(self.data_dir / 'results' / path.name,
                                      {'protocol': 1, 'run_id': int(path.stem), 'status': 'failed',
                                       'updated_at': utcnow(), 'error_code': 'INVALID_HOST_JOB',
                                       'message': '更新任务签名或内容无效，已隔离'}, self.secret)
                continue
            output = self.data_dir / "results" / path.name
            if output.is_file():
                previous = read_signed_json(output, self.secret)
                if previous.get('run_id') == job['run_id'] and previous.get('status') in TERMINAL_STATES:
                    if previous.get('error_code') == 'INVALID_HOST_JOB' or previous.get('target_version') == job['target_version']:
                        continue
            return self.process_job(job)
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=SCRIPTS.parent)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--mode", choices=("native", "compose"), default="native" if os.name == "nt" else "compose")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument("--health-url", default="http://127.0.0.1:28671/api/health")
    parser.add_argument("--drain-timeout", type=int, default=900)
    parser.add_argument("--health-timeout", type=int, default=240)
    args = parser.parse_args()
    if not 1 <= args.poll_seconds <= 300 or not 5 <= args.drain_timeout <= 3600 or not 5 <= args.health_timeout <= 900:
        parser.error("Polling and timeout values are outside supported limits")
    values = read_env(args.root / ".env")
    secret = os.environ.get("SESSION_SECRET") or values.get("SESSION_SECRET", "")
    try:
        try:
            from app.update_config import load_update_config
        except ModuleNotFoundError:
            raise UpdateError('HOST_CONFIG_DEPENDENCY', '宿主机更新器需要安装配置读取依赖 PyYAML') from None
        settings = load_update_config(root=args.root, environ={**values, **os.environ})
        loaded_code = code_stamp(args.root)
        updater = HostUpdater(args.root, args.data_dir or settings.runtime_dir, args.backup_dir or settings.backup_dir,
                              mode=args.mode, secret=secret, probe=HostProbe(args.health_url, secret),
                              drain_timeout=args.drain_timeout, health_timeout=args.health_timeout)
        with process_lock(updater.data_dir / "host.lock"):
            while True:
                result = updater.run_once()
                if args.once:
                    return 1 if result and result.get("status") == "failed" else 0
                if (result and result.get('status') in {'succeeded', 'rolled_back'}
                        and not unfinished_journal(updater.data_dir)
                        and code_stamp(args.root) != loaded_code):
                    break
                time.sleep(args.poll_seconds)
        # Release the OS lock before exec: the new interpreter must acquire it
        # normally and observe terminal job results instead of replaying work.
        script = str(args.root.resolve() / 'scripts/update_host.py')
        os.execv(sys.executable, [sys.executable, '-u', script, *sys.argv[1:]])
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        # Deliberately omit exception text: subprocess/proxy/path failures can
        # contain local paths or credentials even when the job has no secrets.
        code = exc.code if isinstance(exc, UpdateError) else "HOST_DAEMON_FAILED"
        print(json.dumps({"status": "failed", "error_code": code}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
