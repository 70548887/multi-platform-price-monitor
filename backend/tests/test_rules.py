"""Pure rule-schema tests: no database, Redis, browser or source HTTP."""

import ast
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from app.collectors import DEFAULT_RULE
from app.collectors import CollectorError
from app.rule_config import RuleConfig, validate_rule_config


def test_default_rule_is_valid_and_all_fields_are_supported():
    config = validate_rule_config(DEFAULT_RULE)
    assert config.page_size == 50
    assert config.session_method == 'GET'
    assert config.api_prefix == '/api'
    assert config.request_content_type == 'application/json'
    assert config.list_extra == {'goods_name': ''}


def test_standard_json_is_default_and_legacy_content_type_is_explicitly_supported():
    assert validate_rule_config({}).request_content_type == 'application/json'
    legacy = 'application/x-www-form-urlencoded;charset=UTF-8;multipart/form-data'
    assert validate_rule_config({'request_content_type': legacy}).request_content_type == legacy


def test_valid_custom_options_remain_configurable():
    config = validate_rule_config({
        'page_field': 'current', 'size_field': 'pageSize', 'category_field': 'category',
        'detail_id_field': 'goods_id', 'page_size': 200, 'start_page': 2, 'max_pages': 10000,
        'timeout_seconds': 1.5, 'max_attempts': 1, 'session_method': 'POST',
        'request_encoding': 'form', 'request_content_type': 'application/json',
        'category_ids': ['12', 13], 'all_category_value': 'all', 'per_category': True,
        'field_mapping': {'price': 'quote.current', 'id': 'product.id'},
        'list_data_path': 'data.products', 'category_data_path': 'result.categories',
        'list_total_path': 'data.total', 'detail_data_path': 'result.product',
        'session_data_path': 'result.account', 'user_agent': 'Mozilla/5.0 UnitBrowser/1.0',
        'allowed_hosts': ['www.xiaohong66.com', 'xiaohong66.com', 'catalogue.vendor.com'],
        'success_codes': [0, 'success'], 'auth_expired_codes': [401, 'expired'],
        'category_extra': {'include': 'all'}, 'list_extra': {'filter': {'name': 'x', 'tags': ['a']}},
        'detail_extra': {'region': 'CN'},
    })
    assert config.page_field == 'current'
    assert config.category_ids == ['12', 13]
    assert config.timeout_seconds == 1.5
    assert config.field_mapping['price'] == 'quote.current'
    assert config.list_extra['filter'] == {'name': 'x', 'tags': ['a']}


@pytest.mark.parametrize('field,values', [
    ('page_size', ['abc', None, [], {}, True, False, 1.5, '50', 0, -1, 201]),
    ('start_page', ['abc', None, [], True, 1.5, '1', 0, -1]),
    ('max_pages', ['abc', None, {}, True, 1.5, '2', 0, -1, 10001]),
    ('max_attempts', ['abc', None, {}, True, 1.5, '2', 0, 4]),
    ('timeout_seconds', ['abc', '30', None, [], {}, True, 0, 60.1, float('nan'), float('inf')]),
])
def test_numeric_fields_return_422_for_wrong_types_or_ranges(field, values):
    for value in values:
        with pytest.raises(HTTPException) as caught:
            validate_rule_config({field: value})
        assert caught.value.status_code == 422


@pytest.mark.parametrize('config', [
    {'category_ids': '12'}, {'category_ids': [True]}, {'category_ids': [None]},
    {'category_ids': ['']}, {'category_ids': [1.5]}, {'category_ids': {}},
    {'all_category_value': True}, {'all_category_value': []}, {'per_category': 'false'},
    {'per_category': 1}, {'success_codes': []}, {'success_codes': '0'},
    {'success_codes': [True]}, {'auth_expired_codes': [None]},
    {'session_method': 'DELETE'}, {'session_method': 'get'},
    {'request_encoding': 'xml'}, {'request_encoding': None},
    {'request_content_type': 'text/plain'}, {'request_content_type': 'application/json\r\nX: x'},
    {'user_agent': ''}, {'user_agent': 'Agent\r\nX-Test: 1'}, {'user_agent': '测试'},
    {'category_extra': None}, {'list_extra': []}, {'detail_extra': 'x'},
])
def test_other_consumed_fields_are_strictly_typed(config):
    with pytest.raises(HTTPException) as caught:
        validate_rule_config(config)
    assert caught.value.status_code == 422


