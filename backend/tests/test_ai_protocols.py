"""Pure protocol fixtures: no imports of app DB/config, no network or model calls."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.ai_protocols import build_request, parse_response, request_headers, ProtocolResponseError


SCHEMA = {'type':'object','properties':{'summary':{'type':'string'}},'required':['summary'],'additionalProperties':False}
KEY = 'fixture-protocol-secret'


def connection(**changes):
    return SimpleNamespace(**{'base_url':'https://gateway.example.org/v1','model':'fixture-model',
        'key':KEY,'json_mode':True,'api_format':'chat_completions','reasoning_effort':'auto',
        'max_output_tokens':4000,**changes})


def test_chat_default_auto_keeps_backward_compatible_shape_and_never_puts_key_in_body():
    path,headers,body=build_request(connection(),'fixture-input','system',SCHEMA)
    assert path=='/chat/completions'
    assert headers['Authorization']=='Bearer '+KEY and 'x-api-key' not in headers
    assert body=={'model':'fixture-model','messages':[{'role':'system','content':'system'},
        {'role':'user','content':'fixture-input'}],'max_tokens':4000,'response_format':{'type':'json_object'}}
    assert KEY not in str(body)


@pytest.mark.parametrize('model',['gpt-5.4','gpt-6.1-sol','o3','o4-mini'])
def test_modern_chat_models_receive_completion_token_field(model):
    _,_,body=build_request(connection(model=model,reasoning_effort='high',max_output_tokens=8192),'p','s',SCHEMA)
    assert body['max_completion_tokens']==8192 and 'max_tokens' not in body
    assert body['reasoning_effort']=='high'


def test_responses_request_uses_native_input_reasoning_and_no_storage():
    path,headers,body=build_request(connection(api_format='responses',reasoning_effort='xhigh'),'p','s',SCHEMA)
    assert path=='/responses' and headers['Authorization']=='Bearer '+KEY
    assert body=={'model':'fixture-model','instructions':'s','input':[{'role':'user','content':'p'}],
                 'store':False,'max_output_tokens':4000,'reasoning':{'effort':'xhigh'},
                 'text':{'format':{'type':'json_schema','name':'price_data_report','strict':True,'schema':SCHEMA}}}
    _,_,plain=build_request(connection(api_format='responses',json_mode=False),'p','s',SCHEMA)
    assert 'reasoning' not in plain and 'text' not in plain


def test_anthropic_request_uses_separate_auth_adaptive_thinking_and_schema():
    original=deepcopy(SCHEMA)
    cfg=connection(api_format='anthropic_messages',reasoning_effort='medium')
    path,headers,body=build_request(cfg,'p','s',SCHEMA)
    assert path=='/messages'
    assert headers=={'x-api-key':KEY,'anthropic-version':'2023-06-01','Accept':'application/json'}
    assert 'Authorization' not in headers
    assert body['thinking']=={'type':'adaptive'}
    assert body['output_config']=={'effort':'medium','format':{'type':'json_schema','schema':SCHEMA}}
    assert body['system']=='s' and body['messages']==[{'role':'user','content':'p'}]
    body['output_config']['format']['schema']['required'].append('changed')
    assert SCHEMA==original
    assert request_headers(cfg)==headers
    _,_,plain=build_request(connection(api_format='anthropic_messages',json_mode=False),'p','s',SCHEMA)
    assert 'thinking' not in plain and 'output_config' not in plain


@pytest.mark.parametrize('fields',[{'api_format':'unknown'}, {'reasoning_effort':'bad'},
    {'max_output_tokens':True},{'max_output_tokens':1023},{'max_output_tokens':32769},
    {'api_format':'anthropic_messages','reasoning_effort':'minimal'},
    {'api_format':'anthropic_messages','reasoning_effort':'none'},
    {'api_format':'anthropic_messages','reasoning_effort':'ultra'},
    {'base_url':'https://api.openai.com/v1','reasoning_effort':'ultra'}])
def test_invalid_protocol_options_fail_without_silent_replacement(fields):
    with pytest.raises(ValueError):build_request(connection(**fields),'p','s',SCHEMA)


@pytest.mark.parametrize('api_format',['chat_completions','responses'])
def test_ultra_is_only_an_explicit_custom_gateway_passthrough(api_format):
    _,_,body=build_request(connection(api_format=api_format,reasoning_effort='ultra'),'p','s',SCHEMA)
    assert (body.get('reasoning_effort') or body.get('reasoning',{}).get('effort'))=='ultra'


def test_legacy_connection_object_receives_compatible_defaults():
    cfg=SimpleNamespace(base_url='https://gateway.example.org/v1',model='fixture',key=KEY,json_mode=False)
    path,_,body=build_request(cfg,'p','s',SCHEMA)
    assert path=='/chat/completions' and body['max_tokens']==4000
    assert 'reasoning_effort' not in body and 'response_format' not in body


def response_fixture():
    return {'status':'completed','output':[
        {'type':'reasoning','summary':[{'type':'summary_text','text':'private thinking'}]},
        {'type':'message','role':'assistant','status':'completed',
         'content':[{'type':'output_text','text':'{"summary":'},{'type':'output_text','text':'"okay"}'}]}],
        'usage':{'input_tokens':10,'output_tokens':7,'total_tokens':17,'output_tokens_details':{'reasoning_tokens':5}}}


def test_responses_parser_joins_only_final_answer_and_maps_real_usage():
    text,usage=parse_response(connection(api_format='responses'),response_fixture())
    assert text=='{"summary":"okay"}' and 'thinking' not in text
    assert usage=={'prompt_tokens':10,'completion_tokens':7,'total_tokens':17}


@pytest.mark.parametrize('state',['incomplete','failed','queued','in_progress','cancelled',None])
def test_responses_parser_rejects_noncompleted_status(state):
    raw=response_fixture();raw['status']=state
    with pytest.raises(ProtocolResponseError):parse_response(connection(api_format='responses'),raw)


@pytest.mark.parametrize('change',['tool','refusal','empty','missing_text','bad_message_role','bad_content','error'])
def test_responses_parser_rejects_nonfinal_or_invented_outputs(change):
    raw=response_fixture()
    if change=='tool':raw['output'].append({'type':'function_call','name':'run'})
    elif change=='refusal':raw['output'][1]['content']=[{'type':'refusal','refusal':'declined'}]
    elif change=='empty':raw['output']=raw['output'][:1]
    elif change=='missing_text':raw['output'][1]['content']=[{'type':'output_text'}]
    elif change=='bad_message_role':raw['output'][1]['role']='user'
    elif change=='bad_content':raw['output'][1]['content']='not-array'
    else:raw['error']={'code':'fixture'}
    with pytest.raises(ProtocolResponseError):parse_response(connection(api_format='responses'),raw)


def anthropic_fixture():
    return {'role':'assistant','stop_reason':'end_turn','content':[
        {'type':'thinking','thinking':'private chain'}, {'type':'redacted_thinking','data':'redacted'},
        {'type':'text','text':'{"summary":"okay"}'}], 'usage':{'input_tokens':11,'output_tokens':9}}


def test_anthropic_parser_ignores_thinking_and_does_not_fabricate_total_tokens():
    text,usage=parse_response(connection(api_format='anthropic_messages'),anthropic_fixture())
    assert text=='{"summary":"okay"}'
    assert usage=={'prompt_tokens':11,'completion_tokens':9} and 'total_tokens' not in usage


@pytest.mark.parametrize('stop',['max_tokens','tool_use','pause_turn','refusal','stop_sequence',None])
def test_anthropic_parser_rejects_nonfinal_stop_reason(stop):
    raw=anthropic_fixture();raw['stop_reason']=stop
    with pytest.raises(ProtocolResponseError):parse_response(connection(api_format='anthropic_messages'),raw)


@pytest.mark.parametrize('block',[{'type':'tool_use','input':{}},{'type':'refusal','text':'declined'},
    {'type':'text','text':''},{'type':'thinking','thinking':'no final answer'}])
def test_anthropic_parser_rejects_tools_refusal_and_empty_answer(block):
    raw=anthropic_fixture();raw['content']=[block]
    with pytest.raises(ProtocolResponseError):parse_response(connection(api_format='anthropic_messages'),raw)


def test_chat_parser_backwards_compatibility_and_actual_usage():
    raw={'choices':[{'message':{'content':'{"summary":"okay"}'},'finish_reason':'stop'}],
         'usage':{'prompt_tokens':4,'completion_tokens':3,'total_tokens':7}}
    assert parse_response(connection(),raw)==('{"summary":"okay"}',{'prompt_tokens':4,'completion_tokens':3,'total_tokens':7})
    raw['usage']={'prompt_tokens':True,'completion_tokens':-2,'total_tokens':'7'}
    assert parse_response(connection(),raw)[1] is None


@pytest.mark.parametrize('change',['length','tool','refusal','reasoning_only','wrong_role','multi_choice'])
def test_chat_parser_rejects_incomplete_tools_and_nonanswers(change):
    raw={'choices':[{'message':{'content':'okay'},'finish_reason':'stop'}]}
    message=raw['choices'][0]['message']
    if change=='length':raw['choices'][0]['finish_reason']='length'
    elif change=='tool':message['tool_calls']=[{'id':'call'}]
    elif change=='refusal':message['refusal']='declined'
    elif change=='reasoning_only':message['content']=None;message['reasoning_content']='private'
    elif change=='wrong_role':message['role']='user'
    else:raw['choices'].append(raw['choices'][0])
    with pytest.raises(ProtocolResponseError):parse_response(connection(),raw)
