"""Isolated Python bridge to the synthetic tool registry (not an OS sandbox)."""
import json
from pathlib import Path
import sys


def main():
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "simulation" / "tools"))
    import registry
    request = json.load(sys.stdin)
    if request.get("operation") == "catalog":
        names = set(request["tools"]) & set(registry.AGENT_TOOLS.get(request["agent"], []))
        tools = registry.openai_tools(sorted(names))
        result = [{"name": t["function"]["name"], "description": t["function"]["description"],
                   "input": t["function"]["parameters"]} for t in tools]
        sys.stdout.write(json.dumps(result))
        return
    if request["tool"] not in registry.AGENT_TOOLS.get(request["agent"], []):
        result = {"error": "tool not authorized for configured simulation identity"}
    else:
        result = registry.call(request["tool"], request["arguments"],
                               registry.Ctx(request["agent"], request["session_id"], db=Path(request["database"])))
    encoded = json.dumps(result)
    if len(encoded.encode()) > 65536:
        encoded = json.dumps({"error": "tool result exceeds response limit; execution may have occurred"})
    sys.stdout.write(encoded)


if __name__ == "__main__":
    main()