@pytest.mark.parametrize('field,correct', [
    ('category_path', '/customer/Home/GoodsCategoryList'),
    ('list_path', '/customer/Home/GoodsList'),
    ('detail_path', '/customer/Home/GetGoods'),
    ('session_path', '/customer/Profile/Show'),
])
def test_each_endpoint_is_pinned_to_its_own_read_only_route(field, correct):
    assert getattr(validate_rule_config({field: correct}), field) == correct
    wrong = '/customer/Home/GoodsList' if field != 'list_path' else '/customer/Home/GetGoods'
    for value in [wrong, '/customer/../admin', 'https://other.example/read', '//other.example/read', correct + '?x=1', None]:
        with pytest.raises(HTTPException):
            validate_rule_config({field: value})


@pytest.mark.parametrize('prefix', ['../admin', 'api/..', 'api?path=', '//other.example', '/api/', '', None])
def test_api_prefix_cannot_change_the_final_endpoint(prefix):
    with pytest.raises(HTTPException) as caught:
        validate_rule_config({'api_prefix': prefix})
    assert caught.value.status_code == 422


@pytest.mark.parametrize('host', ['127.0.0.1', '::1', 'localhost', 'private.local', 'service.internal',
                                 'https://vendor.com', 'vendor.com:443', 'vendor.com/path', '*.vendor.com',
                                 '.vendor.com', 'bad-.vendor.com', 'x.invalid', '2130706433'])
def test_allowed_hosts_rejects_ips_nonpublic_names_and_url_shapes(host):
    with pytest.raises(HTTPException):
        validate_rule_config({'allowed_hosts': [host]})


@pytest.mark.parametrize('field', ['session_data_path', 'category_data_path', 'list_data_path', 'list_total_path', 'detail_data_path'])
@pytest.mark.parametrize('path', ['', None, [], 'data..value', '.data', 'data.', 'data/value', 'data value'])
def test_object_data_paths_are_nonempty_dotted_strings(field, path):
    with pytest.raises(HTTPException):
        validate_rule_config({field: path})


@pytest.mark.parametrize('mapping', [None, [], {'price': ''}, {'price': None}, {'price': 'data..price'},
                                    {'price': 1}, {'not_supported': 'data.price'}])
def test_field_mapping_only_maps_supported_fields_to_valid_paths(mapping):
    with pytest.raises(HTTPException):
        validate_rule_config({'field_mapping': mapping})


@pytest.mark.parametrize('secret', ['Token', 'Password', 'Authorization', 'COOKIE', 'Cookies', 'Set-Cookie',
                                   'access_token', 'Refresh_Token', 'SECRET', 'api-key', 'clientSecret'])
@pytest.mark.parametrize('field', ['category_extra', 'list_extra', 'detail_extra'])
def test_extra_payload_credentials_are_rejected_recursively_without_echo(field, secret):
    value = 'unit-only-secret-do-not-echo'
    with pytest.raises(HTTPException) as caught:
        validate_rule_config({field: {'filters': [{'nested': {secret: value}}]}})
    assert caught.value.status_code == 422
    assert value not in json.dumps(caught.value.detail)


@pytest.mark.parametrize('field', ['page_field', 'size_field', 'category_field', 'detail_id_field'])
def test_request_field_names_cannot_be_credential_fields(field):
    with pytest.raises(HTTPException):
        validate_rule_config({field: 'Authorization'})


@pytest.mark.parametrize('config', [{'unknown_option': True}, {'Token': 'unit-secret'}, {'headers': {}},
                                  {'max_retries': 3}, {'timeout': 30}, {'code': 'x'}, {'eval': 'x'}])
