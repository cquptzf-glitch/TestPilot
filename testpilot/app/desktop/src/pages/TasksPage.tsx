/** Tasks · 任务（设计 14.3）：任务列表 + 新建（分区表单）+ 执行/中止 + sync_pending 补偿。
 *
 *  交互约定：
 *  - 行内「执行」按**任务自身的驱动模式**执行（任务快照的 driver_config.mode），
 *    不受新建表单里下拉框的影响 —— 建完任务改表单再点执行，跑的仍是任务自己的策略；
 *  - task.run 是阻塞调用：执行期间按钮互斥 + 提示条，结束后自动跳到 run 详情；
 *  - 引擎事件（run.progress / case.finished）驱动列表刷新，执行中状态自动前进。
 */

import { useRef, useState } from 'react';

import { call } from '../bridge';
import { Badge, CopyId, Empty, Msg } from '../components/Bits';
import { PromptModal, pushToast } from '../components/Ui';
import { fmtRelative, fmtTime, statusLabel } from '../format';
import { msg, navigate, useAsync, useEngineEvents } from '../hooks';
import type { RunRow, SuiteRow, TaskRow } from '../types';

const MODES: { value: string; label: string }[] = [
  { value: 'auto', label: 'auto（按 DSL 默认路由）' },
  { value: 'uia_first', label: 'uia_first（UIA 优先）' },
  { value: 'vision_first', label: 'vision_first（视觉优先）' },
  { value: 'uia_only', label: 'uia_only（仅 UIA）' },
  { value: 'vision_only', label: 'vision_only（仅视觉）' },
];

/** 把 RPC 返回体压成一行可读摘要（原样 stringify 会长满整个 Toast）。 */
function summarize(out: unknown): string {
  if (out === null || out === undefined) return '无返回';
  if (typeof out !== 'object') return String(out);
  const o = out as Record<string, unknown>;
  const bits: string[] = [];
  for (const key of ['run_id', 'task_id', 'sync_status', 'status', 'pass', 'fail', 'total']) {
    if (o[key] !== undefined) bits.push(`${key}=${String(o[key])}`);
  }
  if (Array.isArray(o.results)) bits.push(`${(o.results as unknown[]).length} 项`);
  return bits.length > 0 ? bits.join(' ') : `${Object.keys(o).length} 个字段`;
}

