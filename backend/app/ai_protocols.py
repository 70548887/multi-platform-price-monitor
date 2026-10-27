"""Explicit wire adapters; no transport, credentials persistence or automatic fallback.

`ultra` is a gateway extension, not a portable OpenAI effort level. It is
accepted only for a non-OpenAI gateway when explicitly configured by its owner.
"""
from __future__ import annotations

from copy import deepcopy
import re
from urllib.parse import urlsplit


API_FORMATS = frozenset(('chat_completions', 'responses', 'anthropic_messages'))
OPENAI_EFFORTS = frozenset(('auto', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'))
ANTHROPIC_EFFORTS = frozenset(('auto', 'low', 'medium', 'high', 'xhigh', 'max'))


def strict_report_schema(schema):
    """All object fields are required by strict outputs; nullable fields retain null."""
    result=deepcopy(schema)
    def visit(node):
        if isinstance(node,dict):
            node.pop('default',None)
            if node.get('type')=='object':
                node['additionalProperties']=False
                node['required']=list(node.get('properties',{}))
            for value in list(node.values()):visit(value)
        elif isinstance(node,list):
            for item in node:visit(item)
    visit(result)
    return result


class ProtocolResponseError(ValueError):
    """Only fixed messages/codes may cross the protocol boundary."""
    def __init__(self, code='AI_REPORT_INVALID', message='AI 返回的报告格式无效，未应用分析结论'):
        self.code, self.message = code, message
        super().__init__(message)


def _options(connection):
    api_format = getattr(connection, 'api_format', 'chat_completions')
    effort = getattr(connection, 'reasoning_effort', 'auto')
    maximum = getattr(connection, 'max_output_tokens', 4000)
    if api_format not in API_FORMATS:
        raise ValueError('AI 接口类型无效')
    supported = ANTHROPIC_EFFORTS if api_format == 'anthropic_messages' else OPENAI_EFFORTS
    if effort not in supported:
        raise ValueError('此接口不支持所选思考等级')
    if effort == 'ultra' and (urlsplit(connection.base_url).hostname or '').lower().rstrip('.') == 'api.openai.com':
        raise ValueError('OpenAI 官方接口不接受 ultra 扩展等级')
    if type(maximum) is not int or not 1024 <= maximum <= 32768:
        raise ValueError('输出 Token 上限须为 1024 到 32768 的整数')
    return api_format, effort, maximum


def build_request(connection, payload: str, system_prompt: str, report_schema: dict):
    """Return the endpoint suffix, headers, and exactly one inference request."""
    api_format, effort, maximum = _options(connection)
    if not isinstance(payload, str) or not isinstance(system_prompt, str):
        raise ValueError('AI 输入须为文本')
    if api_format == 'anthropic_messages':
        headers = request_headers(connection)
        body = {'model': connection.model, 'system': system_prompt,
                'messages': [{'role': 'user', 'content': payload}], 'max_tokens': maximum}
        output = {}
        if effort != 'auto':
            body['thinking'] = {'type': 'adaptive'}
            output['effort'] = effort
        if connection.json_mode:
            if not isinstance(report_schema, dict) or not report_schema:
                raise ValueError('JSON 输出需要有效报告结构')
            # The configured model/provider must support this schema dialect.
            # Unsupported formats fail explicitly; no paid automatic retry.
            output['format'] = {'type': 'json_schema', 'schema': deepcopy(report_schema)}
        if output:
            body['output_config'] = output
        return '/messages', headers, body
    headers = request_headers(connection)
    if api_format == 'responses':
        body = {'model': connection.model, 'instructions': system_prompt,
                'input': [{'role': 'user', 'content': payload}],
                'store': False, 'max_output_tokens': maximum}
        if effort != 'auto':
            body['reasoning'] = {'effort': effort}
        if connection.json_mode:
            body['text'] = {'format': {'type': 'json_schema','name':'price_data_report',
                                     'strict':True,'schema':strict_report_schema(report_schema)}}
        return '/responses', headers, body
    token_field = 'max_completion_tokens' if re.match(r'^(?:gpt-[5-9](?:\.|-|$)|o[1-9](?:-|$))', connection.model) else 'max_tokens'
    body = {'model': connection.model,
            'messages': [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': payload}],
            token_field: maximum}
    if effort != 'auto':
        body['reasoning_effort'] = effort
    if connection.json_mode:
        body['response_format'] = {'type': 'json_object'}
    return '/chat/completions', headers, body


def request_headers(connection):
    """Use the configured protocol's authentication for inference and models."""
    api_format, _, _ = _options(connection)
    if api_format == 'anthropic_messages':
        return {'x-api-key': connection.key, 'anthropic-version': '2023-06-01', 'Accept': 'application/json'}
    return {'Authorization': 'Bearer ' + connection.key, 'Accept': 'application/json'}


def _usage(raw, names):
    usage = raw.get('usage')
    if not isinstance(usage, dict):
        return None
    result = {target: usage[source] for source, target in names
              if type(usage.get(source)) is int and usage[source] >= 0}
    return result or None


def _require_text(parts):
    if not parts or any(not isinstance(part, str) for part in parts):
        raise ProtocolResponseError()
    text = ''.join(parts)
    if not text.strip():
        raise ProtocolResponseError('AI_REPORT_EMPTY', 'AI 未返回可用的最终文本报告')
    return text


def parse_response(connection, raw: dict):
    """Extract final answer text only. Thinking blocks never become report content."""
    api_format, _, _ = _options(connection)
    if not isinstance(raw, dict):
        raise ProtocolResponseError()
    try:
        if api_format == 'responses':
            if raw.get('status') != 'completed' or raw.get('error') or raw.get('incomplete_details'):
                raise ProtocolResponseError('AI_RESPONSE_INCOMPLETE', 'AI 响应未完整结束，未应用分析结论')
            output = raw['output']
            if not isinstance(output, list):
                raise ProtocolResponseError()
            parts = []
            for item in output:
                if not isinstance(item, dict):
                    raise ProtocolResponseError()
                kind = item.get('type')
                if kind == 'reasoning':
                    continue
                if (kind != 'message' or item.get('role') != 'assistant'
                        or item.get('status', 'completed') != 'completed'):
                    raise ProtocolResponseError('AI_UNEXPECTED_TOOL_RESPONSE', 'AI 返回了工具或非最终消息，未应用分析结论')
                content = item.get('content')
                if not isinstance(content, list):
                    raise ProtocolResponseError()
                for block in content:
                    if not isinstance(block, dict) or block.get('type') != 'output_text':
                        raise ProtocolResponseError('AI_RESPONSE_REFUSED', 'AI 未返回完整文本报告，未应用分析结论')
                    parts.append(block.get('text'))
            return _require_text(parts), _usage(raw, (('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens'), ('total_tokens', 'total_tokens')))
        if api_format == 'anthropic_messages':
            if raw.get('stop_reason') != 'end_turn' or raw.get('role', 'assistant') != 'assistant':
                raise ProtocolResponseError('AI_RESPONSE_INCOMPLETE', 'AI 响应未完整结束或返回工具调用，未应用分析结论')
            content = raw['content']
            if not isinstance(content, list):
                raise ProtocolResponseError()
            parts = []
            for block in content:
                if not isinstance(block, dict):
                    raise ProtocolResponseError()
                kind = block.get('type')
                if kind in ('thinking', 'redacted_thinking'):
                    continue
                if kind != 'text':
                    raise ProtocolResponseError('AI_UNEXPECTED_TOOL_RESPONSE', 'AI 返回了工具或非文本内容，未应用分析结论')
                parts.append(block.get('text'))
            return _require_text(parts), _usage(raw, (('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens'), ('total_tokens', 'total_tokens')))
        choices = raw['choices']
        if not isinstance(choices, list) or len(choices) != 1:
            raise ProtocolResponseError()
        choice = choices[0]
        if not isinstance(choice, dict) or choice.get('finish_reason') not in (None, 'stop'):
            raise ProtocolResponseError('AI_RESPONSE_INCOMPLETE', 'AI 响应被截断或没有正常结束，未应用分析结论')
        message = choice['message']
        if (not isinstance(message, dict) or message.get('role', 'assistant') != 'assistant'
                or message.get('refusal') or message.get('tool_calls') or message.get('function_call')):
            raise ProtocolResponseError('AI_RESPONSE_REFUSED', 'AI 未返回完整文本报告，未应用分析结论')
        return _require_text([message.get('content')]), _usage(raw, (('prompt_tokens', 'prompt_tokens'), ('completion_tokens', 'completion_tokens'), ('total_tokens', 'total_tokens')))
    except ProtocolResponseError:
        raise
    except (KeyError, IndexError, TypeError):
        raise ProtocolResponseError() from None
