/** SVN Change Analysis · SVN 修改 -> Pi Agent 级联影响分析 -> 测试用例草稿。
 *
 * Pi 只负责推理 / Tool Call 决策；所有文件、SVN、用例查询都由 TestPilot 主 RPC
 * 执行。生成结果由主进程校验并落 draft，仍需人工审核后才能 active。
 */
import { useState } from 'react';

import { call, caseJob } from '../bridge';
import { Empty, Msg } from '../components/Bits';
import { pushToast } from '../components/Ui';
import { msg, navigate, useCaseJobBusy } from '../hooks';
import type { SvnChangeGenerateResult, SvnChangePreview } from '../types';

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
      });
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
      <h2>SVN 变更生成测试用例</h2>
      <span className="chip">Pi Agent</span>
      <span className="chip">RPC-only tools</span>
    </div>

    <div className="msg info">
      <strong>权限边界：</strong>
      Pi 不直接读取文件、不执行 SVN / Shell、不写数据库。Agent 的每个工具调用都会通过
      TestPilot 主 RPC 执行，并绑定当前工作副本；生成用例先落草稿，审核通过后才可执行。
    </div>

    <div className="panel change-config">
      <h3>1. 选择 SVN 修改范围</h3>
      <div className="form-grid change-form">
        <label>工作副本目录
          <input type="text" value={workingCopy}
                 onChange={(e) => setWorkingCopy(e.target.value)}
                 placeholder="D:\\work\\MyProduct" />
        </label>
        <label>起始 Revision
          <input type="text" value={fromRevision}
                 onChange={(e) => setFromRevision(e.target.value)}
                 placeholder="可空；例如 1200 / BASE" />
        </label>
        <label>结束 Revision
          <input type="text" value={toRevision}
                 onChange={(e) => setToRevision(e.target.value)}
                 placeholder="可空；例如 1215 / HEAD" />
        </label>
        <label>最多生成用例
          <input type="number" min={1} max={50} value={maxCases}
                 onChange={(e) => setMaxCases(Math.max(1, Math.min(50, Number(e.target.value) || 1)))} />
        </label>
      </div>
      <div className="hstack" style={{ marginTop: 12 }}>
        <button className="btn" disabled={locked || !workingCopy.trim()}
                onClick={() => void runPreview()}>
          预览修改
        </button>
        <label className="hstack muted">
          <input type="checkbox" checked={parseDsl}
                 onChange={(e) => setParseDsl(e.target.checked)} />
          生成后继续解析为 DSL 草稿
        </label>
        <button className="btn primary" disabled={locked || !workingCopy.trim()}
                onClick={() => void generate()}>
          Agent 分析并生成用例
        </button>
        {busy && <span className="muted">{busy}</span>}
      </div>
      <p className="hint">
        Revision 都留空：分析工作副本未提交修改；填写结束 revision：按提交变更分析。
        涉及调用链时 Agent 会继续通过 RPC 读取相关源码和已有测试用例，生成级联影响测试。
      </p>
    </div>

    {error && <Msg kind="error">{error}</Msg>}

    <div className="row split change-layout">
      <div className="left">
        <div className="panel">
          <h3>2. SVN 变更快照</h3>
          {!preview ? <Empty text="先点“预览修改”，确认本次分析范围" /> : <>
            <dl className="kv">
              <dt>工作副本</dt><dd>{preview.working_copy}</dd>
              <dt>Revision</dt>
              <dd>{preview.from_revision || 'WC'} → {preview.to_revision || preview.info?.revision || 'WC'}</dd>
              <dt>仓库</dt><dd>{preview.info?.url || preview.info?.repository_root || '—'}</dd>
              <dt>变更文件</dt><dd>{rows.length}</dd>
            </dl>
            <div className="change-files">
              {rows.map((item) => <div key={item.path} className="change-file-row">
                <span className="badge">{item.status}</span>
                <span className="mono">{item.path}</span>
              </div>)}
            </div>
          </>}
        </div>
      </div>

      <div className="right">
        <div className="panel">
          <h3>3. Agent 影响分析与测试用例</h3>
          {!result ? <Empty text="生成后会显示影响模块、风险和新增测试用例" /> : <>
            <p className="pre-wrap">{result.summary}</p>
            <div className="change-tags">
              {(result.impacted_modules ?? []).map((item) =>
                <span className="chip" key={item}>{item}</span>)}
            </div>
            {!!result.risks?.length && <div className="msg warn" style={{ marginTop: 12 }}>
              <strong>风险：</strong>{result.risks.join('；')}
            </div>}
            <dl className="kv" style={{ marginTop: 12 }}>
              <dt>新增用例</dt><dd>{result.created}</dd>
              <dt>复用已有</dt><dd>{result.reused ?? 0}</dd>
              <dt>本轮去重</dt><dd>{result.duplicates_dropped ?? 0}</dd>
              <dt>Agent 工具调用</dt><dd>{result.agent?.tool_calls ?? 0}</dd>
              <dt>Agent 耗时</dt><dd>{result.agent?.elapsed_ms ?? 0} ms</dd>
              <dt>DSL 解析</dt>
              <dd>{result.parse
                ? `成功 ${result.parse.parsed ?? 0} / 失败 ${result.parse.failed?.length ?? 0}`
                : '未执行'}</dd>
            </dl>
            <table className="grid" style={{ marginTop: 12 }}>
              <thead><tr><th>用例</th><th>优先级</th><th>类型</th><th>代码来源</th><th /></tr></thead>
              <tbody>{result.cases.map((item) => <tr key={item.case_id}>
                <td><span className="mono">{item.case_id}</span>
                  {item.reused && <span className="badge synced" style={{ marginLeft: 6 }}>复用</span>}
                  <div>{item.title}</div>
                </td>
                <td>{item.priority}</td>
                <td>{item.category}</td>
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
                })}>逐条审核 DSL（{parsedIds.length}）</button>}
            </div>
          </>}
        </div>
      </div>
    </div>
  </div>;
}
