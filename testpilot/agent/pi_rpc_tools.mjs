/**
 * TestPilot Pi tools.
 *
 * Pi has NO native filesystem/bash/edit/write tools in TestPilot. Every tool below is
 * a thin JSON-RPC proxy to the main TestPilot process. The bearer token is short-lived
 * and server-scoped to agent.* methods + one canonical workspace.
 */
import { Type } from "@sinclair/typebox";

const RPC_URL = process.env.TESTPILOT_AGENT_RPC_URL || "";
const TOKEN = process.env.TESTPILOT_AGENT_RPC_TOKEN || "";
const WORKSPACE = process.env.TESTPILOT_AGENT_WORKSPACE || "";
const ENABLED = new Set(
  (process.env.TESTPILOT_AGENT_TOOLS || "").split(",").map((x) => x.trim()).filter(Boolean),
);

function enabled(name) {
  return ENABLED.size === 0 || ENABLED.has(name);
}

function rpc(method, params = {}) {
  if (!RPC_URL || !TOKEN || !WORKSPACE) {
    return Promise.reject(new Error("TestPilot Agent RPC 环境变量不完整"));
  }
  return new Promise((resolve, reject) => {
    const id = "tool-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
    const WebSocketClient = globalThis.WebSocket;
    if (typeof WebSocketClient !== "function") {
      reject(new Error(
        "Pi runtime has no native WebSocket; use Pi standalone/Bun or Node >=20.10"
      ));
      return;
    }
    const url = new URL(RPC_URL);
    url.searchParams.set("agent_token", TOKEN);
    const ws = new WebSocketClient(url.toString());
    const timer = setTimeout(() => {
      try { ws.close(); } catch {}
      reject(new Error("TestPilot RPC tool timeout"));
    }, 30_000);

    const done = (fn, value) => {
      clearTimeout(timer);
      try { ws.close(); } catch {}
      fn(value);
    };
    ws.addEventListener("open", () => {
      ws.send(JSON.stringify({
        jsonrpc: "2.0", id, method,
        params: { ...params, workspace: WORKSPACE },
      }));
    });
    ws.addEventListener("message", (event) => {
      try {
        const body = JSON.parse(String(event.data));
        if (body.id !== id) return;
        if (body.error) done(reject, new Error(body.error.message || "RPC tool failed"));
        else done(resolve, body.result);
      } catch (error) {
        done(reject, error);
      }
    });
    ws.addEventListener("error", () => done(reject, new Error("TestPilot RPC 连接失败")));
  });
}

function result(value) {
  return {
    content: [{ type: "text", text: JSON.stringify(value, null, 2) }],
    details: value,
  };
}

function register(pi, def) {
  if (!enabled(def.name)) return;
  pi.registerTool({
    ...def,
    async execute(_toolCallId, params) {
      return result(await rpc(def.rpcMethod, params));
    },
  });
}

export default function testPilotRpcTools(pi) {
  register(pi, {
    name: "tp_read_file",
    label: "Read file via TestPilot RPC",
    description: "Read a UTF-8 text file inside the scoped SVN workspace. Never reads files directly.",
    rpcMethod: "agent.fs.read",
    parameters: Type.Object({
      path: Type.String(),
      offset: Type.Optional(Type.Integer({ minimum: 1 })),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 2000 })),
    }),
  });
  register(pi, {
    name: "tp_find_files",
    label: "Find files via TestPilot RPC",
    description: "Find paths inside the scoped workspace using a glob pattern.",
    rpcMethod: "agent.fs.find",
    parameters: Type.Object({
      pattern: Type.String(),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 500 })),
    }),
  });
  register(pi, {
    name: "tp_grep",
    label: "Search text via TestPilot RPC",
    description: "Search source text inside the scoped workspace. Results include path and line number.",
    rpcMethod: "agent.fs.grep",
    parameters: Type.Object({
      query: Type.String(),
      glob: Type.Optional(Type.String()),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 300 })),
      regex: Type.Optional(Type.Boolean()),
    }),
  });
  register(pi, {
    name: "tp_svn_status",
    label: "SVN status via TestPilot RPC",
    description: "Read SVN working-copy status through the TestPilot main process.",
    rpcMethod: "agent.svn.status",
    parameters: Type.Object({}),
  });
  register(pi, {
    name: "tp_svn_diff",
    label: "SVN diff via TestPilot RPC",
    description: "Read bounded SVN diff through the TestPilot main process. If truncated, request specific paths in smaller calls.",
    rpcMethod: "agent.svn.diff",
    parameters: Type.Object({
      from_revision: Type.Optional(Type.String()),
      to_revision: Type.Optional(Type.String()),
      paths: Type.Optional(Type.Array(Type.String(), { maxItems: 50 })),
      max_chars: Type.Optional(Type.Integer({ minimum: 10000, maximum: 200000 })),
    }),
  });
  register(pi, {
    name: "tp_svn_log",
    label: "SVN log via TestPilot RPC",
    description: "Read bounded SVN log metadata through the TestPilot main process.",
    rpcMethod: "agent.svn.log",
    parameters: Type.Object({
      from_revision: Type.Optional(Type.String()),
      to_revision: Type.Optional(Type.String()),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 100 })),
    }),
  });
  register(pi, {
    name: "tp_case_search",
    label: "Search TestPilot cases via RPC",
    description: "Search existing local test cases to avoid duplicates and find regression coverage.",
    rpcMethod: "agent.case.search",
    parameters: Type.Object({
      query: Type.String(),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 50 })),
    }),
  });
  register(pi, {
    name: "tp_case_detail",
    label: "Read TestPilot case via RPC",
    description: "Read one existing local test case and its current DSL/raw source.",
    rpcMethod: "agent.case.detail",
    parameters: Type.Object({
      case_id: Type.String(),
    }),
  });
}
