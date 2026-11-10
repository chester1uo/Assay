"""Each API app has an independent MCP HTTP lifecycle over the shared tools."""
import json
from fastapi.testclient import TestClient
from assay.api.app import create_app


def test_api_instances_have_independent_mcp_http_lifespans(monkeypatch):
    monkeypatch.setenv('ASSAY_MCP_HTTP', '1')
    monkeypatch.delenv('ASSAY_API_KEYS', raising=False)
    monkeypatch.delenv('ASSAY_ADMIN_KEYS', raising=False)
    monkeypatch.delenv('ASSAY_ACCOUNTS', raising=False)

    def tools(client):
        response = client.post('/mcp/', headers={'Accept': 'application/json, text/event-stream'},
            json={'jsonrpc':'2.0', 'id':1, 'method':'tools/list', 'params':{}})
        assert response.status_code == 200, response.text
        payload = (response.json() if response.headers.get('content-type','').startswith('application/json')
                   else json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith('data: '))))
        return {t['name'] for t in payload['result']['tools']}

    with TestClient(create_app()) as first:
        initial = tools(first)
        with TestClient(create_app()) as second:
            assert tools(second) == initial
            assert tools(first) == initial
        assert tools(first) == initial
    with TestClient(create_app()) as restarted:
        assert tools(restarted) == initial
    assert len(initial) == 39
    assert {'fdb_submit', 'assay_experiment_run', 'assay_evaluate', 'assay_experiment_cancel'} <= initial
