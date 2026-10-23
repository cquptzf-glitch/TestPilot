/** Cases · 用例管理（设计 14.3）：按 ID 拉取 / 增量同步 / sync_status 徽章 / 原文-DSL 并列详情。 */

import { useEffect, useMemo, useState } from 'react';

import { call, caseJob } from '../bridge';
import { Badge, Chips, CopyId, Empty, Msg } from '../components/Bits';
import { PromptModal, pushToast } from '../components/Ui';
import { fmtTime, statusLabel } from '../format';
import { msg, navigate, useAsync, useCaseJobBusy } from '../hooks';
import { caseCanAnalyze, caseIsApproved, caseIsSelectable, caseNeedsAnalysis } from '../caseState';
import type { CaseDetail, CaseRow, SourceSyncState } from '../types';

function parseIds(text: string): number[] {
  return text.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean).map(Number)
    .filter((n) => Number.isSafeInteger(n) && n > 0);
}

/** 把 RPC 返回体压成一行可读摘要（原样 JSON.stringify 在 Toast 里会长到不可读）。 */
function summarize(out: unknown): string {
  if (out === null || out === undefined) return '无返回';
  if (typeof out !== 'object') return String(out);
  const o = out as Record<string, unknown>;
  for (const key of ['count', 'total', 'written', 'task_id', 'id', 'name']) {
    if (key in o) return `${key}=${String(o[key])}`;
  }
  if (Array.isArray(o.cases)) return `${(o.cases as unknown[]).length} 条用例`;
  if (Array.isArray(o.results)) return `${(o.results as unknown[]).length} 项结果`;
  const keys = Object.keys(o);
  return keys.length > 0 ? `${keys.length} 个字段（${keys.slice(0, 4).join(', ')}…）` : '空对象';
}

