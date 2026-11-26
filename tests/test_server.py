import asyncio

from fastmcp import Client


def test_errors_reach_the_client_as_tool_errors(monkeypatch):
    monkeypatch.setenv("LIADS_CONFIG", "/nonexistent/config.yaml")
    import liads_mcp.server as server

    monkeypatch.setattr(server, "_service", None)

    async def call(tool):
        async with Client(server.mcp) as c:
            return await c.call_tool(tool, {"account_id": 1} if tool == "list_campaigns" else {}, raise_on_error=False)

    for tool in ("list_plans", "list_campaigns", "health_check"):
        result = asyncio.run(call(tool))
        assert result.is_error, tool
        assert "No such file" in result.content[0].text
