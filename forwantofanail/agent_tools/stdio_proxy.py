from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main() -> None:
    parser = argparse.ArgumentParser(description="Expose a For Want of a Nail HTTP commander session over MCP stdio.")
    parser.add_argument("--url", default=os.getenv("FWOAN_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--token", default=os.getenv("FWOAN_SESSION_TOKEN"))
    args = parser.parse_args()
    if not args.token:
        parser.error("--token or FWOAN_SESSION_TOKEN is required")
    endpoint = args.url.rstrip("/") + "/mcp"
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            envelope = json.loads(line)
            request = Request(
                endpoint,
                data=json.dumps(envelope).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {args.token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "MCP-Protocol-Version": "2026-07-28",
                    "Mcp-Method": str(envelope.get("method") or ""),
                },
                method="POST",
            )
            with urlopen(request, timeout=120) as response:
                output = json.loads(response.read().decode("utf-8"))
        except (ValueError, HTTPError, URLError) as exc:
            output = {
                "jsonrpc": "2.0",
                "id": envelope.get("id") if isinstance(locals().get("envelope"), dict) else None,
                "error": {"code": -32000, "message": str(exc)},
            }
        sys.stdout.write(json.dumps(output, separators=(",", ":")) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
