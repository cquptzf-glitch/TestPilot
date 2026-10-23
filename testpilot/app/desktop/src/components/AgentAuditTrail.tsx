import type { AgentToolAuditEntry } from '../types';

export function AgentAuditTrail({
  audit,
  title = 'Agent 工具轨迹',
  open = false,
}: {
  audit: AgentToolAuditEntry[];
  title?: string;
  open?: boolean;
}) {
  if (!audit.length) return null;
  return <details className="agent-audit-trail" open={open}>
    <summary>
      <strong>{title}</strong>
      <span className="faint"> · 主 RPC 审计 {audit.length} 次</span>
    </summary>
    <table className="grid compact" style={{ marginTop: 8 }}>
      <thead><tr><th>#</th><th>主进程工具</th><th>输入摘要</th><th>结果摘要</th><th>耗时</th></tr></thead>
      <tbody>{audit.map((entry) => <tr key={`${entry.seq}-${entry.method}`}>
        <td>{entry.seq}</td>
        <td className="mono">{entry.method}</td>
        <td className="mono pre-wrap">{JSON.stringify(entry.params ?? {})}</td>
        <td className="mono pre-wrap">
          {entry.ok ? JSON.stringify(entry.result ?? {}) : '失败：' + (entry.error || 'unknown')}
        </td>
        <td>{entry.elapsed_ms} ms</td>
      </tr>)}</tbody>
    </table>
    <p className="hint">轨迹由 TestPilot 主 RPC 生成，只保留参数/结果摘要，不保存源码与 diff 正文。</p>
  </details>;
}
