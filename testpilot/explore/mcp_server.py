"""模拟器/驱动接口的 MCP server（设计 11.1）— 把设备控制能力暴露给外部 agent。

协议：JSON-RPC 2.0 over stdio，覆盖 MCP 基础子集：
``initialize`` / ``notifications/initialized`` / ``ping`` / ``tools/list`` / ``tools/call``。
接口语义与带外 WebSocket 主通道**完全一致**（同一 ``ToolBox`` / 同一模拟器适配器），
MCP 只是给 agent 的另一种接入形态，不改变执行语义。

启动：``testpilot explore mcp``（stdio 常驻，由 agent 进程拉起）。
"""
import json
import sys

from testpilot.core.errors import TestPilotError

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "testpilot-emulator"
SERVER_VERSION = "0.1.0"


class McpServer:
    """MCP 基础子集实现（与工具箱解耦，单测可直接喂内存流）。"""

    def __init__(self, toolbox, *, name: str = SERVER_NAME,
                 version: str = SERVER_VERSION):
        self.toolbox = toolbox
        self.name = name
        self.version = version

    def handle(self, message: dict):
        """处理一条 JSON-RPC 消息；通知类返回 None（不回包）。"""
        method = message.get("method")
        msg_id = message.get("id")

        if method == "initialize":
            return _ok(msg_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": self.name, "version": self.version},
            })
        if method in ("notifications/initialized", "initialized"):
            return None                       # 通知：按 MCP 规范不回包
        if method == "ping":
            return _ok(msg_id, {})
        if method == "tools/list":
            return _ok(msg_id, {"tools": self.toolbox.list_tools()})
        if method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            try:
                result = self.toolbox.invoke(name, params.get("arguments") or {})
            except TestPilotError as e:
                # 工具失败按 MCP 约定走 isError 结果（而不是 JSON-RPC 错误），
                # agent 才能把它当作"一次可观察的失败"继续探索
                return _ok(msg_id, {
                    "content": [{"type": "text", "text": str(e)}],
                    "isError": True})
            except Exception as e:
                # 畸形 arguments 等意外异常同样按 isError 结果返回：stdio server
                # 是常驻进程，一次非法调用不能让整个探索会话退出
                return _ok(msg_id, {
                    "content": [{"type": "text",
                                 "text": f"{type(e).__name__}: {e}"}],
                    "isError": True})
            return _ok(msg_id, {
                "content": [{"type": "text",
                             "text": json.dumps(result, ensure_ascii=False,
                                                default=str)}],
                "isError": False})
        if msg_id is None:
            return None                       # 其余通知静默忽略
        return _err(msg_id, -32601, f"method not found: {method}")

    def serve_stdio(self, reader, writer) -> int:
        """逐行读取 JSON-RPC 消息直至 EOF（stdio 传输约定）。"""
        for line in reader:
            text = line.strip() if isinstance(line, str) else line.decode(
                "utf-8", "replace").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                writer.write(json.dumps(_err(None, -32700, "parse error")) + "\n")
                writer.flush()
                continue
            if not isinstance(message, dict):
                # 非对象帧（数组/标量）按协议错误回包，而不是让 server 崩溃退出
                writer.write(json.dumps(_err(None, -32600,
                                             "请求必须是对象")) + "\n")
                writer.flush()
                continue
            response = self.handle(message)
            if response is not None:
                writer.write(json.dumps(response, ensure_ascii=False) + "\n")
                writer.flush()
        return 0


def build_server(cfg: dict | None = None) -> McpServer:
    """按配置装配模拟器工具箱（stdio server 的默认形态）。"""
    from testpilot.config.settings import load_settings
    from testpilot.drivers.emulator_adapter import EmulatorAdapter
    from testpilot.explore.toolbox import ToolBox

    cfg = cfg or load_settings()
    emulator = EmulatorAdapter(cfg["emulator"])
    return McpServer(ToolBox({}, emulator=emulator))


def _ok(msg_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message}}


def main(argv=None) -> int:
    """stdio 入口：``python -m testpilot.explore.mcp_server`` / CLI ``explore mcp``。"""
    from testpilot.infra.logsys import setup_logging
    from testpilot.config.settings import load_settings

    cfg = load_settings()
    # 结构化日志走 stderr（stdout 是 MCP 协议通道，绝不能被日志污染）
    setup_logging(cfg.get("log", {}))
    server = build_server(cfg)
    return server.serve_stdio(sys.stdin, sys.stdout)


if __name__ == "__main__":  # pragma: no cover - 进程入口
    sys.exit(main())
