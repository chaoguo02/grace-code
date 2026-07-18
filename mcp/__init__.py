"""
mcp — Model Context Protocol client implementation.

Architecture (CC-aligned, 4 layers):
  1. Protocol Layer  — JSON-RPC 2.0 request/response
  2. Transport Layer — stdio subprocess + streamable HTTP
  3. Registry Layer  — server config + connection management + tool discovery
  4. Tool Wrapper    — mcp__server__tool registration in ToolRegistry
"""
