import { useEffect, useState } from 'react';
import { call, notifyCaseJob, type CaseJob, type ParseBatchResult } from '../bridge';
import { msg, navigate } from '../hooks';

const labels: Record<CaseJob['status'], string> = {
  queued: '等待中', running: '进行中', completed: '已完成', failed: '失败', cancelled: '已取消',
};
const methods: Record<string, string> = {
  'case.parse': '分析生成 DSL', 'case.pull': '拉取用例', 'case.sync': '同步用例',
};

export function CaseJobsPanel({ compact = false }: { compact?: boolean }) {
  const [job, setJob] = useState<CaseJob | null>(null);
  const [error, setError] = useState('');
  const [expanded, setExpanded] = useState(false);
  useEffect(() => {
    let alive = true;
    let pending = false;
    const load = async () => {
      if (pending) return;
      pending = true;
      try {
        const jobs = await call<CaseJob[]>('job.status');
        if (alive) {
          const current = jobs[0] ?? null;
          setJob(current); setError('');
          if (current) notifyCaseJob(current);
        }
      } catch (e) { if (alive) setError(msg(e)); }
      finally { pending = false; }
    };
    void load();
    const timer = setInterval(() => void load(), 1000);
    return () => { alive = false; clearInterval(timer); };
  }, []);
  if (!job || (compact && job.status === 'completed')) return null;
  const active = job.status === 'queued' || job.status === 'running';
  const raw = (job.result ?? job.progress.result) as { parse?: ParseBatchResult; parsed?: number } | undefined;
  const result = raw?.parsed !== undefined ? raw as ParseBatchResult : raw?.parse ?? null;
  const failures = result?.failed ?? [];
  const items = result?.items ?? [];
  return <div className="case-job-panel">
    <div className={`msg ${job.status === 'failed' || failures.length ? 'warn' : 'info'}`} role="status">
      <strong>{methods[job.method] ?? job.method} · {labels[job.status]}</strong>
      {job.progress.total !== undefined && <span>已处理 {job.progress.completed ?? 0} / {job.progress.total}</span>}
      {active && job.progress.case_id && <span>当前 {job.progress.case_id}</span>}
      {result && <span>成功 {result.parsed} · 失败 {failures.length} · 待处理 {Math.max(0, result.total - result.parsed - failures.length)}</span>}
      {job.error && <span>{job.error}</span>}
      <span className="spacer" />
      {active && <button className="btn" onClick={() => {
        void call<CaseJob>('job.cancel', { jobId: job.id })
          .then(setJob).catch((e: unknown) => setError(msg(e)));
      }}>取消分析任务</button>}
      {!!(items.length || failures.length) && <button className="btn" onClick={() => setExpanded(!expanded)}>
        {expanded ? '收起结果' : '查看逐条结果'}
      </button>}
      {!compact && items.length > 0 && <button className="btn primary" onClick={() => navigate('dsl', { caseId: items[0].case_id, queue: items.map(i => i.case_id).join(',') })}>逐条审核（{items.length}）</button>}
      {error && <span>{error}</span>}
    </div>
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
  </div>;
}
