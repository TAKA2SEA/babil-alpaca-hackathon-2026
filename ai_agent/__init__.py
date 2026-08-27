"""
Stage C / Stage E - AI agent package.

Stage C: the fixed AI Proposal schema (proposal.py) and the Proposal to
mleg_builder mapping (options_strategy_mapper.py).

Stage E: read-only Alpaca MCP integration (mcp_tool_client.py,
real_mcp_transport.py). The AI agent's only handle onto Alpaca is a
ReadOnlyMcpClient, which can call nothing outside the read-only allowlist
- no order path exists from anywhere in this package. Real MCP
connections stay disabled until Stage F (REAL_CONNECTION_READY=False)
and the competition paper account is confirmed.

No order execution, no Alpaca trading client, and no order-mutating tool
is reachable from anywhere in this package.
"""
