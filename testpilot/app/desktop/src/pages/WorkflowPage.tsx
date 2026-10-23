import { useEffect, useMemo, useRef, useState } from 'react';
import { call, caseJob, type ParseBatchResult } from '../bridge';
import { Badge, Empty, Msg } from '../components/Bits';
import { msg, navigate, useAsync, useCaseJobBusy } from '../hooks';
import { caseIsApproved, caseIsSelectable, caseNeedsAnalysis } from '../caseState';
import type { CaseRow, RunRow } from '../types';
import { DslReviewPage } from './DslReviewPage';

function restored<T>(key: string, fallback: T): T {
  try { return JSON.parse(sessionStorage.getItem(key) ?? 'null') ?? fallback; }
  catch { return fallback; }
}
interface AppTargetPreset {
  id: string;
  name: string;
  windowTitle: string;
  exe: string;
  launchArgs: string;
  kind: 'preset' | 'recent';
  updatedAt: number;
}

interface ExecuteFailure {
  text: string;
  taskId?: number;
  runId?: number;
  runStatus?: string;
}

const TARGET_PRESETS_KEY = 'tp-flow-target-presets-v1';
const TARGET_PRESET_LIMIT = 12;

function loadTargetPresets(): AppTargetPreset[] {
  try {
    const raw = localStorage.getItem(TARGET_PRESETS_KEY);
    if (!raw) return [];
    const value = JSON.parse(raw) as unknown;
    if (!Array.isArray(value)) return [];
    const items: AppTargetPreset[] = [];
    for (const item of value) {
      if (!item || typeof item !== 'object') continue;
      const row = item as Record<string, unknown>;
      if (typeof row.id !== 'string' || typeof row.name !== 'string'
        || typeof row.windowTitle !== 'string' || typeof row.exe !== 'string'
        || typeof row.updatedAt !== 'number') continue;
      items.push({
        id: row.id,
        name: row.name,
        windowTitle: row.windowTitle,
        exe: row.exe,
        launchArgs: typeof row.launchArgs === 'string' ? row.launchArgs : '',
        kind: row.kind === 'recent' ? 'recent' : 'preset',
        updatedAt: row.updatedAt,
      });
    }
    return items.sort((a, b) => b.updatedAt - a.updatedAt)
      .slice(0, TARGET_PRESET_LIMIT);
  } catch {
    return [];
  }
}

function persistTargetPresets(items: AppTargetPreset[]): void {
  try {
    localStorage.setItem(
      TARGET_PRESETS_KEY,
      JSON.stringify(items.slice(0, TARGET_PRESET_LIMIT)),
    );
  } catch {
    // localStorage 不可用时不阻塞本次测试。
  }
}

function presetDefaultName(windowTitle: string, exe: string): string {
  if (windowTitle.trim()) return windowTitle.trim();
  const normalized = exe.trim().replace(/\\/g, '/');
  return normalized.split('/').filter(Boolean).pop() || '被测软件';
}

function missingWindow(c: CaseRow): boolean {
  const steps = c.steps as { action?: string; args?: { exe?: string; title_re?: string } }[];
  const firstUI = steps.findIndex(s => s.action?.startsWith('ui.') || ['assert.ui', 'vision.click'].includes(s.action ?? ''));
  return firstUI >= 0 && !steps.slice(0, firstUI).some(s => s.action === 'app.launch' && (s.args?.exe || s.args?.title_re));
}