export function CasesPage({ selection }: { selection?: string }) {
  const [ids, setIds] = useState('');
  const [parseAfter, setParseAfter] = useState(false);
  const [status, setStatus] = useState('');
  const [syncStatus, setSyncStatus] = useState('');
  const [query, setQuery] = useState('');
  const [group, setGroup] = useState('');
  const [selected, setSelected] = useState<string[]>(() => selection?.split(',').filter(Boolean) ?? []);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ kind: 'error' | 'warn' | 'info'; text: string } | null>(null);
  /** 非 null 时弹「创建方案」对话框（替代 window.prompt） */
  const [suiteName, setSuiteName] = useState<string | null>(null);
  /** 非 null 时弹「创建测试任务」对话框（选中用例直达任务，免先建方案） */
  const [taskName, setTaskName] = useState<string | null>(null);
  /** 按禅道分组聚合展示（分组标题行可折叠） */
  const [aggregated, setAggregated] = useState(false);
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set());

  const list = useAsync(
    () => call<{ cases: CaseRow[]; sync_state?: SourceSyncState | null }>('case.list', {
      status: status || undefined, syncStatus: syncStatus || undefined,
    }),
    [status, syncStatus],
  );
  const allRows = list.data?.cases ?? [];
  const jobBusy = useCaseJobBusy();
  const locked = busy !== null || jobBusy;
  // 禅道用例分组（模块）：下拉选项取自当前列表的去重结果；null = 未分组/未同步到分组
  const groups = useMemo(() => {
    const set = new Set<string>();
    for (const r of allRows) set.add(r.module_name || '');
    return Array.from(set).filter(Boolean).sort();
  }, [allRows]);
  // 文本过滤：ID / 禅道号 / 标题（列表是本地数据，直接前端过滤即可）
  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    return allRows.filter((r) => {
      if (group && (r.module_name || '') !== group) return false;
      if (!q) return true;
      return `${r.case_id} ${r.zentao_case_id ?? ''} ${r.title}`.toLowerCase().includes(q);
    });
  }, [allRows, query, group]);

  // 筛选后只操作当前结果，避免对隐藏用例发起分析或创建任务。
  useEffect(() => {
    if (list.data) setSelected(prev =>
      prev.filter(id => rows.some(r => r.case_id === id && caseIsSelectable(r))));
  }, [rows, list.data]);
  const pendingRows = rows.filter(r => caseCanAnalyze(r) && caseNeedsAnalysis(r));
  const selectedRows = rows.filter(r => selected.includes(r.case_id));
  const canCreate = selectedRows.length > 0 && selectedRows.every(caseIsApproved);
  const canAnalyze = selectedRows.length > 0 && selectedRows.every(caseCanAnalyze);
  const review = (caseIds: string[]) => navigate('dsl', { caseId: caseIds[0], queue: caseIds.join(',') });
  const analyze = (caseIds: string[]) => run('批量分析生成 DSL',
    () => caseJob('case.parse', { caseIds }), () => { list.reload(); detail.reload(); });

  /** 聚合视图的渲染清单：group 行（可折叠）+ 其成员行 */
  const tableItems = useMemo(() => {
    if (!aggregated) return rows.map((r) => ({ kind: 'row' as const, row: r, key: r.case_id }));
    const buckets = new Map<string, typeof rows>();
    for (const r of rows) {
      const g = r.module_name || '';
      if (!buckets.has(g)) buckets.set(g, []);
      buckets.get(g)!.push(r);
    }
    const out: ({ kind: 'group'; key: string; label: string; count: number }
      | { kind: 'row'; row: (typeof rows)[number]; key: string })[] = [];
    for (const [name, grp] of buckets) {
      out.push({ kind: 'group', key: `g:${name}`, label: name, count: grp.length });
      if (!collapsed.has(name)) {
        for (const r of grp) out.push({ kind: 'row', row: r, key: r.case_id });
      }
    }
    return out;
  }, [aggregated, rows, collapsed]);

  const toggleGroup = (name: string) => setCollapsed((prev) => {
    const next = new Set(prev);
    if (next.has(name)) next.delete(name); else next.add(name);
    return next;
  });

  const summary = useMemo(() => {
    const out: Record<string, number> = {};
    for (const r of allRows) out[r.sync_status] = (out[r.sync_status] ?? 0) + 1;
    return out;
  }, [allRows]);

  /** 来源级最后同步时间优先：即使本次 0 变更，也能准确显示“刚同步过”。
   *  旧库尚未产生 source_sync_state 时，回落到历史用例的 last_synced_at。 */
  const lastSync = useMemo(() => {
    if (list.data?.sync_state?.synced_at) return list.data.sync_state.synced_at;
    const times = allRows.map((r) => r.last_synced_at).filter(Boolean) as string[];
    if (times.length === 0) return null;
    return times.reduce((a, b) => (a > b ? a : b));
  }, [list.data?.sync_state?.synced_at, allRows]);

  const detail = useAsync(
    () => (detailId ? call<CaseDetail>('case.detail', { caseId: detailId }) : Promise.resolve(null)),
    [detailId],
  );

  /** 统一包装：把 RPC 异常转成可读提示，避免每个按钮各写一遍 try/catch。
   *
   *  成功走 Toast（不占位、可叠加），失败走常驻提示条（要让人看见并停下）。
   *  返回 RPC 结果（失败为 null），调用方可据此取返回体里的 id 做跳转。
   */
  const run = async (label: string, fn: () => Promise<unknown>, after?: () => void)
    : Promise<Record<string, unknown> | null> => {
    setBusy(label);
    setNotice(null);
    try {
      const out = await fn() as Record<string, unknown> | null;
      const result = (out?.parse ?? out) as { parsed?: number; failed?: unknown[] } | null;
      if (result?.failed?.length) setNotice({ kind: 'warn', text: `${label}：成功 ${result.parsed ?? 0} 条，失败 ${result.failed.length} 条。请查看上方逐条结果。` });
      else pushToast('pass', `${label} 完成：${result?.parsed !== undefined ? `生成 ${result.parsed} 条 DSL 草稿，请逐条审核` : summarize(out)}`);
      after?.();
      return out;
    } catch (e) {
      setNotice({ kind: 'error', text: `${label} 失败：${msg(e)}` });
      return null;
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="main">
      <div className="page-head">
        <div><h2>用例维护</h2><p className="muted">同步来源、生成 DSL、审核；日常执行直接用“开始测试”。</p></div>
        <span className="hstack" style={{ gap: 'var(--sp-2)' }}>
          <button className={`chip ${syncStatus === '' ? 'on' : ''}`}
                  onClick={() => setSyncStatus('')}>
            全部 {allRows.length}
          </button>
          {Object.entries(summary).map(([k, n]) => (
            <button key={k} className={`chip ${syncStatus === k ? 'on' : ''}`}
                    title={`只看${statusLabel(k)}`}
                    onClick={() => setSyncStatus(syncStatus === k ? '' : k)}>
              {statusLabel(k)} {n}
            </button>
          ))}
        </span>
        {(query || status || syncStatus) && (
          <button className="btn ghost" onClick={() => { setQuery(''); setStatus(''); setSyncStatus(''); }}>
            清除筛选
          </button>
        )}
      </div>

      <details className="panel case-source-panel">
        <summary>
          <strong>添加与同步用例</strong>
          <span className="faint">
            {lastSync ? ` · 上次同步 ${fmtTime(lastSync)}` : ' · 首次使用请先拉取或同步'}
          </span>
        </summary>
        <div className="toolbar source-toolbar">
          <input
            type="text" style={{ width: 280 }} value={ids}
            placeholder="禅道用例 ID，如 1024,1025"
            onChange={(e) => setIds(e.target.value)}
          />
          <button className="btn primary" disabled={locked || parseIds(ids).length === 0}
                  onClick={() => run('拉取', () => caseJob('case.pull', { caseIds: parseIds(ids), parse: parseAfter }), list.reload)}>
            拉取指定用例
          </button>
          <button className="btn" disabled={locked}
                  title={lastSync
                    ? `按远端修改水位增量同步；上次同步：${fmtTime(lastSync)}`
                    : '首次同步会扫描当前产品用例'}
                  onClick={() => run('增量同步', () => caseJob('case.sync', {
                    parse: parseAfter, full: false,
                  }), list.reload)}>
            增量同步
          </button>
          <label className="hstack muted">
            <input type="checkbox" checked={parseAfter} onChange={(e) => setParseAfter(e.target.checked)} />
            同步后自动生成 DSL
          </label>
          <span className="spacer" />
          <button className="btn ghost" disabled={locked}
                  title="忽略增量水位，完整扫描禅道当前产品；仅用于校准、迁移或怀疑漏同步时"
                  onClick={() => run('全量校准', () => caseJob('case.sync', {
                    parse: parseAfter, full: true,
                  }), list.reload)}>
            全量校准
          </button>
        </div>
      </details>
      <div className="toolbar">
        <input
          type="search" style={{ width: 200 }} value={query}
          placeholder="搜索 ID / 标题"
          onChange={(e) => setQuery(e.target.value)}
        />
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">全部状态</option>
          <option value="draft">草稿</option>
          <option value="active">已生效</option>
          <option value="rejected">已拒绝</option>
          <option value="deprecated">已停用</option>
        </select>
        <select value={syncStatus} onChange={(e) => setSyncStatus(e.target.value)}>
          <option value="">全部同步态</option>
          <option value="synced">已同步</option>
          <option value="stale">待重解析</option>
          <option value="conflict">有冲突</option>
        </select>
        {groups.length > 0 && (
          <select value={group} onChange={(e) => setGroup(e.target.value)}
                  title="按禅道用例分组（模块）筛选">
            <option value="">全部分组</option>
            {groups.map((g) => <option key={g} value={g}>{g}</option>)}
          </select>
        )}
        {groups.length > 0 && (
          <label className="hstack muted" title="用例表按分组（模块）分节展示，可折叠">
            <input type="checkbox" checked={aggregated}
                   onChange={(e) => setAggregated(e.target.checked)} />
            按分组聚合
          </label>
        )}
      </div>
      <div className="toolbar case-actions primary-actions">
        <span className="muted">当前 {rows.length} 条 · 已选 {selectedRows.length} 条</span>
        <button className="btn primary" disabled={locked || selectedRows.length === 0}
          onClick={() => navigate('home', { selection: selectedRows.map(r => r.case_id).join(',') })}>
          开始测试（{selectedRows.length}）→
        </button>
        <button className="btn" disabled={locked || !canAnalyze}
          onClick={() => void analyze(selectedRows.map(r => r.case_id))}>
          生成 / 更新 DSL
        </button>
        <button className="btn" disabled={!selectedRows.some(r => r.steps?.length)}
          onClick={() => review(selectedRows.filter(r => r.steps?.length).map(r => r.case_id))}>
          审核 DSL
        </button>
        <details className="inline-more">
          <summary className="btn ghost">更多操作</summary>
          <div className="more-actions-popover">
            <button className="btn" disabled={locked || pendingRows.length === 0}
              onClick={() => void analyze(pendingRows.map(r => r.case_id))}>
              生成当前全部待更新（{pendingRows.length}）
            </button>
            <button className="btn" disabled={locked || !canCreate}
              onClick={() => setTaskName(`任务-${new Date().toISOString().slice(5, 10)}`)}>
              创建独立测试任务
            </button>
            <button className="btn" disabled={locked || !canCreate}
              onClick={() => setSuiteName(`方案-${new Date().toISOString().slice(5, 10)}`)}>
              保存为测试方案
            </button>
          </div>
        </details>
        {selectedRows.length > 0 && !canCreate && <span className="hint">未审核用例可直接点“开始测试”，流程会先带你完成 DSL 审核。</span>}
      </div>

      <PromptModal
        open={suiteName !== null}
        title="创建测试方案"
        label="方案名称"
        defaultValue={suiteName ?? ''}
        hint={`选用例 ${selected.length} 条：${selected.slice(0, 6).join('、')}${selected.length > 6 ? ' …' : ''}`}
        confirmText="创建"
        busy={busy !== null}
        onCancel={() => setSuiteName(null)}
        onSubmit={(name) => {
          setSuiteName(null);
          void run('创建方案', () => call('suite.create', { name, caseIds: selected }), () => {
            setSelected([]);
            navigate('suites');
          });
        }}
      />

      <PromptModal
        open={taskName !== null}
        title="创建测试任务"
        label="任务名称"
        defaultValue={taskName ?? ''}
        hint={`直接以选用例建任务（快照展开，免先建方案）：${selected.slice(0, 6).join('、')}${selected.length > 6 ? ' …' : ''}`}
        confirmText="创建并前往"
        busy={busy !== null}
        onCancel={() => setTaskName(null)}
        onSubmit={async (name) => {
          setTaskName(null);
          const out = await run('创建任务', () => call<{ task_id: number }>(
            'task.create', { name, caseIds: selected }));
          if (out && out.task_id) {
            setSelected([]);
            navigate('tasks', { taskId: Number(out.task_id) });
          }
        }}
      />

      {busy && <div className="msg info">正在执行：{busy}…</div>}
      {notice && <Msg kind={notice.kind}>{notice.text}</Msg>}
      {list.error && <Msg kind="error">读取用例失败：{list.error}</Msg>}

      <table className="grid">
        <thead>
          <tr>
            <th style={{ width: 30 }}><input type="checkbox" aria-label="全选当前可用用例"
              checked={rows.some(caseIsSelectable)
                && rows.filter(caseIsSelectable).every(r => selected.includes(r.case_id))}
              ref={el => {
                if (el) {
                  const selectableCount = rows.filter(caseIsSelectable).length;
                  el.indeterminate = selectedRows.length > 0
                    && selectedRows.length < selectableCount;
                }
              }}
              onChange={e => setSelected(
                e.target.checked ? rows.filter(caseIsSelectable).map(r => r.case_id) : [],
              )} /></th>
            <th>用例 ID</th>
            <th>标题</th>
            <th>分组</th>
            <th className="num">DSL 版本</th>
            <th>同步状态</th>
            <th>状态</th>
            <th className="num">步骤</th>
            <th>更新时间 / 上次同步</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {tableItems.map((it) => {
            if (it.kind === 'group') {
              return (
                <tr key={it.key} className="group-row"
                    onClick={() => toggleGroup(it.label)}>
                  <td colSpan={10}>
                    <span className="faint">{collapsed.has(it.label) ? '▶' : '▼'}</span>{' '}
                    <b>{it.label || '未分组'}</b>
                    <span className="faint">　{it.count} 条</span>
                  </td>
                </tr>
              );
            }
            const r = it.row;
            return (
            <tr key={r.case_id} className={`clickable ${detailId === r.case_id ? 'on' : ''}`}
                onClick={() => setDetailId(detailId === r.case_id ? null : r.case_id)}>
              <td onClick={(e) => e.stopPropagation()}>
                <input type="checkbox" aria-label={`选择 ${r.case_id}`}
                       disabled={!caseIsSelectable(r)}
                       checked={selected.includes(r.case_id)}
                       onChange={(e) => setSelected((prev) => e.target.checked
                         ? [...prev, r.case_id] : prev.filter((c) => c !== r.case_id))} />
              </td>
              <td>{r.zentao_case_id
                ? <span className="hstack"><CopyId text={r.case_id} /><span className="faint">#{r.zentao_case_id}</span></span>
                : <CopyId text={r.case_id} />}</td>
              <td>{r.title || <span className="faint">（无标题）</span>}</td>
              <td className="faint">{r.module_name
                ? <span className="chip">{r.module_name}</span>
                : '—'}</td>
              <td className="num">{r.version === 0 ? '草稿' : `v${r.version}`}</td>
              <td><Badge status={r.sync_status} /></td>
              <td><Badge status={r.status} /></td>
              <td className="num">{r.steps?.length ?? 0}</td>
              <td className="mono muted" title={r.updated_at ?? ''}>
                {fmtTime(r.updated_at)}
                {r.last_synced_at && (
                  <div className="faint" style={{ fontSize: 'var(--fs-xs)' }}>
                    同步 {fmtTime(r.last_synced_at)}
                  </div>
                )}
              </td>
              <td onClick={(e) => e.stopPropagation()}>
                <button className="btn"
                  disabled={!caseIsSelectable(r) || (locked && !r.steps?.length)}
                  onClick={() => r.steps?.length
                    ? review([r.case_id])
                    : void analyze([r.case_id])}>
                  {r.status === 'deprecated'
                    ? '已停用'
                    : r.status === 'rejected'
                      ? '已拒绝'
                      : r.steps?.length ? '审核 / 编辑' : '生成 DSL'}
                </button>
              </td>
            </tr>
          );
          })}
        </tbody>
      </table>
      {rows.length === 0 && !list.loading && <Empty text="本地还没有用例：输入禅道用例 ID 拉取，或先做增量同步" />}

      {detailId && (
        <div className="panel" style={{ marginTop: 12 }}>
          <h3>
            用例详情 · <CopyId text={detailId} />
            {detail.data && (
              <span className="hstack" style={{ marginLeft: 8 }}>
                <Badge status={detail.data.status} />
                <Badge status={detail.data.sync_status} />
                <span className="faint">
                  {detail.data.version === 0 ? '未生效' : `v${detail.data.version}`}
                  {detail.data.step_count !== undefined ? ` · ${detail.data.step_count} 步` : ''}
                </span>
              </span>
            )}
          </h3>
          {detail.error && <Msg kind="error">{detail.error}</Msg>}
          {detail.data && (
            <div className="row split">
              <div className="left">
                <div className="faint" style={{ marginBottom: 4 }}>禅道原文步骤</div>
                <pre className="mono" style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{detail.data.source_text || '（无原文）'}</pre>
                <div style={{ marginTop: 8 }}>
                  <div className="faint">前置条件</div>
                  <Chips items={detail.data.preconditions} />
                </div>
              </div>
              <div className="right">
                <div className="faint" style={{ marginBottom: 4 }}>
                  DSL 步骤{detail.data.uncertain_steps?.length ? `（不确定 ${detail.data.uncertain_steps.length} 步，待人审）` : ''}
                </div>
                <table className="grid">
                  <thead>
                    <tr><th className="num">#</th><th>动作</th><th>目标</th><th>原文对照</th><th>校验</th></tr>
                  </thead>
                  <tbody>
                    {(detail.data.steps as Record<string, never>[]).map((s, i) => {
                      const step = s as unknown as {
                        id: string; action: string; source_text?: string; uncertain?: boolean;
                        target?: { key?: string }; verify?: { type?: string; expect?: string };
                      };
                      return (
                        <tr key={step.id ?? i}>
                          <td className="num">{i + 1}</td>
                          <td className="mono">
                            {step.action}
                            {step.uncertain ? <span className="badge blocked" style={{ marginLeft: 4 }}>待核</span> : null}
                          </td>
                          <td className="mono faint">{step.target?.key ?? '—'}</td>
                          <td className="faint">{step.source_text ?? '—'}</td>
                          <td className="mono faint">
                            {step.verify?.type && step.verify.type !== 'none'
                              ? `${step.verify.type}${step.verify.expect ? `: ${step.verify.expect}` : ''}` : '—'}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
                {(detail.data.steps as unknown[]).length === 0 && <Empty text="尚无 DSL：点行尾『生成 DSL』，或勾选用例进行批量分析" />}
                <div style={{ marginTop: 8 }} className="hstack">
                  <button className="btn primary" onClick={() => navigate('dsl', { caseId: detailId })}>
                    {detail.data.steps.length ? '审核 / 编辑 DSL' : '查看并生成 DSL'}
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
