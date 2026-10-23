import { useEffect, useState } from 'react';
import { call, notifyCaseJob, type CaseJob, type ParseBatchResult } from '../bridge';
import { AgentAuditTrail } from './AgentAuditTrail';
import { msg, navigate } from '../hooks';
import type { SvnChangeGenerateResult } from '../types';

const labels: Record<CaseJob['status'], string> = {
  queued: '等待中', running: '进行中', completed: '已完成', failed: '失败', cancelled: '已取消',
};
const methods: Record<string, string> = {
  'case.parse': '分析生成 DSL', 'case.pull': '拉取用例', 'case.sync': '同步用例',
  'change.svn.generate': 'SVN 变更生成测试用例',
};

export function CaseJobsPanel({ compact = false }: { compact?: boolean }) {
  const [job, setJob] = useState<CaseJob | null>(null);
  const [error, setError] = useState('');
  const [expanded, setExpanded] = useState(false);
  const [dismissedId, setDismissedId] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    let pending = false;
    let timer: number | undefined;

    const schedule = (active: boolean) => {
      if (!alive) return;
      window.clearTimeout(timer);
      timer = window.setTimeout(
        () => void load(),
        active ? 1000 : 5000,
      );
    };
    const load = async () => {
      if (pending || !alive) return;
      pending = true;
      let active = false;
      try {
        const jobs = await call<CaseJob[]>('job.status');
        if (alive) {
          const current = jobs[0] ?? null;
          active = current?.status === 'queued' || current?.status === 'running';
          setJob(current); setError('');
          if (current) notifyCaseJob(current);
        }
      } catch (e) {
        if (alive) setError(msg(e));
      } finally {
        pending = false;
        schedule(active);
      }
    };
    const onState = () => void load();
    window.addEventListener('testpilot:case-job-state', onState);
    void load();
    return () => {
      alive = false;
      window.clearTimeout(timer);
      window.removeEventListener('testpilot:case-job-state', onState);
    };
  }, []);
  if (!job || job.id === dismissedId || (compact && job.status === 'completed')) return null;
  const active = job.status === 'queued' || job.status === 'running';
  const raw = (job.result ?? job.progress.result) as
    ({ parse?: ParseBatchResult; parsed?: number } & Partial<SvnChangeGenerateResult>) | undefined;
  const changeResult = job.method === 'change.svn.generate'
    ? raw as SvnChangeGenerateResult | undefined : undefined;
  const result = raw?.parsed !== undefined ? raw as ParseBatchResult : raw?.parse ?? null;
  const failures = result?.failed ?? [];
  const items = result?.items ?? [];
  const generated = changeResult?.cases ?? [];
  const agentAudit = changeResult?.agent?.audit ?? job.progress.agent_audit ?? [];
  const continueItems = result?.items ?? [];
  const continueAction = !compact && !active
    ? continueItems.length > 0
      ? {
          label: `继续审核（${continueItems.length}）`,
          run: () => navigate('dsl', {
            caseId: continueItems[0].case_id,
            queue: continueItems.map(i => i.case_id).join(','),
          }),
        }
      : generated.length > 0
        ? {
            label: `查看生成用例（${generated.length}）`,
            run: () => navigate('cases', {
              selection: generated.map(i => i.case_id).join(','),
            }),
          }
        : null
    : null;
  return <div className="case-job-panel">
    <div className={`msg ${job.status === 'failed' || failures.length ? 'warn' : 'info'}`} role="status">
      <strong>{methods[job.method] ?? job.method} · {labels[job.status]}</strong>
      {job.progress.total !== undefined && <span>已处理 {job.progress.completed ?? 0} / {job.progress.total}</span>}
      {active && job.progress.case_id && <span>当前 {job.progress.case_id}</span>}
      {active && job.progress.phase === 'agent_tool' && job.progress.tool &&
        <span>Agent 工具：{job.progress.tool}</span>}
      {active && job.progress.tool_calls !== undefined &&
        <span>工具调用 {job.progress.tool_calls} 次</span>}
      {result && <span>成功 {result.parsed ?? 0} · 失败 {failures.length} · 待处理 {Math.max(0, (result.total ?? 0) - (result.parsed ?? 0) - failures.length)}</span>}
      {changeResult && <span>
        新增 {changeResult.created} · 复用 {changeResult.reused ?? 0} · 去重 {changeResult.duplicates_dropped ?? 0} · Pi 工具 {changeResult.agent?.tool_calls ?? 0} 次
      </span>}
      {job.error && <span>{job.error}</span>}
      <span className="spacer" />
      {active && <button className="btn" onClick={() => {
        void call<CaseJob>('job.cancel', { jobId: job.id })
          .then(setJob).catch((e: unknown) => setError(msg(e)));
      }}>取消后台任务</button>}
      {!active && <button className="btn ghost" onClick={() => {
        setExpanded(false);
        setDismissedId(job.id);
      }}>关闭</button>}
      {continueAction && <button className="btn primary" onClick={continueAction.run}>
        {continueAction.label}
      </button>}
      {!!(items.length || failures.length || generated.length || agentAudit.length) &&
        <button className="btn" onClick={() => setExpanded(!expanded)}>
          {expanded ? '收起详情' : job.status === 'failed' && agentAudit.length
            ? '查看失败轨迹' : '查看详情'}
        </button>}
      {error && <span>{error}</span>}
    </div>
    {expanded && agentAudit.length > 0 && <div className="panel batch-results">
      <AgentAuditTrail
        audit={agentAudit}
        title={job.status === 'failed' || job.status === 'cancelled'
          ? 'Agent 终止前工具轨迹' : 'Agent 工具轨迹'}
        open
      />
      {job.progress.agent_error && <p className="msg warn pre-wrap">
        Agent：{job.progress.agent_error}
      </p>}
    </div>}
    {expanded && result && <div className="panel batch-results">
      <table className="grid"><thead><tr><th>用例</th><th>分析结果</th><th>下一步</th></tr></thead>
        <tbody>{items.map(i => <tr key={i.case_id}><td>{i.case_id}</td>
          <td>已生成 {i.step_count} 步 · 待核 {i.uncertain_steps?.length ?? 0}</td>
          <td><button className="btn" onClick={() => navigate('dsl', { caseId: i.case_id, queue: items.map(x => x.case_id).join(',') })}>审核 DSL</button></td></tr>)}
          {failures.map(i => <tr key={i.case_id}><td>{i.case_id}</td><td className="pre-wrap">{i.error}</td>
            <td><button className="btn" onClick={() => navigate('dsl', { caseId: i.case_id })}>查看并重试</button></td></tr>)}
        </tbody>
      </table>
    </div>}
    {expanded && changeResult && <div className="panel batch-results">
      <h3>SVN 变更生成结果</h3>
      <p className="pre-wrap">{changeResult.summary}</p>
      {!!changeResult.risks?.length && <p className="muted">风险：{changeResult.risks.join('；')}</p>}
      <table className="grid"><thead><tr><th>用例</th><th>优先级 / 类型</th><th>来源文件</th><th>下一步</th></tr></thead>
        <tbody>{generated.map(i => <tr key={i.case_id}>
          <td>{i.case_id}{i.reused && <span className="badge synced" style={{ marginLeft: 6 }}>复用</span>}
            <div className="faint">{i.title}</div>
          </td>
          <td>{i.priority} · {i.category}</td>
          <td className="pre-wrap">{i.source_files.join('\n') || '—'}</td>
          <td><button className="btn" onClick={() => navigate('dsl', { caseId: i.case_id })}>
            {result?.items?.some(x => x.case_id === i.case_id) ? '审核 DSL' : '查看用例'}
          </button></td>
        </tr>)}</tbody>
      </table>
    </div>}
  </div>;
}
