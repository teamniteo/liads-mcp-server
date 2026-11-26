"""`linkedin-ads-mcp` runs the MCP server on stdio; `linkedin-ads-mcp login` signs in to LinkedIn."""

from __future__ import annotations

import json
import sys


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["login"]:
        from .auth import login
        from .config import load_config

        print(json.dumps(login(load_config(), open_browser="--no-browser" not in args), indent=2))
        return
    if args[:1] in (["-h"], ["--help"]):
        print(__doc__)
        return
    from .server import mcp

    mcp.run()


if __name__ == "__main__":
    main()
