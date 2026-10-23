/** SVN Change Analysis · SVN 修改 -> Pi Agent 级联影响分析 -> 测试用例草稿。
 *
 * Pi 只负责推理 / Tool Call 决策；所有文件、SVN、用例查询都由 TestPilot 主 RPC
 * 执行。生成结果由主进程校验并落 draft，仍需人工审核后才能 active。
 */
import { useState } from 'react';

import { call, caseJob } from '../bridge';
import { Msg } from '../components/Bits';
import { AgentAuditTrail } from '../components/AgentAuditTrail';
import { pushToast } from '../components/Ui';
import { msg, navigate, useCaseJobBusy } from '../hooks';
import type {
  AgentToolAuditEntry, SvnChangeGenerateResult, SvnChangePreview,
} from '../types';

const WC_KEY = 'tp-svn-working-copy-v1';

function savedWorkingCopy(): string {
  try { return localStorage.getItem(WC_KEY) ?? ''; } catch { return ''; }
}

function saveWorkingCopy(value: string): void {
  try { localStorage.setItem(WC_KEY, value); } catch { /* ignore */ }
}

function changedRows(preview: SvnChangePreview) {
  const map = new Map<string, { status: string; path: string }>();
  for (const item of preview.changed ?? []) {
    map.set(item.path, { status: item.status || item.item || '?', path: item.path });
  }
  for (const item of preview.working_status ?? []) {
    if (!map.has(item.path)) {
      map.set(item.path, { status: item.status || item.item || '?', path: item.path });
    }
  }
  return Array.from(map.values());
}