def test_unknown_legacy_and_executable_fields_are_rejected(config):
    with pytest.raises(HTTPException) as caught:
        validate_rule_config(config)
    assert caught.value.status_code == 422


@pytest.mark.parametrize('value', [None, [], 'rule'])
def test_rule_must_be_an_object(value):
    with pytest.raises(HTTPException) as caught:
        validate_rule_config(value)
    assert caught.value.status_code == 422


def test_rule_size_and_nonfinite_nested_numbers_are_rejected():
    for config in [{'list_extra': {'text': 'x' * 32000}}, {'list_extra': {'number': float('inf')}}]:
        with pytest.raises(HTTPException) as caught:
            validate_rule_config(config)
        assert caught.value.status_code == 422


def test_empty_options_receive_independent_defaults():
    first, second = RuleConfig(), RuleConfig()
    first.list_extra['x'] = 1
    assert second.list_extra == {'goods_name': ''}


def _constructor_failure_endpoint(name):
    """Execute current endpoint code with fakes, without importing app.main."""
    module = ast.parse((Path(__file__).parents[1] / 'app' / 'main.py').read_text(encoding='utf-8'))
    endpoint = next(node for node in module.body if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
    endpoint.decorator_list = []
    endpoint.returns = None
    for argument in endpoint.args.args:
        argument.annotation = None
    endpoint.args.defaults = []

    platform = SimpleNamespace(id=1, active_rule_id=2, base_url='https://www.xiaohong66.com',
                               encrypted_session='unit-only-encrypted', account_fingerprint=None, adapter='xiaohong66')
    rule = SimpleNamespace(platform_id=1, config={}, test_ok=True, tested_at=None)
    state = {'closed': False, 'commits': 0}

    class FakeDB:
        def refresh(self, *args, **kwargs):
            pass

        def get(self, *args):
            return rule

        def commit(self):
            state['commits'] += 1

    @asynccontextmanager
    async def operation(_):
        async def sleep(_):
            pass
        try:
            yield SimpleNamespace(sleep=sleep)
        finally:
            state['closed'] = True

    def constructor(*args, **kwargs):
        raise CollectorError('Invalid source session envelope')

    platform_model, rule_model = object(), object()
    namespace = {
        'HTTPException': HTTPException, 'CollectorError': CollectorError,
        'Xiaohong66Collector': constructor, 'Platform': platform_model, 'Rule': rule_model,
        'require': lambda db, model, identifier: platform if model is platform_model else rule,
        'platform_operation': operation, 'decrypt_session': lambda value: value,
        'validate_rule': validate_rule_config, 'default_rule': lambda: {},
        'require_supported': lambda platform: None,
        'validate_adapter_rule': lambda adapter, value: validate_rule_config(value),
        'adapter_default_rule': lambda adapter: {},
        'utcnow': lambda: datetime.now(timezone.utc), 'hashlib': hashlib,
    }
    exec(compile(ast.Module(body=[endpoint], type_ignores=[]), '<pure-endpoint-test>', 'exec'), namespace)
    return namespace[name], FakeDB(), state


def test_session_constructor_error_returns_400_and_releases_platform_operation():
    endpoint, db, state = _constructor_failure_endpoint('connect_session')
    with pytest.raises(HTTPException) as caught:
        asyncio.run(endpoint(1, SimpleNamespace(token='pm-session-v1:invalid-json'), db, None))
    assert caught.value.status_code == 400
    assert state['closed'] is True


def test_preview_constructor_error_returns_failed_result_and_releases_operation():
    endpoint, db, state = _constructor_failure_endpoint('test_rule')
    result = asyncio.run(endpoint(2, SimpleNamespace(source_product_id=None), db, None))
    assert result == {'ok': False, 'products': [], 'categories': [], 'errors': ['Invalid source session envelope']}
    assert state['closed'] is True
    assert state['commits'] == 1