export function TasksPage({ taskId }: { taskId?: number }) {
  const tasks = useAsync(() => call<{ tasks: TaskRow[] }>('task.list'), []);
  const suites = useAsync(() => call<{ suites: SuiteRow[] }>('suite.list'), []);
  const rows = tasks.data?.tasks ?? [];
  const anyRunning = rows.some((t) => (t.runs ?? []).some((r) => r.status === 'running'));

  // 新建任务表单（分区呈现，不再挤成一行）
  const [name, setName] = useState('');
  const [suiteId, setSuiteId] = useState('');
  const [caseIds, setCaseIds] = useState('');
  const [createMode, setCreateMode] = useState('');
  const [force, setForce] = useState(false);
  const [remote, setRemote] = useState(false);
  const [build, setBuild] = useState('');
  const [remoteId, setRemoteId] = useState('');
  const [reconcileTask, setReconcileTask] = useState<number | null>(null);

  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ kind: 'error' | 'info' | 'warn'; text: string } | null>(null);
  const lastReload = useRef(0);

  // 执行中任何用例结束 -> 节流刷新（2s），任务行的"最近 run"随执行自动前进；
  // run 收尾事件绕过节流，避免任务行停在"执行中"等下一次手动刷新
  useEngineEvents(['run.progress', 'case.finished'], (_ev, payload) => {
    const p = payload as { phase?: string };
    if (p?.phase !== 'finished' && Date.now() - lastReload.current < 2000) return;
    lastReload.current = Date.now();
    tasks.reload();
  });

  const run = async <T,>(label: string, fn: () => Promise<T>): Promise<T | null> => {
    setBusy(label); setNotice(null);
    try {
      const out = await fn();
      pushToast('pass', `${label}完成：${summarize(out)}`);
      tasks.reload();
      return out;
    } catch (e) {
      setNotice({ kind: 'error', text: `${label}失败：${msg(e)}` });
      return null;
    } finally { setBusy(null); }
  };

  const create = async () => {
    if (!name.trim()) { setNotice({ kind: 'warn', text: '任务名不能为空' }); return; }
    if (!suiteId && !caseIds.trim()) { setNotice({ kind: 'warn', text: '请选择方案或填用例 ID' }); return; }
    if (suiteId && caseIds.trim()) {
      setNotice({ kind: 'warn', text: '测试方案和临时用例只能选择一种来源' });
      return;
    }
    const payload: Record<string, unknown> = {
      name: name.trim(),
      driverConfig: createMode ? { mode: createMode } : undefined,
      remote,
    };
    if (remote) {
      if (build.trim()) payload.build = build.trim();
      if (remoteId.trim()) payload.zentaoTaskId = Number(remoteId);
    }
    if (suiteId) payload.suiteId = Number(suiteId);
    if (caseIds.trim()) payload.caseIds = caseIds.split(/[,，\s]+/).filter(Boolean);
    if (force) payload.force = true;
    const out = await run('建任务', () => call<{
      task_id: number; sync_status: string;
      not_runnable?: { case_id: string; reason: string }[];
    }>('task.create', payload));
    if (out?.not_runnable?.length) {
      setNotice({ kind: 'warn', text: `已强制建任务，但 ${out.not_runnable.length} 条用例不可执行：${out.not_runnable.map((b) => b.case_id).join('、')}` });
    } else if (out?.task_id) {
      setName('');
      setCaseIds('');
    }
  };

  const exec = async (t: TaskRow) => {
    setBusy(`执行 #${t.id}`);
    setNotice(null);
    try {
      // 关键：用任务快照自己的驱动模式（新建表单的下拉框只影响"下次建任务"）
      const own = String(t.driver_config?.mode ?? 'auto');
      const stats = await call<Record<string, unknown>>(
        'task.run', { taskId: t.id, mode: own === 'auto' ? undefined : own });
      pushToast('pass', `run 执行完成：${summarize(stats)}`, 8000);
      tasks.reload();
      const runId = Number((stats as { run_id?: number })?.run_id ?? 0);
      if (runId > 0) navigate('run', { runId });
    } catch (e) {
      setNotice({ kind: 'error', text: `执行失败：${msg(e)}` });
    } finally { setBusy(null); }
  };

  const lastRun = (t: TaskRow): RunRow | null => (t.runs ?? [])[0] ?? null;

  return (
    <div className="main">
      <div className="page-head">
        <h2>测试任务</h2>
        <span className="hint">从方案创建可重复运行的任务；需要时同步汇总结果到禅道</span>
      </div>

      <details className="panel task-create" open={rows.length === 0 || undefined}>
        <summary>新建测试任务 <span className="muted">选择方案或临时用例</span></summary>
        <div className="form-2col">
          <div className="field">
            <label>任务名称</label>
            <input type="text" value={name} placeholder="例：回归-20260912"
                   onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="field">
            <label>驱动模式（可选覆盖）</label>
            <select value={createMode} onChange={(e) => setCreateMode(e.target.value)}>
              <option value="">使用方案 / 全局默认</option>
              {MODES.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
            </select>
          </div>
          <div className="field">
            <label>选择方案（与临时用例二选一）</label>
            <select value={suiteId} disabled={!!caseIds.trim()}
                    onChange={(e) => { setSuiteId(e.target.value); setCaseIds(''); }}>
              <option value="">（不选方案）</option>
              {(suites.data?.suites ?? []).map((s) => (
                <option key={s.id} value={s.id}>#{s.id} {s.name}（{s.case_ids.length} 例）</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>或临时选用例 ID</label>
            <input type="text" placeholder={suiteId ? '已选择测试方案' : 'TC-1024, TC-1025（逗号/空格分隔）'}
                   value={caseIds} disabled={!!suiteId}
                   onChange={(e) => { setCaseIds(e.target.value); setSuiteId(''); }} />
          </div>
          <div className="field span2">
            <details className="task-advanced">
              <summary>高级选项
                <span className="faint">
                  {' · '}{remote ? '同步禅道' : '仅本地'}{force ? ' · 强制建任务' : ''}
                </span>
              </summary>
              <div className="hstack task-advanced-body">
                <label className="hstack muted" title="在禅道创建测试任务并回填任务号">
                  <input type="checkbox" checked={remote} onChange={(e) => setRemote(e.target.checked)} />
                  同步到禅道
                </label>
                <label className="hstack muted" title="存在不可执行用例（未生效/无步骤）时仍创建">
                  <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} />
                  允许包含不可执行用例
                </label>
              </div>
              {remote && <div className="form-2col task-remote-fields">
                <label>提测构建/版本
                  <input value={build} onChange={(e) => setBuild(e.target.value)}
                         placeholder="留空使用设置中的默认值" />
                </label>
                <label>已有禅道测试单 ID
                  <input type="number" min="1" value={remoteId}
                         onChange={(e) => setRemoteId(e.target.value)}
                         placeholder="可空；填写后复用并回读核对" />
                </label>
              </div>}
            </details>
          </div>
          <div className="span2" style={{ display: 'flex', justifyContent: 'flex-end', gap: 'var(--sp-3)' }}>
            <button className="btn" disabled={busy !== null} title="重推 sync_pending 的任务汇总"
                    onClick={() => void run('补偿回写', () => call('task.syncPending'))}>补偿待回写任务</button>
            <button className="btn" disabled={busy !== null}
                    onClick={() => { tasks.reload(); suites.reload(); }}>刷新</button>
            <button className="btn primary" disabled={busy !== null} onClick={create}>创建任务</button>
          </div>
        </div>
      </details>

      {busy && <div className="msg info">正在执行：{busy}…（可随时停止，也可以切换页面查看进度）</div>}
      {notice && <Msg kind={notice.kind}>{notice.text}</Msg>}
      {tasks.error && <Msg kind="error">读取任务失败：{tasks.error}</Msg>}

      <table className="grid">
        <thead>
          <tr>
            <th>任务</th><th>名称</th><th>禅道任务</th><th className="num">用例</th>
            <th>驱动模式</th><th>回写状态</th><th>最近 run</th><th>创建</th><th />
          </tr>
        </thead>
        <tbody>
          {rows.map((t) => {
            const ownMode = String(t.driver_config?.mode ?? 'auto');
            const recent = lastRun(t);
            const missingRemoteId = t.zentao_task_id == null;
            const uncertainCreate = missingRemoteId && (
              t.remote_state?.creation_uncertain === true
              || t.remote_state?.creation_attempted === true
            );
            return (
              <tr key={t.id} className={t.id === taskId ? 'on' : undefined}>
                <td><CopyId text={`#${t.id}`} /></td>
                <td>{t.name}</td>
                <td className="mono">{t.zentao_task_id ?? '—'}</td>
                <td className="num">{t.case_ids.length}</td>
                <td className="mono muted">{ownMode}</td>
                <td><Badge status={t.sync_status} /></td>
                <td>
                  {recent
                    ? <span className="hstack">
                        <span className={`dot ${recent.status === 'running' ? 'busy' : 'off'}`} />
                        <button className="copy" title="打开 run 详情"
                                onClick={() => navigate('run', { runId: recent.id })}>
                          #{recent.id} · {statusLabel(recent.status)}
                        </button>
                        <span className="faint">{fmtRelative(recent.started_at)}</span>
                      </span>
                    : <span className="faint">未执行</span>}
                </td>
                <td className="mono muted" title={fmtTime(t.created_at)}>{fmtRelative(t.created_at)}</td>
                <td style={{ whiteSpace: 'nowrap' }}>
                  <button className="btn" disabled={busy !== null} title={`按任务自身的驱动模式（${ownMode}）执行`}
                          onClick={() => void exec(t)}>执行</button>{' '}
                  <button className="btn danger" disabled={!anyRunning && !busy?.startsWith('执行')}
                          title="中止当前 run（全局协作中止，轨迹完整保留）"
                          onClick={() => void run('中止', () => call('task.abort'))}>中止</button>{' '}
                  {t.sync_status === 'sync_pending' && missingRemoteId && (
                    <button className={uncertainCreate ? 'btn primary' : 'btn'}
                            disabled={busy !== null}
                            title={uncertainCreate
                              ? '上次创建结果不确定，必须先在禅道核对实际测试单 ID，系统不会自动再次创建'
                              : '如果禅道已经存在对应测试单，可在这里绑定并核对'}
                            onClick={() => setReconcileTask(t.id)}>
                      {uncertainCreate ? '需核对远端 ID' : '核对远端 ID'}
                    </button>
                  )}
                  {t.sync_status === 'sync_pending' && !uncertainCreate && (
                    <button className="btn" disabled={busy !== null}
                            title={missingRemoteId ? '明确创建失败，可安全重试创建' : '重试汇总结果回写'}
                            onClick={() => void run('单任务补偿', () => call('task.syncPending', { taskId: t.id }))}>
                      补偿
                    </button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {rows.length === 0 && !tasks.loading && (
        <Empty
          text="还没有独立任务。日常回归直接用“开始测试”；只有需要长期复用固定范围或高级参数时才在这里建任务。"
          action={<span className="hstack">
            <button className="btn primary" onClick={() => navigate('home')}>开始测试</button>
            <button className="btn" onClick={() => navigate('suites')}>管理测试方案</button>
          </span>}
        />
      )}
      {reconcileTask !== null && <PromptModal open title="核对已有禅道测试单" label="测试单 ID"
        defaultValue="" confirmText="核对并补偿" busy={busy !== null}
        validate={(value) => /^[1-9][0-9]*$/.test(value) ? null : "请填写正整数 ID"}
        onCancel={() => setReconcileTask(null)}
        onSubmit={(value) => {
          const taskId = reconcileTask;
          void run('核对远端', () => call('task.reconcile', { taskId, zentaoTaskId: Number(value) }))
            .then((out) => { if (out) setReconcileTask(null); });
        }} />}
      {anyRunning && (
        <div className="msg info" style={{ marginTop: 'var(--sp-3)' }}>
          有任务正在执行：列表会随引擎事件自动刷新；可在执行记录页实时查看进度。
        </div>
      )}
    </div>
  );
}
