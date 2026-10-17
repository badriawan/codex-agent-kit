"""Verify stdio discovery; --live additionally makes two synthetic API requests."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import Client, StdioServerParameters


async def check(live: bool):
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).with_name("server.py"))],
        env={"TYPESAFE_API_KEY": os.environ.get("TYPESAFE_API_KEY", "")},
    )
    async with Client(parameters) as client:
        tools = (await client.list_tools()).tools
        names = sorted(tool.name for tool in tools)
        assert names == sorted(["route_work", "review_change", "choose_file", "choose_tool",
                                "retry_decision", "choose_action", "pre_tool", "post_tool",
                                "reviewed_retry", "report_outcome", "routing_context"]), names
        print("MCP tools:", ", ".join(names))
        context_result = await client.call_tool("routing_context", {})
        assert not context_result.is_error, "Routing context endpoint failed"
        assert context_result.structured_content.get("grants_action") is False
        assert "session_id" not in context_result.structured_content
        print("Routing context endpoint: available, local, no action granted")
        if live:
            cases = [
                ("route_work", {"task_summary": "Read existing Python source to find where command arguments are parsed. Report file paths only; no edits."}),
                ("review_change", {"plan": "Fix a typo in a documentation comment.",
                                   "change_summary": "One spelling correction in a comment; no executable code changed.",
                                   "validation": "passed", "outside_plan": [], "irreversible": False}),
            ]
            for name, arguments in cases:
                result = await client.call_tool(name, arguments)
                assert not result.is_error, "MCP tool error"
                data = result.structured_content
                assert data and data.get("status") in {"advisory", "uncertain"}, data
                print(json.dumps({"tool": name, "status": data["status"],
                                  "choice": data["decision"]["choice"], "model": data["model"],
                                  "elapsed_ms": data["elapsed_ms"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Make two synthetic TypeSafe API requests")
    args = parser.parse_args()
    asyncio.run(check(args.live))