export function ChangeAnalysisPage() {
  const [workingCopy, setWorkingCopy] = useState(savedWorkingCopy);
  const [fromRevision, setFromRevision] = useState('');
  const [toRevision, setToRevision] = useState('');
  const [maxCases, setMaxCases] = useState(20);
  const [parseDsl, setParseDsl] = useState(true);
  const [preview, setPreview] = useState<SvnChangePreview | null>(null);
  const [result, setResult] = useState<SvnChangeGenerateResult | null>(null);
  const [failureAudit, setFailureAudit] = useState<AgentToolAuditEntry[]>([]);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const jobBusy = useCaseJobBusy();
  const locked = !!busy || jobBusy;

  const params = () => ({
    workingCopy: workingCopy.trim(),
    fromRevision: fromRevision.trim() || undefined,
    toRevision: toRevision.trim() || undefined,
  });

  const runPreview = async () => {
    setFailureAudit([]);
    setResult(null);
    if (!workingCopy.trim()) {
      setError('请填写 SVN 工作副本目录');
      return;
    }
    setBusy('正在读取 SVN 变更…');
    setError('');
    try {
      saveWorkingCopy(workingCopy.trim());
      const out = await call<SvnChangePreview>('change.svn.preview', params());
      setPreview(out);
      setResult(null);
      if ((out.changed?.length ?? 0) + (out.working_status?.length ?? 0) === 0) {
        setError('当前 revision 范围 / 工作副本没有检测到修改');
      }
    } catch (e) {
      setError(msg(e));
    } finally {
      setBusy('');
    }
  };

  const generate = async () => {
    setFailureAudit([]);
    setResult(null);
    if (!workingCopy.trim()) {
      setError('请填写 SVN 工作副本目录');
      return;
    }
    setBusy('Pi Agent 正在分析变更并生成测试用例…');
    setError('');
    try {
      saveWorkingCopy(workingCopy.trim());
      const out = await caseJob<SvnChangeGenerateResult>('change.svn.generate', {
        ...params(), maxCases, parse: parseDsl,
      }, (job) => {
        if (job.progress.agent_audit?.length) {
          setFailureAudit(job.progress.agent_audit);
        }
      });
      setFailureAudit([]);
      setResult(out);
      setPreview(out.snapshot);
      pushToast('pass',
        `新增 ${out.created} 条，复用 ${out.reused ?? 0} 条，去重 ${out.duplicates_dropped ?? 0} 条${parseDsl ? '；待解析草稿已进入 DSL 审核队列' : ''}`);
      window.dispatchEvent(new Event('testpilot:cases-changed'));
    } catch (e) {
      setError(msg(e));
    } finally {
      setBusy('');
    }
  };

  const rows = preview ? changedRows(preview) : [];
  const generatedIds = result?.cases.map((item) => item.case_id) ?? [];
  const parsedIds = result?.parse?.items?.map((item) => item.case_id) ?? [];

  return <div className="main">
    <div className="page-head">
      <div>
        <h2>SVN 变更生成</h2>
        <p className="muted">选择修改范围后直接生成测试用例；“预览修改”只是可选核对步骤。</p>
      </div>
      <span className="chip">Pi Agent</span>
      <span className="chip">RPC-only</span>
    </div>

    <div className="panel change-config">
      <div className="form-grid change-form">
        <label>SVN 工作副本
          <input type="text" value={workingCopy}
                 onChange={(e) => setWorkingCopy(e.target.value)}
                 placeholder="D:\\work\\MyProduct" />
        </label>
        <label>起始 Revision
          <input type="text" value={fromRevision}
                 onChange={(e) => setFromRevision(e.target.value)}
                 placeholder="可空；如 1200 / BASE" />
        </label>
        <label>结束 Revision
          <input type="text" value={toRevision}
                 onChange={(e) => setToRevision(e.target.value)}
                 placeholder="可空；如 1215 / HEAD" />
        </label>
        <label>最多生成
          <input type="number" min={1} max={50} value={maxCases}
                 onChange={(e) => setMaxCases(Math.max(1, Math.min(50, Number(e.target.value) || 1)))} />
        </label>
      </div>
      <div className="change-primary-actions">
        <button className="btn primary large" disabled={locked || !workingCopy.trim()}
                onClick={() => void generate()}>
          分析变更并生成测试用例
        </button>
        <button className="btn" disabled={locked || !workingCopy.trim()}
                onClick={() => void runPreview()}>
          仅预览修改
        </button>
        <label className="hstack muted">
          <input type="checkbox" checked={parseDsl}
                 onChange={(e) => setParseDsl(e.target.checked)} />
          自动生成 DSL 草稿
        </label>
        {busy && <span className="muted">{busy}</span>}
      </div>
      <p className="hint">
        Revision 都留空＝分析当前工作副本；只填结束 Revision＝分析该次提交。
        Pi 不直接读文件或执行 Shell，所有读取都经 TestPilot 主 RPC。
      </p>
    </div>

    {error && <Msg kind="error">{error}</Msg>}
    {error && failureAudit.length > 0 &&
      <AgentAuditTrail audit={failureAudit} title="Agent 失败前工具轨迹" open />}

    {preview && <details className="panel change-snapshot" open={!result || undefined}>
      <summary>
        <strong>本次变更</strong>
        <span className="faint">
          {' · '}{rows.length} 个文件 · {preview.from_revision || 'WC'} → {preview.to_revision || preview.info?.revision || 'WC'}
        </span>
      </summary>
      <dl className="kv" style={{ marginTop: 10 }}>
        <dt>工作副本</dt><dd>{preview.working_copy}</dd>
        <dt>仓库</dt><dd>{preview.info?.url || preview.info?.repository_root || '—'}</dd>
      </dl>
      <div className="change-files compact">
        {rows.map((item) => <div key={item.path} className="change-file-row">
          <span className="badge">{item.status}</span>
          <span className="mono">{item.path}</span>
        </div>)}
      </div>
    </details>}

    {result && <div className="panel change-result">
      <div className="change-result-head">
        <div><h3>影响分析与测试用例</h3><p className="pre-wrap">{result.summary}</p></div>
        <div className="change-kpis">
          <span><strong>{result.created}</strong>新增</span>
          <span><strong>{result.reused ?? 0}</strong>复用</span>
          <span><strong>{result.duplicates_dropped ?? 0}</strong>去重</span>
          <span><strong>{result.agent?.tool_calls ?? 0}</strong>工具调用</span>
        </div>
      </div>
      <div className="change-tags">
        {(result.impacted_modules ?? []).map((item) =>
          <span className="chip" key={item}>{item}</span>)}
      </div>
      {!!result.risks?.length && <div className="msg warn" style={{ marginTop: 12 }}>
        <strong>需要重点回归：</strong>{result.risks.join('；')}
      </div>}
      {!!result.agent?.audit?.length &&
        <AgentAuditTrail audit={result.agent.audit} />}
      <table className="grid" style={{ marginTop: 12 }}>
        <thead><tr><th>测试用例</th><th>级别 / 类型</th><th>代码来源</th><th>下一步</th></tr></thead>
        <tbody>{result.cases.map((item) => <tr key={item.case_id}>
          <td><span className="mono">{item.case_id}</span>
            {item.reused && <span className="badge synced" style={{ marginLeft: 6 }}>复用</span>}
            <div>{item.title}</div>
          </td>
          <td>{item.priority} · {item.category}</td>
          <td className="pre-wrap">{item.source_files.slice(0, 4).join('\n') || '—'}</td>
          <td><button className="btn" onClick={() => navigate('dsl', { caseId: item.case_id })}>
            {parsedIds.includes(item.case_id) ? '审核 DSL' : '查看草稿'}
          </button></td>
        </tr>)}</tbody>
      </table>
      <div className="hstack" style={{ marginTop: 12 }}>
        <button className="btn" onClick={() => navigate('cases', {
          selection: generatedIds.join(','),
        })}>回到用例库</button>
        {parsedIds.length > 0 && <button className="btn primary"
          onClick={() => navigate('dsl', {
            caseId: parsedIds[0], queue: parsedIds.join(','),
          })}>审核本次 DSL（{parsedIds.length}）→</button>}
        <span className="spacer" />
        <span className="faint">
          {result.generation_run_id ? `批次 ${result.generation_run_id} · ` : ''}
          Agent {result.agent?.elapsed_ms ?? 0} ms · DSL {result.parse
            ? `成功 ${result.parse.parsed ?? 0} / 失败 ${result.parse.failed?.length ?? 0}`
            : '未生成'}
        </span>
      </div>
    </div>}
  </div>;
}
