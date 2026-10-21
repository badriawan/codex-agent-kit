"""Discover the role-only MCP; --live makes one bounded role-advice request."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tomllib

from mcp import Client, StdioServerParameters

TASK = ("Read the existing four Codex role definition files and report their fixed "
        "models and reasoning efforts with file references. Read-only inspection; no edits.")
ROLES = ["explorer", "researcher", "worker", "architect"]


async def check(live=False, codex_home=None, output=None):
    env = {}
    if live:
        key = os.environ.get("TYPESAFE_API_KEY", "")
        if codex_home:
            config = tomllib.loads((Path(codex_home).expanduser() / "config.toml").read_text())
            key = config.get("mcp_servers", {}).get("jev", {}).get("env", {}).get("TYPESAFE_API_KEY", key)
        if not key:
            raise RuntimeError("missing_api_key")
        env["TYPESAFE_API_KEY"] = key
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).with_name("role_server.py"))],
        env=env,
    )
    async with Client(parameters) as client:
        tools = (await client.list_tools()).tools
        assert [tool.name for tool in tools] == ["route_work"], "Unexpected MCP tools"
        schema = tools[0].input_schema
        assert set(schema["required"]) == {"task_summary", "available_roles"}
        print("MCP tools: route_work (role-only)")
        invalid = await client.call_tool("route_work", {"task_summary": "Read-only inspection", "available_roles": ["unknown"]})
        assert not invalid.is_error, "MCP validation failed"
        data = invalid.structured_content
        assert data["disposition"] == "main" and data["validated"] is False and "agent_type" not in data
        print("Invalid role: returned to primary without API or spawn")
        if live:
            result = await asyncio.wait_for(client.call_tool("route_work", {"task_summary": TASK, "available_roles": ROLES}), timeout=25)
            assert not result.is_error, "MCP role call failed"
            data = result.structured_content
            assert isinstance(data, dict), "Missing structured output"
            if data.get("validated"):
                assert data.get("disposition") == "selected" and data.get("agent_type") in ROLES
                assert data["selected_role"] == data["agent_type"]
            else:
                assert data.get("disposition") == "main" and "agent_type" not in data
            safe = {k: data[k] for k in ["status", "disposition", "validated", "reason", "selected_role", "agent_type", "advisor_model", "elapsed_ms"] if k in data}
            if data.get("decision"):
                safe["confidence"] = data["decision"]["confidence"]
            if output:
                path = Path(output)
                with path.open("w") as stream:
                    json.dump(safe, stream, indent=2)
                    stream.write("\n")
                path.chmod(0o600)
            print(json.dumps(safe))
            if data.get("reason") in {"api_or_configuration_error", "invalid_response", "timeout"}:
                return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Make one synthetic TypeSafe request")
    parser.add_argument("--codex-home", help="Read the existing private Jev credential only for --live")
    parser.add_argument("--output", help="Save the sanitized live result for the primary handoff")
    args = parser.parse_args()
    try:
        code = asyncio.run(check(args.live, args.codex_home, args.output))
    except Exception as exc:
        print(f"Role MCP check failed ({type(exc).__name__}); inspect configuration locally.", file=sys.stderr)
        code = 1
    raise SystemExit(code)
