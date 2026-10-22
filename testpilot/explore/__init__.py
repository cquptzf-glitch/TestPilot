"""探索域（M5+ piagent）— 可选上层，不参与确定性回归主链路。

- ``toolbox``：驱动/模拟器 -> agent 工具（能力白名单 + JSON Schema）；
- ``session``：探索留痕 -> DSL 草稿（人审后生效）；
- ``mcp_server``：模拟器接口的 MCP server（stdio），供外部 agent 接入。

即：**agent 负责"发现"，DSL 引擎负责"回归"**（设计 11.1）。
"""
from testpilot.explore.session import ExplorationSession, ExploreStep
from testpilot.explore.toolbox import TOOL_SPECS, ToolBox, ToolError

__all__ = ["ToolBox", "ToolError", "TOOL_SPECS",
           "ExplorationSession", "ExploreStep"]
