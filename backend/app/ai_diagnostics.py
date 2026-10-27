"""Saved-connection checks never submit a model inference request."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import time
from urllib.parse import urlsplit, unquote

import httpx

from .ai_analysis import AnalysisError
from .models import utcnow
from .services import iso

MODEL_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}')


def issue_description(code):
    descriptions = {
        'AI_RATE_LIMITED': ('AI 服务端返回限流响应（HTTP 429），分析尚未成功。', '核对服务商额度、并发限制和模型权限，恢复后再手动重试。'),
        'AI_AUTH_FAILED': ('AI 服务端拒绝访问，密钥或模型权限需要核对。', '在 AI 设置中检查已保存连接；模型列表可读取不代表推理额度可用。'),
        'AI_JSON_MODE_UNSUPPORTED': ('AI 接口不接受 JSON 模式参数。', '核对服务商兼容格式，可关闭 JSON 模式后手动重试。'),
        'AI_TOKEN_LIMIT_UNSUPPORTED': ('当前模型不接受输出长度参数。', '核对模型名称和接口兼容格式。'),
        'AI_REASONING_UNSUPPORTED': ('当前模型不接受所选思考等级。', '在 AI 配置中改为自动，或核对服务商声明支持的等级后再重试。'),
        'AI_HTTP_FAILED': ('AI 服务返回错误响应，尚未生成有效分析报告。', '查看批次错误，并检查服务商状态、模型和接口配置。'),
        'AI_NETWORK_FAILED': ('AI 请求连接失败或中断。', '检查已保存的接口和代理线路，恢复后再手动重试。'),
        'AI_ENDPOINT_UNAVAILABLE': ('AI 域名解析失败或地址不符合公开接口要求。', '核对 Base URL 与服务器网络、DNS。'),
        'AI_REPORT_INVALID': ('模型响应格式或商品引用编号未通过校验。', '核对模型是否支持结构化 JSON 输出；原始无效结果未应用。'),
        'AI_REQUEST_FAILED': ('模型请求失败或超时。', '查看批次记录并核对服务商状态，确认后手动重试。'),
        'AI_DAILY_LIMIT': ('本系统今日商品或请求额度已用完。', '等待北京时间次日额度重置，或核对每日额度设置。'),
        'AI_SETTINGS_CHANGED': ('任务执行期间配置发生变化，后续批次未继续发送。', '核对最新配置后重新创建任务。'),
        'AI_TASK_INTERRUPTED': ('AI 工作进程中断，未确认的请求不会自动重发。', '查看已完成批次和实际用量，再决定是否手动重试。'),
        'AI_QUEUE_FAILED': ('AI 工作队列暂不可用。', '检查独立 AI 工作进程与 Redis 后再提交。'),
    }
    return descriptions.get(code, ('AI 任务尚未完整完成。', '查看报告中的各批次说明，核对配置后再操作。'))


async def check_connection(connection, revision, *, transport=None):
    """Bounded GET /models. Models/auth status cannot prove inference availability."""
    started=time.monotonic()
    result={'status':'failed','authenticated':None,'model_available':None,'models':[],
            'models_truncated':False,'latency_ms':0,'checked_at':iso(utcnow()),'config_revision':revision,
            'message':'连接检查未完成','error_code':None}

    def response(status,message,code=None,**fields):
        return {**result,'status':status,'message':message,'error_code':code,
                'latency_ms':int((time.monotonic()-started)*1000),**fields}

    async def request():
        from .ai_protocols import request_headers
        host=httpx.URL(connection.base_url).host
        if transport is None:
            try:
                addresses=await asyncio.get_running_loop().getaddrinfo(host,443,type=socket.SOCK_STREAM)
                if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                    raise ValueError
            except (OSError,ValueError):
                raise AnalysisError('AI_ENDPOINT_UNAVAILABLE','域名未解析为可用的公开地址') from None
        async with httpx.AsyncClient(proxy=connection.proxy,transport=transport,trust_env=False,
                follow_redirects=False,timeout=httpx.Timeout(15,connect=8)) as client:
            async with client.stream('GET',connection.base_url+'/models',
                headers=request_headers(connection)) as remote:
                if remote.status_code in (401,403):
                    return response('failed','接口拒绝访问，请核对密钥、权限和地址。','AI_AUTH_FAILED',authenticated=False)
                if remote.status_code==429:
                    return response('warning','服务端限制了本次连接检查，请稍后再检查。','AI_RATE_LIMITED')
                if remote.status_code in (404,405,501):
                    return response('warning','此接口不提供模型列表，无法通过该检查验证鉴权或模型；请核对服务商说明。','AI_MODELS_UNSUPPORTED')
                if remote.status_code!=200:
                    return response('failed',f'模型列表接口返回 HTTP {remote.status_code}，请检查服务状态。','AI_CONNECTION_HTTP_FAILED')
                content=bytearray()
                async for chunk in remote.aiter_bytes():
                    content.extend(chunk)
                    if len(content)>256*1024:
                        return response('failed','模型列表响应过大，未展示原始内容。','AI_MODELS_INVALID')
        try:
            body=json.loads(content)
            rows=body['data']
            if not isinstance(rows,list) or any(not isinstance(item,dict) for item in rows):
                raise ValueError
        except (ValueError,TypeError,KeyError):
            return response('failed','接口没有返回有效的模型列表，不能据此确认鉴权。','AI_MODELS_INVALID')
        secrets=[connection.key,connection.proxy]
        if connection.proxy:
            proxy=urlsplit(connection.proxy)
            secrets += [unquote(proxy.username or ''),unquote(proxy.password or '')]
        identifiers=[]
        for item in rows:
            identifier=item.get('id')
            if (not isinstance(identifier,str) or not MODEL_ID.fullmatch(identifier)
                    or identifier.lower().startswith(('sk-','github_pat_','ghp_'))
                    or any(secret and secret in identifier for secret in secrets)):
                continue
            if identifier not in identifiers:
                identifiers.append(identifier)
        available=connection.model in identifiers
        return response('connected' if available else 'warning',
            '连接与鉴权检查通过，当前模型出现在列表中；尚未验证推理请求。' if available else
            '连接与鉴权检查通过，但当前模型未出现在列表中，请核对模型名称。',
            None if available else 'AI_MODEL_NOT_LISTED',authenticated=True,model_available=available,
            models=identifiers[:200],models_truncated=len(identifiers)>200)

    try:
        return await asyncio.wait_for(request(),timeout=20)
    except AnalysisError as exc:
        return response('failed',exc.message,exc.code)
    except (httpx.HTTPError,asyncio.TimeoutError,OSError):
        return response('failed','连接检查失败或超时，请核对网络与代理。','AI_CONNECTION_FAILED')
