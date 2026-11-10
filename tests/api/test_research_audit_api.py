"""REST and MCP expose the same read-only judge packet and authentication."""
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from assay.agent import normalize_tool_result
from assay.api.app import create_app
from assay.service import AssayService
from assay.research import ResearchWorkspace


def test_audit_rest_mcp_auth_parity_and_readonly_budget(tmp_path,monkeypatch):
    class Backend:
        def evaluate(self,expr,dataset):
            raise AssertionError('audit must not execute')
    ws=ResearchWorkspace(tmp_path/'state.sqlite3',{'test':Backend()})
    ds=ws.register_dataset('audit','NASDAQ100',['2024-01-02','2024-01-05'],backend='test')
    sid=ws.create_session(ds['id'],'Review evidence',max_experiments=2)['id']
    monkeypatch.setattr(AssayService,'_instance',SimpleNamespace(research=ws))
    monkeypatch.setenv('ASSAY_API_KEYS','audit-test-key')
    monkeypatch.delenv('ASSAY_ACCOUNTS',raising=False)
    path=f'/v1/research/sessions/{sid}/audit'
    before=ws.export_session(sid)
    with TestClient(create_app()) as c:
        assert c.get(path).status_code==401
        headers={'Authorization':'Bearer audit-test-key'}
        result=c.get(path,headers=headers).json()
        assert result==ws.audit_session(sid)
        assert c.get(path+'?limit=101',headers=headers).status_code==422
        assert c.get(path+'?expected_manifest_hash=bad',headers=headers).status_code==422
        response=c.post('/mcp/',headers=dict(headers,Accept='application/json, text/event-stream'),
            json={'jsonrpc':'2.0','id':1,'method':'tools/call',
                  'params':{'name':'assay_research_audit','arguments':{'session_id':sid}}})
        assert response.status_code==200
        payload=response.json() if response.headers['content-type'].startswith('application/json') else json.loads(
            next(line[6:] for line in response.text.splitlines() if line.startswith('data: ')))
        assert normalize_tool_result(payload['result'])==result
    assert ws.export_session(sid)==before
