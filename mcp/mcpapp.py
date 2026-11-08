"""The shared MCPServer instance.

Kept separate from server.py so tool modules can import it without a circular
import: server.py imports the tool modules, and the tool modules import this.
Named mcpapp rather than mcp so it cannot shadow the installed SDK package.
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer

server = MCPServer(
    name="rag",
    version="1.2.0",
    instructions=(
        "Tools for a local retrieval-augmented generation stack running in "
        "Docker. Query a document corpus, manage collections, ingest documents, "
        "and build gold-standard evaluation sets. Everything runs locally: no "
        "data leaves the machine. Start with rag_health if anything seems wrong."
    ),
)
