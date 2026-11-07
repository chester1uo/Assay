"""Optional reference client: a model chooses tools discovered from an MCP server.

Run ``python -m assay.agent --help``. The server owns research state; this client
owns the conversation and bounded execution, and can be replaced by other agents.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

RESEARCH_TOOLS = {
    'assay_backend_list', 'assay_dataset_register', 'assay_dataset_list',
    'assay_research_session_create', 'assay_research_session_list',
    'assay_research_context', 'assay_research_note',
    'assay_experiment_run', 'assay_experiment_get', 'assay_experiment_compare',
    'assay_experiment_replay', 'assay_lint',
}


def normalize_tool_result(result):
    """Decode a single JSON text result as well as structured MCP objects.

    Preserve non-JSON, mixed, and multi-block content without guessing how to
    combine it. Raw transport results remain in the execution receipt.
    """
    if not isinstance(result, dict):
        return result
    if isinstance(result.get('structuredContent'), dict):
        return result['structuredContent']
    content = result.get('content')
    if (isinstance(content, list) and len(content) == 1
            and isinstance(content[0], dict) and content[0].get('type') == 'text'):
        try:
            value = json.loads(content[0]['text'])
        except (ValueError, KeyError, TypeError):
            return result
        if isinstance(value, dict):
            return value
    return result


def model_tool_result(name, result):
    """Summarize routine evaluation feedback; keep full reports in receipts/server.

    experiment_get remains an explicit full-report inspection tool. No numeric
    score is recomputed here, and scope/failure metadata remain visible.
    """
    result = normalize_tool_result(result)
    if name not in {'assay_experiment_run', 'assay_experiment_replay'} or not isinstance(result, dict):
        return result
    if 'id' not in result or 'status' not in result:
        return result
    summary = {k: v for k, v in result.items() if k not in {'report', 'panel_report'}}
    report = result.get('report')
    if isinstance(report, dict):
        fields = ('failure_mode', 'suggestion', 'diagnostics', 'rank_ic', 'rank_icir', 'ic', 'icir',
                  'turnover_1d', 'decay_halflife_days', 'ic_by_horizon', 'n_dates', 'n_symbols',
                  'eval_period', 'panel_period', 'score_period', 'scoring_complete', 'universe_id',
                  'universe_as_of', 'universe_symbols_hash', 'data_as_of', 'execution', 'adj',
                  'diagnostics_scope', 'unavailable_score_metrics')
        summary['report'] = {k: report[k] for k in fields if k in report}
    else:
        summary['report'] = report
    summary['report_detail'] = 'summary; call assay_experiment_get with this ID for the full saved report'
    return summary


def atomic_json(path: Path, payload):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


class ProviderError(RuntimeError):
    def __init__(self, message, receipt):
        super().__init__(message)
        self.receipt = receipt


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ChatModel:
    """Minimal Chat Completions-compatible transport; no implicit retries."""

    def __init__(self, base_url, api_key, model, *, temperature=.7, max_tokens=4096,
                 timeout=120, extra_body=None, allowed_returned_models=None):
        parts = urlsplit(base_url)
        if parts.scheme not in ('https', 'http') or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError('base_url must be an HTTP(S) origin/path without credentials or query')
        self.url = base_url.rstrip('/') + '/chat/completions'
        self.api_key, self.model = api_key, model
        self.temperature, self.max_tokens, self.timeout = temperature, max_tokens, timeout
        self.allowed_returned_models = list(allowed_returned_models or [model])
        self.last_receipt = None
        self.extra_body = dict(extra_body or {})
        if set(self.extra_body) & {'model', 'messages', 'tools', 'tool_choice', 'temperature', 'max_tokens', 'stream'}:
            raise ValueError('extra_body cannot override conversation or execution settings')

    def configuration(self):
        config = dict(endpoint=self.url, model=self.model, temperature=self.temperature,
                      max_tokens=self.max_tokens, timeout=self.timeout, extra_body=self.extra_body,
                      allowed_returned_models=self.allowed_returned_models, automatic_retries=0)
        return json.loads(json.dumps(config).replace(self.api_key, '[redacted]')) if self.api_key else config

    def complete(self, messages, tools):
        self.last_receipt = None
        body = dict(model=self.model, messages=messages, tools=tools, tool_choice='auto',
                    temperature=self.temperature, max_tokens=self.max_tokens, stream=False, **self.extra_body)
        request = Request(self.url, data=json.dumps(body).encode(), headers={
            'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.api_key,
        })
        try:
            with build_opener(NoRedirect()).open(request, timeout=self.timeout) as response:
                raw, status = response.read().decode('utf-8', errors='replace'), response.status
        except HTTPError as exc:
            raw = exc.read().decode('utf-8', errors='replace')
            self.last_receipt = dict(http_status=exc.code, response_text=raw.replace(self.api_key, '[redacted]') if self.api_key else raw)
            raise ProviderError(f'provider HTTP {exc.code}; no automatic retry or redirect', self.last_receipt) from None
        self.last_receipt = dict(http_status=status, response_text=raw.replace(self.api_key, '[redacted]') if self.api_key else raw)
        try:
            return json.loads(raw)
        except ValueError:
            raise ProviderError('provider returned invalid JSON', self.last_receipt) from None


async def run_agent(session, model, *, task: str, output: Path, max_turns=8,
                    max_tool_calls=16, system_prompt=None, allowed_tools=None):
    """Run a bounded autonomous tool conversation and retain every received response.

    A final textual response is recorded as model_finished, not task success.
    Objective completion must be checked against the saved research records.
    """
    if type(max_turns) is not int or max_turns < 1 or type(max_tool_calls) is not int or max_tool_calls < 1:
        raise ValueError('positive integer turn and tool-call limits are required')
    if not task.strip():
        raise ValueError('task must not be empty')
    output.mkdir(parents=True, exist_ok=False)
    ledger = dict(schema_version=1, status='initializing', max_turns=max_turns,
                  max_tool_calls=max_tool_calls, provider_calls=[], tool_calls=[],
                  usage={'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0},
                  usage_complete=True, final_text=None)
    ledger['model_config'] = model.configuration() if hasattr(model, 'configuration') else None
    save = lambda: atomic_json(output / 'run.json', ledger)
    save()
    messages = [dict(role='system', content=system_prompt or (
        'You are a quantitative research assistant using Assay MCP tools. Discover the environment, '
        'inspect prior research, propose explicit hypotheses, evaluate candidates and use feedback to '
        'choose the next step. Retain failures and record decisions. Respect dataset settings and budgets. '
        'Use stable request IDs for experiment retries. Never describe evaluation completion as proof of '
        'profitability. End with a concise evidence-linked report of what you actually observed.')),
        dict(role='user', content=task)]
    try:
        available = []
        cursor = None
        while True:
            page = await session.list_tools(cursor=cursor)
            available.extend(page.tools)
            cursor = page.nextCursor
            if not cursor:
                break
        allowed = RESEARCH_TOOLS if allowed_tools is None else set(allowed_tools)
        definitions = {t.name: t for t in available if t.name in allowed}
        if not definitions:
            raise ValueError('server has no allowed research tools')
        schemas = [dict(type='function', function=dict(name=t.name, description=t.description or '',
                   parameters=t.inputSchema)) for t in definitions.values()]
        atomic_json(output / 'tools.json', schemas)
        ledger['tool_schema_hash'] = hashlib.sha256(json.dumps(schemas, sort_keys=True).encode()).hexdigest()
        ledger['status'] = 'running'
        for turn in range(max_turns):
            atomic_json(output / 'conversation.json', messages)
            call = dict(turn=turn, status='started', request_hash=hashlib.sha256(
                json.dumps(dict(messages=messages, tools=schemas, model_config=ledger['model_config']), sort_keys=True).encode()).hexdigest(),
                usage_complete=False)
            ledger['provider_calls'].append(call)
            ledger['usage_complete'] = False
            save()
            started = time.perf_counter()
            try:
                response = await asyncio.to_thread(model.complete, messages, schemas)
            except BaseException as exc:
                call.update(status='interrupted' if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)) else 'failed',
                            elapsed_seconds=time.perf_counter() - started,
                            receipt=getattr(exc, 'receipt', None))
                raise
            call.update(status='response_received', elapsed_seconds=time.perf_counter() - started,
                        response=response, receipt=getattr(model, 'last_receipt', None))
            save()  # retain malformed/truncated responses before interpreting them
            if not isinstance(response, dict):
                raise ValueError('provider response must be an object')
            usage = response.get('usage') or {}
            call['usage_complete'] = isinstance(usage, dict) and all(
                type(usage.get(k)) is int and usage[k] >= 0 for k in ledger['usage'])
            if not isinstance(usage, dict):
                usage = {}
            for key in ledger['usage']:
                value = usage.get(key)
                if type(value) is int and value >= 0:
                    ledger['usage'][key] += value
            ledger['usage_complete'] = all(c['usage_complete'] for c in ledger['provider_calls'])
            accepted = getattr(model, 'allowed_returned_models', None)
            if accepted and response.get('model') not in accepted:
                raise ValueError('provider returned an unaccepted model identity')
            if not response.get('choices'):
                raise ValueError('provider returned no choices')
            choice = response['choices'][0]
            if choice.get('finish_reason') in ('length', 'content_filter'):
                raise ValueError('provider response incomplete: ' + choice['finish_reason'])
            message = choice['message']
            if not isinstance(message, dict):
                raise ValueError('provider message must be an object')
            # Some compatible providers require reasoning_content on tool continuations.
            assistant = {k: message[k] for k in ('content', 'tool_calls', 'reasoning_content') if k in message}
            assistant['role'] = 'assistant'
            tool_calls = assistant.get('tool_calls') or []
            if not isinstance(tool_calls, list):
                raise ValueError('tool_calls must be a list')
            if not tool_calls:
                if not isinstance(assistant.get('content'), str) or not assistant['content'].strip():
                    raise ValueError('provider returned neither tool calls nor final text')
                messages.append(assistant)
                ledger.update(status='model_finished', final_text=assistant['content'])
                break
            ids = [c.get('id') for c in tool_calls if isinstance(c, dict)]
            if len(ids) != len(tool_calls) or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
                raise ValueError('invalid or duplicate tool-call IDs')
            messages.append(assistant)
            for tool in tool_calls:
                if len(ledger['tool_calls']) >= max_tool_calls:
                    ledger['status'] = 'tool_budget_exhausted'
                    break
                function = tool.get('function') or {}
                name = function.get('name')
                entry = dict(turn=turn, id=tool['id'], name=name, arguments=function.get('arguments'), status='started')
                ledger['tool_calls'].append(entry)
                save()
                try:
                    if name not in definitions:
                        raise ValueError('tool is not in the discovered allowlist')
                    args = json.loads(function.get('arguments', ''))
                    if not isinstance(args, dict):
                        raise ValueError('tool arguments must be an object')
                except (ValueError, TypeError) as exc:
                    result = dict(error=str(exc))
                    entry['status'] = 'rejected'
                else:
                    returned = await session.call_tool(name, args)
                    entry['isError'] = bool(returned.isError)
                    if hasattr(returned, 'model_dump'):
                        entry['transport_result'] = returned.model_dump(mode='json')
                    result = returned.structuredContent
                    if result is None:
                        result = {'content': [c.model_dump(mode='json') for c in returned.content]}
                    entry['status'] = 'error' if returned.isError else 'completed'
                entry['result'] = result
                presented = model_tool_result(name, result)
                if entry.get('isError'):
                    presented = {'isError': True, 'result': presented}
                entry['model_result'] = presented
                messages.append(dict(role='tool', tool_call_id=tool['id'], content=json.dumps(presented)))
                save()
            if ledger['status'] == 'tool_budget_exhausted':
                break
        else:
            ledger['status'] = 'turn_budget_exhausted'
    except BaseException as exc:
        ledger.update(status='interrupted' if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)) else 'failed',
                      error={'type': type(exc).__name__, 'message': str(exc)})
        raise
    finally:
        atomic_json(output / 'conversation.json', messages)
        save()
    return ledger


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--api-key-env', default='ASSAY_AGENT_API_KEY')
    parser.add_argument('--task-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='New directory for receipts and conversation')
    parser.add_argument('--max-turns', type=int, default=8)
    parser.add_argument('--max-tool-calls', type=int, default=16)
    parser.add_argument('--max-tokens', type=int, default=4096)
    parser.add_argument('--temperature', type=float, default=.7)
    parser.add_argument('--extra-body', type=json.loads, default={})
    parser.add_argument('--allow-returned-model', action='append')
    parser.add_argument('--server-command', nargs=argparse.REMAINDER, required=True)
    args = parser.parse_args()
    if not args.server_command:
        parser.error('server command is empty')
    key = os.environ.get(args.api_key_env)
    if not key:
        parser.error('API key environment variable is unset')
    model = ChatModel(args.base_url, key, args.model, temperature=args.temperature,
                      max_tokens=args.max_tokens, extra_body=args.extra_body,
                      allowed_returned_models=args.allow_returned_model)

    async def execute():
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        params = StdioServerParameters(command=args.server_command[0], args=args.server_command[1:], env=dict(os.environ))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await run_agent(session, model, task=args.task_file.read_text(), output=args.output,
                                       max_turns=args.max_turns, max_tool_calls=args.max_tool_calls)
    result = asyncio.run(execute())
    print(json.dumps({k: result[k] for k in ('status', 'usage', 'usage_complete', 'final_text')}, indent=2))


if __name__ == '__main__':
    main()