export function WorkflowPage({ selection, running, ready = false }: {
  selection?: string;
  running: number | null;
  ready?: boolean;
}) {
  const cases = useAsync(() => call<{ cases: CaseRow[] }>('case.list'), []);
  const rows = cases.data?.cases ?? [];
  const [selected, setSelected] = useState<string[]>(() => selection?.split(',').filter(Boolean) ?? restored('tp-flow-selection', []));
  const [phase, setPhase] = useState(ready && !!selection ? 3 : 1);
  const [query, setQuery] = useState('');
  const [pullIds, setPullIds] = useState('');
  const [windowTitle, setWindowTitle] = useState(() => restored('tp-flow-window-title', ''));
  const [exe, setExe] = useState(() => restored('tp-flow-exe', ''));
  const [launchArgs, setLaunchArgs] = useState(() => restored('tp-flow-launch-args', ''));
  const [targetPresets, setTargetPresets] = useState<AppTargetPreset[]>(loadTargetPresets);
  const [targetPresetId, setTargetPresetId] = useState('');
  const [targetPresetName, setTargetPresetName] = useState('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [executeFailure, setExecuteFailure] = useState<ExecuteFailure | null>(null);
  const [batch, setBatch] = useState<ParseBatchResult | null>(null);
  const [reviewId, setReviewId] = useState<string>();
  const [remote, setRemote] = useState(() => restored('tp-flow-remote', false));
  const [mode, setMode] = useState(() => restored('tp-flow-mode', 'auto'));
  const [caseRetry, setCaseRetry] = useState(() => restored('tp-flow-case-retry', 0));
  const [task, setTask] = useState<{ id: number; fingerprint: string } | null>(() => restored('tp-flow-task', null));
  const gate = useRef(false);
  const background = useCaseJobBusy();
  const locked = !!busy || background || running !== null;
  const chosen = selected.map(id => rows.find(c => c.case_id === id)).filter((c): c is CaseRow => !!c);
  const visible = useMemo(() => rows.filter(c =>
    (c.case_id + ' ' + c.title).toLowerCase().includes(query.trim().toLowerCase())), [rows, query]);
  const runnable = chosen.length === selected.length && chosen.length > 0 && chosen.every(caseIsApproved);
  const windowMissing = chosen.filter(missingWindow);
  // 任务快照只由用例版本、驱动策略和远端同步决定。
  // caseRetry 是本次 task.run 参数，改变它不应重复创建同一个任务。
  const fingerprint = JSON.stringify([chosen.map(c => [c.case_id, c.version]), remote, mode]);
  const reusable = task?.fingerprint === fingerprint ? task : null;
  useEffect(() => { setExecuteFailure(null); }, [fingerprint, caseRetry, phase]);
  useEffect(() => { sessionStorage.setItem('tp-flow-selection', JSON.stringify(selected)); }, [selected]);
  useEffect(() => { sessionStorage.setItem('tp-flow-task', JSON.stringify(task)); }, [task]);
  useEffect(() => { sessionStorage.setItem('tp-flow-window-title', JSON.stringify(windowTitle)); }, [windowTitle]);
  useEffect(() => { sessionStorage.setItem('tp-flow-exe', JSON.stringify(exe)); }, [exe]);
  useEffect(() => { sessionStorage.setItem('tp-flow-launch-args', JSON.stringify(launchArgs)); },
    [launchArgs]);
  useEffect(() => { sessionStorage.setItem('tp-flow-remote', JSON.stringify(remote)); }, [remote]);
  useEffect(() => { sessionStorage.setItem('tp-flow-mode', JSON.stringify(mode)); }, [mode]);
  useEffect(() => { sessionStorage.setItem('tp-flow-case-retry', JSON.stringify(caseRetry)); }, [caseRetry]);
  useEffect(() => {
    if (cases.data) setSelected(ids =>
      ids.filter(id => rows.some(c => c.case_id === id && caseIsSelectable(c))));
  }, [cases.data]);
  useEffect(() => {
    if (!ready || !cases.data || phase !== 3) return;
    // 深链到“执行前确认”只用于已经审核完成的 selection；如果用例状态已变化，
    // 或仍缺少被测窗口信息，就退回选择页处理必要条件，绝不直接绕过安全门槛。
    if (!runnable || windowMissing.length > 0) setPhase(1);
  }, [ready, cases.data, phase, runnable, windowMissing.length]);

  const applyTargetPreset = (id: string) => {
    setTargetPresetId(id);
    const preset = targetPresets.find((item) => item.id === id);
    if (!preset) return;
    setWindowTitle(preset.windowTitle);
    setExe(preset.exe);
    setLaunchArgs(preset.launchArgs);
    setTargetPresetName(preset.kind === 'preset' ? preset.name : '');
  };

  const saveTargetPreset = () => {
    const title = windowTitle.trim();
    const appExe = exe.trim();
    const args = launchArgs.trim();
    if (!title && !appExe) {
      setError('请先填写窗口标题或程序路径，再保存目标预设');
      return;
    }
    const name = targetPresetName.trim() || presetDefaultName(title, appExe);
    const now = Date.now();
    const existing = targetPresetId
      ? targetPresets.find((item) => item.id === targetPresetId && item.kind === 'preset')
      : undefined;
    const id = existing?.id ?? ('target-' + now.toString(36));
    const next: AppTargetPreset = {
      id, name, windowTitle: title, exe: appExe, launchArgs: args,
      kind: 'preset', updatedAt: now,
    };
    const items = [next, ...targetPresets.filter((item) => item.id !== id)]
      .slice(0, TARGET_PRESET_LIMIT);
    setTargetPresets(items);
    setTargetPresetId(id);
    setTargetPresetName(name);
    persistTargetPresets(items);
    setError('');
  };

  const deleteTargetPreset = () => {
    if (!targetPresetId) return;
    const items = targetPresets.filter((item) => item.id !== targetPresetId);
    setTargetPresets(items);
    persistTargetPresets(items);
    setTargetPresetId('');
    setTargetPresetName('');
  };

  const rememberRecentTarget = () => {
    const title = windowTitle.trim();
    const appExe = exe.trim();
    const args = launchArgs.trim();
    if (!title && !appExe) return;

    const now = Date.now();
    const sameTarget = (item: AppTargetPreset) =>
      item.windowTitle === title && item.exe === appExe && item.launchArgs === args;
    const presets = targetPresets.filter((item) => item.kind === 'preset');
    const recents = [
      {
        id: 'recent-' + now.toString(36),
        name: presetDefaultName(title, appExe),
        windowTitle: title,
        exe: appExe,
        launchArgs: args,
        kind: 'recent' as const,
        updatedAt: now,
      },
      ...targetPresets.filter((item) => item.kind === 'recent' && !sameTarget(item)),
    ].slice(0, 5);
    const items = [...presets, ...recents]
      .sort((a, b) => b.updatedAt - a.updatedAt)
      .slice(0, TARGET_PRESET_LIMIT);
    setTargetPresets(items);
    persistTargetPresets(items);
  };

  const action = async (label: string, fn: () => Promise<void>) => {
    if (gate.current) return;
    gate.current = true; setBusy(label); setError('');
    try { await fn(); } catch (e) { setError(msg(e)); }
    finally { gate.current = false; setBusy(''); }
  };
  const pull = () => action('正在拉取用例…', async () => {
    const tokens = pullIds.split(/[,，\s]+/).filter(Boolean);
    if (!tokens.length || tokens.some(t => !/^\d+$/.test(t) || !Number.isSafeInteger(Number(t)) || Number(t) <= 0))
      throw new Error('请输入正确的禅道用例 ID，例如 1024,1025');
    const result = await caseJob<{ cases: { case_id: string }[] }>('case.pull', { caseIds: [...new Set(tokens.map(Number))] });
    setSelected(prev => [...new Set([...prev, ...result.cases.map(c => c.case_id)])]);
    setPullIds(''); cases.reload();
  });
  const prepare = () => action('正在准备所选用例…', async () => {
    if (!chosen.length || chosen.length !== selected.length) throw new Error('请先选择用例');
    rememberRecentTarget();
    if (chosen.length > 500) throw new Error('每批最多选择 500 条用例');
    let appTarget: Record<string, string> | undefined;
    if (windowTitle.trim() || exe.trim()) {
      appTarget = {};
      if (exe.trim()) {
        appTarget.exe = exe.trim();
        if (launchArgs.trim()) appTarget.args = launchArgs.trim();
      }
      if (windowTitle.trim()) appTarget.title_re = '.*' + windowTitle.trim().replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '.*';
    }
    if (windowMissing.length && !appTarget) {
      throw new Error(
        `${windowMissing.map(c => c.case_id).join('、')} 缺少被测窗口信息；请展开“被测软件”补充窗口标题或程序路径`,
      );
    }
    // 已审核且目标完整的用例直接复用。只有本身待解析，或确实缺窗口且本次
    // 提供了 appTarget 的用例才重新生成 DSL，避免无意义 LLM 调用与二次审核。
    const ids = chosen
      .filter(c => caseNeedsAnalysis(c) || (!!appTarget && missingWindow(c)))
      .map(c => c.case_id);
    setBatch(null);
    if (ids.length) {
      const result = await caseJob<ParseBatchResult>('case.parse', { caseIds: ids, appTarget });
      setBatch(result);
    }
    const fresh = (await call<{ cases: CaseRow[] }>('case.list')).cases.filter(c => selected.includes(c.case_id));
    cases.reload();
    const pending = fresh.find(c => !caseIsApproved(c));
    setReviewId(pending?.case_id);
    setPhase(pending ? 2 : 3);
  });
  const accepted = async (_cid: string) => {
    try {
      // dsl.accept 已经写库；这里基于最新状态决定“下一条”，避免 cases.reload
      // 尚未完成时用旧 chosen 快照把审核队列跳回已通过用例。
      const fresh = (await call<{ cases: CaseRow[] }>('case.list')).cases
        .filter(c => selected.includes(c.case_id));
      const next = fresh.find(c => !caseIsApproved(c));
      cases.reload();
      if (next) {
        setReviewId(next.case_id);
      } else {
        setReviewId(undefined);
        setPhase(3);
      }
    } catch (e) {
      setError('刷新审核队列失败：' + msg(e));
      cases.reload();
    }
  };
  const execute = async () => {
    if (gate.current) return;
    gate.current = true;
    setBusy(reusable ? '正在执行已有任务…' : '正在创建并执行测试…');
    setError('');
    setExecuteFailure(null);
    let current = reusable;
    let previousRunId: number | null = null;
    let previousRunKnown = false;
    try {
      if (!runnable) throw new Error('请先审核所选用例');
      if (windowMissing.length)
        throw new Error('部分用例缺少被测软件的启动或附加步骤，请先设置被测窗口并生成 DSL');
      if (!current) {
        const created = await call<{ task_id: number }>('task.create', {
          name: '测试 · ' + new Date().toLocaleString('zh-CN'), caseIds: selected,
          driverConfig: { mode }, remote,
        });
        current = { id: created.task_id, fingerprint };
        // 在发起长时间执行前保存任务 ID；失败重试不重复建单。
        sessionStorage.setItem('tp-flow-task', JSON.stringify(current));
        setTask(current);
      }
      // 仅用于失败恢复：记住执行前最近一次 run。查询失败不能阻塞真正执行。
      try {
        const before = await call<{ runs: RunRow[] }>('run.list', {
          limit: 1, taskId: current.id,
        });
        previousRunId = before.runs[0]?.id ?? null;
        previousRunKnown = true;
      } catch { /* recovery hint only */ }

      const result = await call<{ run_id: number }>('task.run', {
        taskId: current.id, writeback: remote, caseRetry,
      });
      setTask(null); sessionStorage.removeItem('tp-flow-task');
      navigate('run', { runId: result.run_id });
    } catch (e) {
      // 不靠错误字符串分类。若本次调用已经产生新的 run（无论仍在执行、
      // 已完成、error 或 aborted），优先进入该 run；只有确认没产生新 run 时才允许重试。
      let runId: number | undefined;
      let runStatus: string | undefined;
      if (current && previousRunKnown) {
        try {
          const after = await call<{ runs: RunRow[] }>('run.list', {
            limit: 1, taskId: current.id,
          });
          const latest = after.runs[0];
          if (latest && latest.id !== previousRunId) {
            runId = latest.id;
            runStatus = latest.status;
          }
        } catch { /* 保留原始执行错误 */ }
      }
      setExecuteFailure({
        text: msg(e), taskId: current?.id, runId, runStatus,
      });
    } finally {
      gate.current = false;
      setBusy('');
    }
  };

  return <div className="main workflow-page">
    <div className="page-head"><div><h2>开始测试</h2><p className="muted">选用例、确认步骤、执行测试，在这里完成。</p></div>
      <span className="spacer" /><button className="btn ghost" onClick={() => navigate('overview')}>运行概览</button></div>
    <ol className="flow-steps" aria-label="测试流程">
      {['选择用例', '分析与确认', '执行测试'].map((name, i) => <li key={name} className={phase === i + 1 ? 'current' : phase > i + 1 ? 'done' : ''}>
        <span>{phase > i + 1 ? '✓' : i + 1}</span><strong>{name}</strong></li>)}
    </ol>
    {busy && <Msg kind="info">{busy}</Msg>}
    {error && <Msg kind="error">{error}</Msg>}
    {executeFailure && <Msg kind="error">
      执行未完成：{executeFailure.text}
      <span className="hstack" style={{ marginTop: 'var(--sp-2)' }}>
        {executeFailure.runId && <button className="btn primary"
          title={executeFailure.runStatus ? `本次 run 当前状态：${executeFailure.runStatus}` : undefined}
          onClick={() => navigate('run', { runId: executeFailure.runId })}>
          查看本次运行 →
        </button>}
        {!executeFailure.runId && executeFailure.taskId && <button className="btn primary"
          disabled={locked} onClick={() => void execute()}>
          重试已有任务
        </button>}
        <button className="btn" disabled={locked} onClick={() => {
          setExecuteFailure(null);
          setPhase(1);
        }}>返回修改范围</button>
      </span>
    </Msg>}
    {cases.error && <Msg kind="error">读取用例失败：{cases.error}<button className="btn" onClick={cases.reload}>重试</button></Msg>}

    {phase === 1 && <section className="panel flow-card">
      <h3>选择本次要测试的用例</h3>
      <div className="flow-import"><label htmlFor="flow-pull">从禅道添加用例</label>
        <div className="hstack"><input id="flow-pull" value={pullIds} placeholder="用例 ID，如 1024,1025" onChange={e => setPullIds(e.target.value)} />
          <button className="btn" disabled={locked || !pullIds.trim()} onClick={() => void pull()}>拉取并选中</button></div></div>
      <div className="hstack flow-filter"><input type="search" aria-label="搜索测试用例" placeholder="搜索用例编号或标题" value={query} onChange={e => setQuery(e.target.value)} />
        <span className="muted">已选 {chosen.length} 条</span>
        <button className="btn ghost" disabled={locked} onClick={() => setSelected([])}>清空选择</button></div>
      <div className="flow-case-list">
        <label className="flow-select-all"><input type="checkbox" aria-label="全选搜索结果" disabled={locked}
          checked={visible.some(c => caseIsSelectable(c)) && visible.filter(c => caseIsSelectable(c)).every(c => selected.includes(c.case_id))}
          onChange={e => { const ids = visible.filter(c => caseIsSelectable(c)).map(c => c.case_id);
            setSelected(prev => e.target.checked ? [...new Set([...prev, ...ids])] : prev.filter(id => !ids.includes(id))); }} />全选搜索结果</label>
        {visible.map(c => <label key={c.case_id} className={selected.includes(c.case_id) ? 'flow-case selected' : 'flow-case'}>
          <input type="checkbox" aria-label={'测试 ' + c.case_id} disabled={locked || !caseIsSelectable(c)} checked={selected.includes(c.case_id)}
            onChange={e => setSelected(prev => e.target.checked ? [...prev, c.case_id] : prev.filter(id => id !== c.case_id))} />
          <span className="flow-case-title"><strong>{c.title || c.case_id}</strong><small>{c.case_id} · {c.steps?.length ?? 0} 步</small></span>
          <Badge status={c.status} /><span className="muted">{
            c.status === 'deprecated' ? '已停用'
              : c.status === 'rejected' ? '已拒绝，需先修订'
                : caseIsApproved(c) ? '可直接执行'
                  : caseNeedsAnalysis(c) ? '需要生成 DSL' : '等待确认步骤'
          }</span>
        </label>)}
        {!visible.length && <Empty text={cases.loading ? '正在读取用例…' : '暂无用例，请先输入禅道 ID 拉取；也可到用例库导入 DSL。'} />}
      </div>
      <details className="flow-target" open={windowMissing.length > 0 || undefined}>
        <summary>
          被测软件
          <span className="faint">
            {windowMissing.length > 0
              ? ` · ${windowMissing.length} 条用例需要补充`
              : windowTitle || exe ? ` · ${windowTitle || presetDefaultName('', exe)}` : ' · 按需设置'}
          </span>
        </summary>
        <p className="muted">已打开软件可只填窗口标题；需要启动时填写程序路径。填写后会应用到本次生成的草稿，再由你确认。</p>
        <div className="flow-target-presets">
          <label>目标预设 / 最近使用
            <select aria-label="常用被测软件预设" value={targetPresetId}
                    onChange={(e) => applyTargetPreset(e.target.value)}>
              <option value="">手动填写</option>
              {targetPresets.some((item) => item.kind === 'preset') &&
                <optgroup label="常用预设">
                  {targetPresets.filter((item) => item.kind === 'preset').map((preset) =>
                    <option key={preset.id} value={preset.id}>{preset.name}</option>)}
                </optgroup>}
              {targetPresets.some((item) => item.kind === 'recent') &&
                <optgroup label="最近使用">
                  {targetPresets.filter((item) => item.kind === 'recent').map((preset) =>
                    <option key={preset.id} value={preset.id}>{preset.name}</option>)}
                </optgroup>}
            </select>
          </label>
          <label>预设名称
            <input value={targetPresetName}
                   onChange={(e) => setTargetPresetName(e.target.value)}
                   placeholder="例如：IVD 主控软件" />
          </label>
          <div className="flow-target-preset-actions">
            <button type="button" className="btn" disabled={locked || (!windowTitle.trim() && !exe.trim())}
                    onClick={saveTargetPreset}>
              {targetPresets.some((item) => item.id === targetPresetId && item.kind === 'preset')
                ? '更新预设' : '保存为预设'}
            </button>
            <button type="button" className="btn ghost"
                    disabled={locked || !targetPresetId}
                    onClick={deleteTargetPreset}>删除</button>
          </div>
        </div>
        {targetPresets.length > 0 && <p className="hint">
          常用预设与最近 5 个目标保存在本机；切换后会自动填入窗口标题和程序路径。
        </p>}
        <div className="form-2col"><label>窗口标题<input aria-label="被测窗口标题"
          value={windowTitle} onChange={e => setWindowTitle(e.target.value)}
          placeholder="例如：仪器控制软件" /></label>
          <label>程序路径（可选）<input aria-label="被测程序路径"
            value={exe} onChange={e => setExe(e.target.value)}
            placeholder="D:\\Software\\Instrument.exe" /></label>
          <label className="flow-target-launch-args">启动参数（可选）
            <input aria-label="被测程序启动参数" value={launchArgs}
              onChange={e => setLaunchArgs(e.target.value)}
              placeholder='例如：--test-mode --config "D:\\Config\\test.yaml"' />
          </label></div>
      </details>
      <div className="flow-footer"><span className="muted">{chosen.filter(caseNeedsAnalysis).length} 条待生成 · {chosen.filter(c => !caseIsApproved(c) && !caseNeedsAnalysis(c)).length} 条待确认 · {chosen.filter(caseIsApproved).length} 条可执行</span>
        <button className="btn primary large"
          disabled={locked || chosen.length === 0 || cases.loading
            || (windowMissing.length > 0 && !windowTitle.trim() && !exe.trim())}
          onClick={() => void prepare()}>
          {windowMissing.length > 0 && !windowTitle.trim() && !exe.trim()
            ? '先补充被测软件'
            : chosen.length > 0 && chosen.every(caseIsApproved) && windowMissing.length === 0
              ? '下一步：执行测试 →' : '下一步：分析与确认 →'}
        </button></div>
    </section>}

    {phase === 2 && <section className="flow-review">
      <div className="panel"><h3>核对生成的操作和预期结果</h3>
        <p className="muted">已确认 {chosen.filter(caseIsApproved).length} / {chosen.length} 条。逐条确认后即可执行。</p>
        {batch && batch.failed.length > 0 && <Msg kind="warn">成功生成 {batch.parsed} 条，失败 {batch.failed.length} 条。修正原文后可重试，或返回移除失败用例。</Msg>}
        <div className="flow-review-tabs">{chosen.map(c =>
          <button type="button"
            className={'chip ' + (reviewId === c.case_id ? 'on' : '')}
            key={c.case_id}
            disabled={locked}
            title={caseIsApproved(c) ? '已确认，可点击回看或编辑' : '点击切换到该用例'}
            onClick={() => setReviewId(c.case_id)}>
            {caseIsApproved(c) ? '✓ ' : ''}{c.case_id}
          </button>)}</div>
        {batch?.failed.map(f => <p key={f.case_id} className="msg error">{f.case_id}：{f.error}</p>)}
      </div>
      {reviewId && <DslReviewPage key={reviewId} embedded caseId={reviewId} onAccepted={accepted} onBack={() => { cases.reload(); setPhase(1); }} />}
      <div className="flow-footer">
        <span className="muted">确认当前用例后自动进入下一条；全部确认后自动进入执行前确认。</span>
        {runnable && <button className="btn" disabled={locked} onClick={() => setPhase(3)}>
          已全部确认，继续执行 →
        </button>}
      </div>
    </section>}

    {phase === 3 && <section className="panel flow-card">
      <h3>执行前确认</h3><p className="muted">确认范围、驱动策略和失败复跑策略后开始。运行中仍可停止，完成后自动进入报告。</p>
      <div className="flow-preflight">
        <div><strong>{chosen.length}</strong><span>用例</span></div>
        <div><strong>{chosen.reduce((n, c) => n + (c.steps?.length ?? 0), 0)}</strong><span>总步骤</span></div>
        <div><strong>{mode === 'auto' ? '自动' : mode.replace('_', ' ')}</strong><span>驱动策略</span></div>
        <div><strong>{caseRetry ? caseRetry + ' 次' : '关闭'}</strong><span>失败复跑</span></div>
      </div>
      <ul className="flow-ready-list">{chosen.map(c => <li key={c.case_id}><Badge status={c.status} /><strong>{c.title}</strong><span className="muted">{c.case_id} · v{c.version} · {c.steps.length} 步</span></li>)}</ul>
      {windowMissing.length > 0 && <Msg kind="warn">{windowMissing.map(c => c.case_id).join('、')} 缺少启动或附加窗口步骤。请返回选择用例，展开“设置被测软件”后重新生成并确认。</Msg>}
      <details className="flow-run-advanced">
        <summary>
          <strong>执行选项</strong>
          <span className="faint">
            {' · '}{mode === 'auto' ? '自动路由' : mode.replace('_', ' ')}
            {' · '}{caseRetry ? `失败复跑 ${caseRetry} 次` : '不复跑'}
            {' · '}{remote ? '回写禅道' : '仅本地'}
          </span>
        </summary>
        <div className="flow-run-options">
          <label>驱动方式<select aria-label="测试驱动方式" value={mode} onChange={e => setMode(e.target.value)} disabled={locked}>
            <option value="auto">自动路由（推荐）</option>
            <option value="uia_first">UIA 优先，再视觉兜底</option>
            <option value="vision_first">视觉优先，再 UIA 兜底</option>
            <option value="uia_only">仅 UIA / 控件驱动</option>
            <option value="vision_only">仅视觉驱动</option>
          </select></label>
          <label>失败用例复跑<select aria-label="失败用例复跑次数" value={caseRetry} onChange={e => setCaseRetry(Number(e.target.value))} disabled={locked}>
            <option value={0}>关闭（默认）</option>
            <option value={1}>失败后再跑 1 次</option>
            <option value={2}>失败后最多再跑 2 次</option>
            <option value={3}>失败后最多再跑 3 次</option>
          </select></label>
          <label className="flow-checkbox"><input type="checkbox" checked={remote} onChange={e => setRemote(e.target.checked)} disabled={locked} />
            <span><strong>同步到禅道</strong><small>创建远端任务并回写汇总结果</small></span>
          </label>
        </div>
        <p className="hint">复跑用于过滤环境抖动；报告仍保留每次尝试和首次失败证据。</p>
      </details>
      {reusable && <Msg kind="info">已创建任务 #{reusable.id}，当前配置未变化，再次执行会复用该任务。</Msg>}
      <div className="flow-footer"><button className="btn" disabled={locked} onClick={() => setPhase(1)}>返回选择用例</button>
        <button className="btn primary large" disabled={locked || !runnable || !!windowMissing.length || cases.loading} onClick={() => void execute()}>{reusable ? '重试执行已有任务 →' : '开始执行 →'}</button></div>
    </section>}
  </div>;
}
