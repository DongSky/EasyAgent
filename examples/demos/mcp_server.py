import argparse

from mcp.server.fastmcp import FastMCP

parser = argparse.ArgumentParser()
parser.add_argument("--http", action="store_true")
parser.add_argument("--port", type=int, default=8766)
args = parser.parse_args()
server = FastMCP("EasyAgent fixture", host="127.0.0.1", port=args.port)


@server.tool()
def add(a: float, b: float) -> dict:
    """Add two numbers."""
    return {"value": a + b}


server.run(transport="streamable-http" if args.http else "stdio")
