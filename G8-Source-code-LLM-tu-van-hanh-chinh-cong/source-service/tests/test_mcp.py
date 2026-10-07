import asyncio
import json

from mcp import Client

from app.server import mcp, store


def test_mcp_exposes_three_tools_and_structured_read(monkeypatch, tmp_path):
    corpus = tmp_path / "corpus"
    procedures = corpus / "normalized/procedures.jsonl"
    procedures.parent.mkdir(parents=True)
    procedures.write_text(
        json.dumps(
            {
                "procedure_id": "procedure-alpha",
                "review": {"status": "APPROVED_FOR_DEMO"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("G5_SOURCE_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("G5_CORPUS_PATH", str(corpus))
    monkeypatch.setenv("G5_REVIEW_TOKEN", "review-secret-token")
    store.cache_clear()

    async def run():
        async with Client(mcp, raise_exceptions=True) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "get_procedure_update",
                "lookup_current_procedure",
                "manage_source_update",
            }
            result = await client.call_tool(
                "get_procedure_update",
                {"procedure_id": "procedure-alpha", "field": "fees"},
            )
            assert result.is_error is False
            payload = result.structured_content
            if set(payload) == {"result"}:
                payload = payload["result"]
            assert payload["status"] == "NO_PUBLISHED_UPDATE"

    asyncio.run(run())
    store.cache_clear()


def test_current_tool_returns_structured_disabled_result(monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.delenv("G5_REALTIME_MCP_ENABLED", raising=False)

    async def run():
        async with Client(mcp, raise_exceptions=True) as client:
            result = await client.call_tool(
                "lookup_current_procedure",
                {
                    "procedure_id": "fictional-alpha",
                    "procedure_name": "Alpha",
                    "field": "processing_times",
                    "jurisdiction": "Fictional Ward",
                    "as_of": datetime.now(timezone.utc).date().isoformat(),
                },
            )
            assert not result.is_error
            payload = result.structured_content
            if set(payload) == {"result"}:
                payload = payload["result"]
            assert payload["status"] == "ERROR"
            assert payload["retrieval_note"] == "DVC_REALTIME_DISABLED"
            assert payload["value"] is None
            assert payload["checked_at"]

    asyncio.run(run())
