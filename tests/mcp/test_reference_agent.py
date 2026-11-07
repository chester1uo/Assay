"""Reference agent execution controls; scripted providers are not LLM evidence."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from assay.agent import run_agent, ChatModel


class Session:
    def __init__(self):
        self.calls = []

    async def list_tools(self, cursor=None):
        return SimpleNamespace(tools=[SimpleNamespace(name='assay_lint', description='Validate an expression',
            inputSchema={'type': 'object', 'properties': {'expr': {'type': 'string'}}, 'required': ['expr']})], nextCursor=None)

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return SimpleNamespace(structuredContent={'ok': args['expr'] == 'close'}, isError=False)


class Model:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append(json.loads(json.dumps(messages)))
        return next(self.responses)


def reply(calls=None, content=None, finish='stop'):
    return {'model': 'scripted-test-only', 'choices': [{'finish_reason': finish,
        'message': {'role': 'assistant', 'content': content, 'tool_calls': calls or []}}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}


def call(expr='close', name='assay_lint', identifier='a'):
    return {'id': identifier, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps({'expr': expr})}}


def test_feedback_is_returned_to_model_and_receipts_are_retained(tmp_path):
    model = Model([reply([call('invalid')]), reply([call('close')]), reply(content='Finished with a valid expression.')])
    session = Session()
    result = asyncio.run(run_agent(session, model, task='Find valid expression', output=tmp_path / 'run'))
    assert result['status'] == 'model_finished'
    assert json.loads(model.requests[1][-1]['content']) == {'ok': False}
    assert json.loads(model.requests[2][-1]['content']) == {'ok': True}
    assert result['usage']['total_tokens'] == 45 and result['usage_complete']
    assert len(session.calls) == 2
    assert json.loads((tmp_path / 'run/run.json').read_text()) == result
    assert result['provider_calls'][0]['response']['model'] == 'scripted-test-only'


def test_tool_budget_caps_multi_call_responses(tmp_path):
    session = Session()
    model = Model([reply([call(identifier='a'), call(identifier='b')])])
    result = asyncio.run(run_agent(session, model, task='test', output=tmp_path / 'run', max_tool_calls=1))
    assert result['status'] == 'tool_budget_exhausted' and len(session.calls) == 1


def test_unknown_tools_are_rejected_without_execution(tmp_path):
    session = Session()
    model = Model([reply([call(name='not_allowed')]), reply(content='Recovered')])
    result = asyncio.run(run_agent(session, model, task='test', output=tmp_path / 'run'))
    assert session.calls == [] and result['tool_calls'][0]['status'] == 'rejected'
    assert 'allowlist' in model.requests[1][-1]['content']


def test_incomplete_response_is_retained_and_not_executed(tmp_path):
    session = Session()
    model = Model([reply([call()], finish='length')])
    with pytest.raises(ValueError, match='incomplete'):
        asyncio.run(run_agent(session, model, task='test', output=tmp_path / 'run'))
    result = json.loads((tmp_path / 'run/run.json').read_text())
    assert result['status'] == 'failed' and len(result['provider_calls']) == 1
    assert result['provider_calls'][0]['response']['choices'][0]['finish_reason'] == 'length'
    assert session.calls == []


def test_turn_budget_and_unknown_usage_are_explicit(tmp_path):
    response = reply([call()])
    response.pop('usage')
    result = asyncio.run(run_agent(Session(), Model([response]), task='test', output=tmp_path / 'run', max_turns=1))
    assert result['status'] == 'turn_budget_exhausted' and not result['usage_complete']


def test_existing_run_directory_cannot_be_overwritten(tmp_path):
    with pytest.raises(FileExistsError):
        asyncio.run(run_agent(Session(), Model([]), task='test', output=tmp_path))


def test_chat_transport_rejects_credential_urls_and_execution_overrides():
    with pytest.raises(ValueError):
        ChatModel('https://user:secret@example.com', 'secret', 'test')
    with pytest.raises(ValueError):
        ChatModel('https://example.com', 'secret', 'test', extra_body={'tools': []})


def test_http_failures_keep_receipts_and_never_follow_redirects(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(self.path)
            self.rfile.read(int(self.headers['Content-Length']))
            if self.path.startswith('/redirect'):
                self.send_response(302)
                self.send_header('Location', '/unexpected')
                self.end_headers()
                self.wfile.write(b'redirect not followed')
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'not-json')

        def do_GET(self):
            seen.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        for route, expected in [('invalid', 200), ('redirect', 302)]:
            model = ChatModel(f'http://127.0.0.1:{server.server_port}/{route}', 'test-secret', 'test-model')
            with pytest.raises(RuntimeError):
                asyncio.run(run_agent(Session(), model, task='test', output=tmp_path / route))
            data = json.loads((tmp_path / route / 'run.json').read_text())
            assert data['provider_calls'][0]['receipt']['http_status'] == expected
            assert data['provider_calls'][0]['receipt']['response_text']
            assert not data['usage_complete']
            assert data['model_config']['model'] == 'test-model'
            assert 'test-secret' not in json.dumps(data)
        assert seen == ['/invalid/chat/completions', '/redirect/chat/completions']
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_no_choices_response_is_saved_with_unknown_usage(tmp_path):
    with pytest.raises(ValueError, match='no choices'):
        asyncio.run(run_agent(Session(), Model([{'choices': []}]), task='test', output=tmp_path / 'run'))
    data = json.loads((tmp_path / 'run/run.json').read_text())
    assert data['provider_calls'][0]['response'] == {'choices': []}
    assert not data['usage_complete']


def test_unaccepted_model_cannot_execute_tools(tmp_path):
    model = Model([reply([call()])])
    model.allowed_returned_models = ['different-model']
    session = Session()
    with pytest.raises(ValueError, match='model identity'):
        asyncio.run(run_agent(session, model, task='test', output=tmp_path / 'run'))
    assert session.calls == []


def test_compact_feedback_preserves_metrics_scope_and_failure_without_series():
    from assay.agent import model_tool_result
    original = {'id': 'exp1', 'status': 'failed', 'expr': 'close', 'report': {
        'rank_ic': None, 'failure_mode': 'INCOMPLETE_SCORE_COVERAGE', 'score_period': ['2024-01-02', '2024-01-05'],
        'scoring_complete': False, 'suggestion': 'Check data', 'rank_ic_series': [1] * 100,
        'diagnostics': {'ok': False}}, 'panel_report': {'rank_ic': .9, 'rank_ic_series': [1] * 200}}
    compact = model_tool_result('assay_experiment_run', original)
    assert compact['report']['rank_ic'] is None
    assert compact['report']['score_period'] == original['report']['score_period']
    assert compact['report']['failure_mode'] == 'INCOMPLETE_SCORE_COVERAGE'
    assert 'rank_ic_series' not in compact['report'] and 'panel_report' not in compact
    assert model_tool_result('assay_experiment_get', original) == original
    assert len(original['report']['rank_ic_series']) == 100


def test_text_json_results_normalize_without_losing_other_content():
    from assay.agent import normalize_tool_result, model_tool_result
    record = {'id': 'exp', 'status': 'completed', 'report': {'rank_ic': .2, 'ic_series': [1, 2]}, 'panel_report': {'large': True}}
    wrapped = {'content': [{'type': 'text', 'text': json.dumps(record)}]}
    compact = model_tool_result('assay_experiment_run', wrapped)
    assert compact['id'] == 'exp' and compact['report']['rank_ic'] == .2
    assert 'ic_series' not in compact['report'] and 'panel_report' not in compact
    assert normalize_tool_result(wrapped) == record
    assert model_tool_result('assay_experiment_get', wrapped) == record
    for other in ({'content': [{'type': 'text', 'text': 'plain'}]},
                  {'content': [{'type': 'text', 'text': '{}'}, {'type': 'image', 'data': 'x'}]},
                  {'content': [None]}):
        assert normalize_tool_result(other) == other


def test_reference_client_normalizes_live_research_mcp_transport(tmp_path):
    import os
    from pathlib import Path
    import sys
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from assay.demo import prepare_demo

    service, dataset = prepare_demo(tmp_path / 'data')
    sid = service.research.create_session(dataset['id'], 'transport regression', max_experiments=1)['id']
    def request(name, args, identifier):
        return {'id': identifier, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}
    model = Model([
        reply([request('assay_research_context', {'session_id': sid}, 'context')]),
        reply([request('assay_experiment_run', {'session_id': sid, 'expr': '-cs_rank(ts_returns(close,5))',
            'hypothesis': 'synthetic test', 'request_id': 'transport'}, 'experiment')]),
        reply(content='Finished transport regression.')])
    async def scenario():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'assay.mcp.server'],
            env=dict(os.environ, ASSAY_DATA_DIR=str(tmp_path / 'data'),
                     PYTHONPATH=str(Path(__file__).resolve().parents[2] / 'src')), cwd=str(tmp_path))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await run_agent(session, model, task='test live MCP feedback', output=tmp_path / 'run')
    result = asyncio.run(scenario())
    context = json.loads(model.requests[1][-1]['content'])
    assert context['session_id'] == sid and 'content' not in context
    feedback = json.loads(model.requests[2][-1]['content'])
    assert feedback['status'] == 'completed' and feedback['id']
    assert 'panel_report' not in feedback and 'ic_series' not in feedback['report']
    assert feedback['report_detail'].startswith('summary;')
    raw = result['tool_calls'][1]['result']
    from assay.agent import normalize_tool_result
    assert normalize_tool_result(raw)['report'] != feedback['report']


@pytest.mark.parametrize('structured', [True, False])
def test_mcp_errors_remain_explicit_in_model_feedback_and_raw_receipts(tmp_path, structured):
    from mcp.types import CallToolResult, TextContent
    class ErrorSession(Session):
        async def call_tool(self, name, args):
            value = {'message': 'unavailable'}
            return CallToolResult(isError=True, structuredContent=value if structured else None,
                content=[TextContent(type='text', text=json.dumps(value))])
    model = Model([reply([call()]), reply(content='Tool failed; stopping.')])
    ledger = asyncio.run(run_agent(ErrorSession(), model, task='test', output=tmp_path / 'run'))
    assert json.loads(model.requests[1][-1]['content']) == {'isError': True, 'result': {'message': 'unavailable'}}
    entry = ledger['tool_calls'][0]
    assert entry['status'] == 'error' and entry['isError']
    assert entry['transport_result']['isError']
