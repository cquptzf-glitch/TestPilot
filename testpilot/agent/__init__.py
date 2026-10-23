"""Agent runtime package.

Agent reasoning is deliberately separated from tool execution. Pi (or a future runtime)
may decide which tool to call; every actual read/operation is executed by the TestPilot
RPC main process under a scoped capability token.
"""

from testpilot.agent.pi_runtime import PiAgentRuntime

__all__ = ["PiAgentRuntime"]
