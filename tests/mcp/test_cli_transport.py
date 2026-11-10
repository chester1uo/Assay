"""The public CLI delegates through the real MCP argv entry point."""
from importlib import import_module


def test_serve_mcp_cli_passes_supported_transport_host_and_port(monkeypatch):
    from assay.cli import main
    server=import_module('assay.mcp.server')
    calls=[]
    monkeypatch.setattr(server,'main',lambda argv:calls.append(argv))
    main(['serve-mcp','--transport','http','--host','127.0.0.1','--port','8123'])
    assert calls==[['--transport','http','--host','127.0.0.1','--port','8123']]
