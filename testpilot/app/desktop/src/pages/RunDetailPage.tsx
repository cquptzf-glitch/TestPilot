/** RunDetail · 执行详情（设计 14.3）：上下文条 + 三 Tab（概览 / 用例明细 / 轨迹回放）。
 *
 *  轨迹回放是该页的核心：左时间线（含禅道原文对照）+ 右详情（错误块 / before-after 双截图 /
 *  模拟器指令回执与状态快照 / 相似历史证据链）。运行中通过引擎事件增量刷新。
 */

import { useEffect, useMemo, useRef, useState } from 'react';

import { assetPath, call, readAssetDataUrl, readTextFile, shotRel } from '../bridge';
import { Badge, CountCard, Donut, DriverBadge, Empty, HeatBlocks, Msg, PassBar, SplitBar } from '../components/Bits';
import { Lightbox } from '../components/Ui';
import { fmtMs, fmtTime, passRate, shortSig, copyText, statusLabel } from '../format';
import { msg, useAsync, useEngineEvents, type AsyncState, type EngineContextValue } from '../hooks';
import type {
  CaseCtxEntry, CaseExecRow, DriverStats, EnvCompatEntry, RetryAttempt,
  RunCompare, RunDetail, RunRow, ShotDiffResult, StabilityEntry, StepTrace,
} from '../types';

type Tab = 'overview' | 'cases' | 'trace';

export function RunDetailPage({ runId, engine, initialCaseExec, initialStepNo }: {
  runId: number;
  engine: EngineContextValue;
  initialCaseExec?: string;
  initialStepNo?: string;
}) {
  const [tab, setTab] = useState<Tab>(initialCaseExec ? 'trace' : 'overview');
  const [caseExecId, setCaseExecId] = useState<number | null>(
    initialCaseExec ? Number(initialCaseExec) : null);
  const [stepNo, setStepNo] = useState<number | null>(
    initialStepNo ? Number(initialStepNo) : null);
  // 复跑用例的第几次执行（null = 默认看最后一次；首次失败往往才是要查的那次）
  const [attemptNo, setAttemptNo] = useState<number | null>(null);
  const [traceId, setTraceId] = useState<number | null>(null);
  useEffect(() => setTraceId(null), [caseExecId, attemptNo, runId]);
  const [report, setReport] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ kind: 'error' | 'info'; text: string } | null>(null);
  // 截图区：并列对照 / 差异高亮（差异图由引擎预计算并缓存）
  const [view, setView] = useState<'split' | 'diff'>('split');
  const [diffBusy, setDiffBusy] = useState(false);
  const [shotDiff, setShotDiff] = useState<ShotDiffResult | null>(null);
  const [zoom, setZoom] = useState<{ src: string; caption: string } | null>(null);
  const openZoom = (src: string, caption: string) => setZoom({ src, caption });

  const detail = useAsync(() => call<RunDetail>('run.detail', { runId }), [runId]);

  // 截图根目录来自壳已授权的资源根目录（引擎 health.paths）；未就绪时不渲染截图
  const shotRoot = engine.health?.paths?.screenshot_root;

  // 运行中增量刷新：每条事件全量 run.detail 会重算稳定性+签名画像，必须节流；
  // run 收尾（phase=finished）立即刷新拿最终统计，避免被节流窗口吞掉
  const lastReload = useRef(0);
  useEngineEvents(['step.finished', 'case.finished', 'run.progress'], (_ev, payload) => {
    const p = payload as { run_id?: number; phase?: string };
    if (p?.run_id !== runId) return;
    if (p.phase === 'finished' || Date.now() - lastReload.current >= 2000) {
      lastReload.current = Date.now();
      detail.reload();
      steps.reload();
    }
  });

  // 切换 run（深链/对比面板跳转）时重置内部定位，别把上个 run 的用例/步骤带过来
  useEffect(() => {
    setCaseExecId(initialCaseExec ? Number(initialCaseExec) : null);
    setStepNo(initialStepNo ? Number(initialStepNo) : null);
    setAttemptNo(null);
    setView('split');
    setShotDiff(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  const run = detail.data;
  const cases = run?.cases ?? [];
  const activeExec = cases.find((c) => c.id === caseExecId) ?? null;

  // env_compat 矩阵：同一方案/用例集下的历史版本对照（不传版本 = 各版本全要）
  const envCompat = useAsync(
    () => (run
      ? call<{ entries: EnvCompatEntry[] }>('kb.envCompat', {
          suiteId: run.env.suite_id ?? undefined,
          caseIds: run.env.suite_id == null ? run.env.case_ids : undefined,
        })
      : Promise.resolve({ entries: [] })),
    [run?.id],
  );
  // 当前用例的人审上下文（case_ctx）：轨迹页直接看到"人已经知道的事"
  const caseCtx = useAsync(
    () => (activeExec
      ? call<{ entries: CaseCtxEntry[] }>('kb.caseCtxList',
                                          { caseId: activeExec.case_id })
      : Promise.resolve({ entries: [] })),
    [activeExec?.case_id],
  );
  const ctxEntry = caseCtx.data?.entries?.[0] ?? null;

  // 用例稳定性（跨 run 派生视图，随 run.detail 一起返回）：不额外发请求
  const stability: Record<string, StabilityEntry> = run?.stability ?? {};
  const flakyIds = useMemo(
    () => Object.entries(run?.stability ?? {})
      .filter(([, s]) => s.level === 'flaky').map(([cid]) => cid),
    [run?.stability]);

  // 回归对比：默认取**同一任务的上一次 run** —— 回归收尾最常问的就是"比上次退步了吗"
  const runList = useAsync(() => call<{ runs: RunRow[] }>('run.list', { limit: 100, taskId: run?.task_id }),
                           [run?.task_id]);
  const baselines = useMemo(
    () => (runList.data?.runs ?? []).filter(
      (r) => r.task_id === run?.task_id && r.id < (run?.id ?? 0) && r.status === 'completed'),
    [runList.data, run?.task_id, run?.id]);
  const [baseRunId, setBaseRunId] = useState<number | null>(null);
  const effectiveBase = baseRunId ?? baselines[0]?.id ?? null;
  const compare = useAsync(
    () => (run && effectiveBase
      ? call<RunCompare>('run.compare', { baseRunId: effectiveBase, targetRunId: run.id })
      : Promise.resolve(null)),
    [run?.id, effectiveBase]);

  const steps = useAsync(
    () => (caseExecId
      ? call<{ steps: StepTrace[] }>('run.stepTrace', {
          caseExecId, attempt: attemptNo ?? undefined })
      : Promise.resolve({ steps: [] })),
    [caseExecId, attemptNo],
  );
  const stepList = steps.data?.steps ?? [];
  const activeStep = stepList.find((s) => s.id === traceId)
    ?? [...stepList].reverse().find((s) => s.step_no === stepNo)
    ?? stepList[0] ?? null;
  const activeIndex = stepList.findIndex((s) => s.id === activeStep?.id);
  const selectTrace = (s: StepTrace | undefined) => {
    if (s) { setTraceId(s.id); setStepNo(s.step_no); }
  };

  // 慢步骤线索：定位/动作耗时超阈值（性能问题在报告里最容易漏，这里直接列出来）
  const slowestSteps = useMemo(() => {
    if (stepList.length === 0) return [];
    const rows = cases.flatMap((c) => {
      if (c.id !== caseExecId) return [];
      return stepList
        .map((s) => ({ caseId: c.case_id, step: s, total: (s.locate_ms ?? 0) + (s.act_ms ?? 0) }))
        .filter((r) => r.total >= 3000);
    });
    return rows.sort((a, b) => b.total - a.total).slice(0, 5);
  }, [cases, caseExecId, stepList]);

  // 未显式指定时默认落在"最需要看的"那条用例上（失败 > 阻塞 > 第一条）
  useEffect(() => {
    if (caseExecId !== null || cases.length === 0) return;
    const target = cases.find((c) => c.status === 'fail' || c.status === 'blocked') ?? cases[0];
    setCaseExecId(target.id);
  }, [cases, caseExecId]);

  // 轨迹页键盘流：J/K 步进（与 KBReview 的队列导航同一套手感）
  useEffect(() => {
    if (tab !== 'trace' || stepList.length === 0) return;
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName;
      if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
      const idx = stepList.findIndex((s) => s.id === activeStep?.id);
      if (e.key === 'j' || e.key === 'J') {
        e.preventDefault();
        selectTrace(stepList[Math.min(idx + 1, stepList.length - 1)]);
      } else if (e.key === 'k' || e.key === 'K') {
        e.preventDefault();
        selectTrace(stepList[Math.max(idx - 1, 0)]);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [tab, stepList, activeStep?.id]);

  const pr = passRate(run?.stats);
  const rootCauses = useMemo(() => {
    const out: Record<string, number> = {};
    for (const c of cases) {
      const cat = c.root_cause?.category;
      if (cat) out[cat] = (out[cat] ?? 0) + 1;
    }
    return out;
  }, [cases]);

  // 耗时统计：均耗时容易被单个长用例带偏，所以中位数一起给（排查性能看中位更有意义）
  const durations = useMemo(
    () => cases.map((c) => c.duration_ms ?? 0).filter((n) => n > 0).sort((a, b) => a - b),
    [cases]);
  const avgMs = durations.length > 0
    ? Math.round(durations.reduce((a, b) => a + b, 0) / durations.length) : null;
  const medianMs = durations.length > 0
    ? durations[Math.floor(durations.length / 2)] : null;

  const openCase = (c: CaseExecRow) => {
    setCaseExecId(c.id);
    setStepNo(null);
    setAttemptNo(null);          // 换用例时回到"最后一次尝试"，避免停在别的用例的第 N 次
    setTab('trace');
  };

  // 对比面板里的用例可能不在本次 run（如"已修复"只在上一次跑过）—— 找不到就不跳，避免空转
  const openCaseById = (caseId: string) => {
    const target = cases.find((c) => c.case_id === caseId);
    if (target) openCase(target);
  };

  // 切到「差异高亮」时才请引擎算差异（首次要解码两张整屏 PNG，没必要预取）
  const wantsDiff = view === 'diff';
  useEffect(() => {
    if (!wantsDiff || !activeStep) return;
    let alive = true;
    setDiffBusy(true);
    setShotDiff(null);
    call<ShotDiffResult>('run.shotDiff', {
      runId: run?.id ?? runId,
      caseExecId: activeStep.case_exec_id,
      stepNo: activeStep.step_no,
      attempt: activeStep.attempt ?? attemptNo ?? undefined,
      traceId: activeStep.id,
    })
      .then((out) => { if (alive) setShotDiff(out); })
      .catch((e) => {
        if (alive) setShotDiff({ rel: null, cached: false, stats: {}, detail: msg(e) });
      })
      .finally(() => { if (alive) setDiffBusy(false); });
    return () => { alive = false; };
  }, [wantsDiff, activeStep?.case_exec_id, activeStep?.step_no, activeStep?.id, activeStep?.attempt, attemptNo, run?.id, runId]);

  const genReport = async () => {
    setBusy(true); setNotice(null);
    try {
      const out = await call<{ path: string }>('run.report', { runId });
      const text = await readTextFile(out.path);
      setReport(text);
      setNotice({ kind: 'info', text: `报告已生成：${out.path}` });
    } catch (e) {
      setNotice({ kind: 'error', text: `生成/读取报告失败：${(e as Error).message ?? e}` });
    } finally { setBusy(false); }
  };

  if (detail.error) return <div className="main"><Msg kind="error">读取 run 详情失败：{detail.error}</Msg></div>;
  if (!run) return <div className="main"><Empty text="加载中…" /></div>;

  return (
    <div className="main wide">
      {/* Run 上下文条 */}
      <div className="panel" style={{ marginBottom: 10 }}>
        <div className="hstack" style={{ gap: 12, flexWrap: 'wrap' }}>
          <strong>run #{run.id}</strong>
          <span>{run.env.task_name ?? `任务#${run.task_id}`}</span>
          <Badge status={run.status} />
          <PassBar pass={pr.pass} fail={pr.fail} blocked={pr.blocked} skip={pr.skip} total={pr.total} />
          {(run.stats.retry_cases ?? 0) > 0 && (
            <span className="chip" title="失败用例复跑（flake 过滤）：复跑后通过的判为疑似偶发">
              复跑 {run.stats.retry_cases} 例（通过 {run.stats.retry_passed ?? 0}）
            </span>
          )}
          <span className="spacer" />
          <span className="muted mono" title={run.started_at}>
            {fmtTime(run.started_at)} ~ {fmtTime(run.finished_at)}
          </span>
          <button className="btn" onClick={() => detail.reload()}>刷新</button>
          <details className="inline-more">
            <summary className="btn ghost">更多</summary>
            <div className="more-actions-popover">
              <button className="btn" title="复制可直开的深链（含当前用例/步骤定位）"
                      onClick={() => copyText(deepLink(run.id, caseExecId, stepNo))}>
                复制当前深链
              </button>
              <button className="btn" disabled={busy} onClick={genReport}>
                重新生成文本报告
              </button>
            </div>
          </details>
        </div>
      </div>

      {notice && <Msg kind={notice.kind}>{notice.text}</Msg>}

      {zoom && <Lightbox src={zoom.src} caption={zoom.caption} onClose={() => setZoom(null)} />}

      <div className="tabs">
        <button className={tab === 'overview' ? 'on' : ''} onClick={() => setTab('overview')}>概览</button>
        <button className={tab === 'cases' ? 'on' : ''} onClick={() => setTab('cases')}>用例明细（{cases.length}）</button>
        <button className={tab === 'trace' ? 'on' : ''} onClick={() => setTab('trace')}>轨迹回放</button>
      </div>

      {tab === 'overview' && (
        <div className="col">
          <div className="row" style={{ alignItems: 'stretch' }}>
            <div className="panel" style={{ display: 'flex', alignItems: 'center',
                                            gap: 'var(--sp-6)' }}>
              <Donut rate={pr.rate} pass={pr.pass} total={pr.total} size={104} />
              <div className="stack" style={{ gap: 'var(--sp-2)' }}>
                <div className="legend">
                  <span><i style={{ background: 'var(--s-pass)' }} />通过 {pr.pass}</span>
                  <span><i style={{ background: 'var(--s-fail)' }} />失败 {pr.fail}</span>
                  <span><i style={{ background: '#e0a33a' }} />阻塞 {pr.blocked}</span>
                  <span><i style={{ background: '#b9c1cb' }} />跳过 {pr.skip}</span>
                </div>
                <span className="faint" style={{ fontSize: 'var(--fs-sm)' }}>
                  用例总数 {pr.total}；均耗时 {fmtMs(avgMs)}；中位 {fmtMs(medianMs)}
                </span>
                <PassBar pass={pr.pass} fail={pr.fail} blocked={pr.blocked}
                         skip={pr.skip} total={pr.total} />
              </div>
            </div>
            <div className="cards" style={{ flex: 1 }}>
              <CountCard value={run.stats.kb_hit_cases ?? 0} label="KB 命中" tone="accent"
                         sub={pr.total > 0
                           ? `${Math.round(((run.stats.kb_hit_cases ?? 0) / pr.total) * 100)}% 用例`
                           : undefined}
                         title="命中知识库（失败库/元素库）的用例数" />
              <CountCard value={run.stats.llm_usage?.calls ?? 0} label="LLM 调用" tone="accent"
                         sub={`${(run.stats.llm_usage?.tokens ?? 0).toLocaleString()} token`} />
              <CountCard value={run.stats.llm_usage?.errors ?? 0} label="LLM 失败"
                         tone={(run.stats.llm_usage?.errors ?? 0) > 0 ? 'blocked' : 'skip'} />
              <CountCard value={slowestSteps.length} label="慢步骤(>3s)"
                         tone={slowestSteps.length > 0 ? 'blocked' : 'skip'}
                         title="定位+动作耗时超过 3 秒的步骤数（性能热点线索）" />
              <CountCard value={flakyIds.length} label="抖动用例"
                         tone={flakyIds.length > 0 ? 'blocked' : 'skip'}
                         sub={flakyIds.length > 0
                           ? flakyIds.slice(0, 2).join('、')
                           : '窗口内无翻转'}
                         title="最近窗口内既有通过也有失败的用例（疑似偶发；详见用例明细的稳定性列）" />
            </div>
          </div>

          <RunComparePanel baselines={baselines} baseRunId={effectiveBase}
                           onSelect={setBaseRunId} compare={compare}
                           caseIds={new Set(cases.map((c) => c.case_id))}
                           onOpenCase={openCaseById} />

          <div className="row split">
            <div className="left" style={{ flex: '0 0 440px' }}>
              <div className="panel">
                <h3>根因分布
                  <span className="spacer" />
                  <span className="faint" style={{ fontWeight: 400 }}>
                    共 {Object.values(rootCauses).reduce((a, b) => a + b, 0)} 条
                  </span>
                </h3>
                <HeatBlocks
                  emptyText="本次没有归因结果（或全部通过）"
                  items={Object.entries(rootCauses).map(([k, n]) => ({
                    key: k, value: n, title: `${k}: ${n} 次`,
                  }))}
                />
              </div>

              <DriverStatsPanel stats={run.stats.drivers} />

              {slowestSteps.length > 0 && (
                <div className="panel">
                  <h3>最慢步骤 TOP {slowestSteps.length}</h3>
                  <table className="grid">
                    <thead>
                      <tr><th>用例</th><th className="num">步骤</th><th>动作</th>
                        <th>耗时拆解</th><th>状态</th></tr>
                    </thead>
                    <tbody>
                      {slowestSteps.map((row) => (
                        <tr key={`${row.caseId}-${row.step.step_no}`} className="clickable"
                            onClick={() => {
                              const target = cases.find((c) => c.case_id === row.caseId);
                              if (target) {
                                setCaseExecId(target.id);
                                setStepNo(row.step.step_no);
                                setTab('trace');
                              }
                            }}>
                          <td className="mono">{row.caseId}</td>
                          <td className="num mono">#{row.step.step_no}</td>
                          <td className="mono">{row.step.action}</td>
                          <td>
                            <SplitBar segments={[
                              { label: '定位', value: row.step.locate_ms ?? 0,
                                color: 'var(--c-accent)' },
                              { label: '动作', value: row.step.act_ms ?? 0, color: '#7aa7e0' },
                            ]} />
                          </td>
                          <td><Badge status={stepStatus(row.step)} /></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
            <div className="right">
              <div className="panel">
                <h3>环境快照（归因可比性）</h3>
                <dl className="kv">
                  <dt>机器</dt><dd>{run.env.machine ?? '—'}</dd>
                  <dt>平台</dt><dd>{run.env.platform ?? '—'}</dd>
                  <dt>Python</dt><dd>{run.env.python ?? '—'}</dd>
                  <dt>屏幕</dt><dd>{run.env.screen ?? '—'}</dd>
                  <dt>DPI</dt><dd>{run.env.dpi
                    ? `${(run.env.dpi.scale ?? 1).toFixed(2)}x / 感知 ${run.env.dpi.aware ? '是' : '否'} / 坐标空间 ${run.env.dpi.space ?? '—'}`
                    : '—'}</dd>
                  <dt>显示器</dt><dd>{(run.env.dpi?.monitors ?? []).map((m) => `#${m.index} ${m.rect.join(',')}${m.primary ? '(主)' : ''}`).join('；') || '—'}</dd>
                  <dt>DSL 版本集</dt><dd>{Object.entries(run.env.dsl_versions ?? {}).map(([k, v]) => `${k}:v${v}`).join('，') || '—'}</dd>
                  <dt>禅道回写</dt><dd>{run.stats.writeback ?? '—'}</dd>
                  <dt>LLM</dt><dd>调用 {run.stats.llm_usage?.calls ?? 0} 次 / token {run.stats.llm_usage?.tokens ?? 0} / 失败 {run.stats.llm_usage?.errors ?? 0}</dd>
                </dl>
                {run.env.dpi && run.env.dpi.aware === false && (run.env.dpi.scale ?? 1) !== 1 && (
                  <Msg kind="warn">本次运行 DPI 感知未开启且缩放非 100%：坐标已按缩放比补偿，建议排查 SetProcessDpiAwareness</Msg>
                )}
              </div>
              <EnvCompatPanel entries={envCompat.data?.entries ?? []} />
            </div>
          </div>

          {report && (
            <div className="panel">
              <h3>报告内容</h3>
              <pre className="mono pre-wrap" style={{ maxHeight: '40vh', overflow: 'auto', margin: 0 }}>{report}</pre>
            </div>
          )}
        </div>
      )}

      {tab === 'cases' && (
        <>
          <table className="grid">
            <thead>
              <tr><th>用例</th><th className="num">DSL</th><th>状态</th><th className="num">耗时</th>
                <th>稳定性</th><th>失败签名</th><th className="num">KB</th><th>根因</th></tr>
            </thead>
            <tbody>
              {cases.map((c) => (
                <tr key={c.id} className={`clickable ${c.status === 'fail' ? 'row-fail' : c.status === 'blocked' ? 'row-blocked' : ''}`}
                    onClick={() => openCase(c)}>
                  <td className="mono">{c.case_id}<span className="faint"> #{c.id}</span></td>
                  <td className="num">v{c.dsl_version}</td>
                  <td>
                    <span className="hstack" style={{ gap: 6 }}>
                      <Badge status={c.status} />
                      {(c.attempts ?? 1) > 1 && (
                        <span className="chip" title={retryTooltip(c)}>复跑{c.attempts}次</span>
                      )}
                    </span>
                  </td>
                  <td className="num mono muted">{fmtMs(c.duration_ms)}</td>
                  <td><StabilityCell entry={stability[c.case_id]} /></td>
                  <td className="mono" title={c.failure_signature ?? ''}>{shortSig(c.failure_signature, 18)}</td>
                  <td className="num">{c.kb_hit_id ? c.kb_hit_id : '—'}</td>
                  <td className="mono faint">{c.root_cause?.category ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {cases.length === 0 && <Empty text="本次 run 没有用例记录" />}
        </>
      )}

      {tab === 'trace' && (
        <>
          {cases.length === 0 ? <Empty text="没有可回放的用例" /> : (
            <div className="toolbar">
              <span className="muted">用例</span>
              <select value={caseExecId ?? activeCaseId(cases)} onChange={(e) => { setCaseExecId(Number(e.target.value)); setStepNo(null); setAttemptNo(null); }}>
                {cases.map((c) => <option key={c.id} value={c.id}>{c.case_id}（{c.status}）</option>)}
              </select>
              {(activeExec?.attempts ?? 1) > 1 && (
                <>
                  <span className="muted">第几次执行</span>
                  <div className="segmented">
                    {Array.from({ length: activeExec?.attempts ?? 1 }, (_, i) => i + 1).map((n) => (
                      <button key={n}
                              className={(attemptNo ?? (activeExec?.attempts ?? 1)) === n ? 'on' : ''}
                              title={attemptHint(activeExec, n)}
                              onClick={() => { setAttemptNo(n); setStepNo(null); }}>
                        {n}
                      </button>
                    ))}
                  </div>
                </>
              )}
              {activeExec?.failure_signature && <span className="mono faint">{activeExec.failure_signature}</span>}
              {activeExec?.kb_hit_id && <span className="chip">KB 命中 #{activeExec.kb_hit_id}</span>}
              <span className="spacer" />
              <button className="btn" disabled={!activeStep || activeIndex <= 0}
                      title="上一步（K）"
                      onClick={() => {
                        const idx = stepList.findIndex((s) => s.id === activeStep?.id);
                        if (idx > 0) selectTrace(stepList[idx - 1]);
                      }}>上一步</button>
              <button className="btn" disabled={!activeStep || activeIndex >= stepList.length - 1}
                      title="下一步（J）"
                      onClick={() => {
                        const idx = stepList.findIndex((s) => s.id === activeStep?.id);
                        if (idx >= 0 && idx < stepList.length - 1) selectTrace(stepList[idx + 1]);
                      }}>下一步</button>
              <button className="btn" onClick={() => steps.reload()}>刷新轨迹</button>
            </div>
          )}

          {activeExec && (
            <div className="row split" style={{ alignItems: 'flex-start' }}>
              <div className="left" style={{ flex: '0 0 42%' }}>
                {/* 回放条：分段宽度 = 步骤耗时占比，颜色 = 结果；点分段直接跳步 */}
                {stepList.length > 0 && (
                  <div className="replay-bar" aria-label="步骤耗时比例">
                    {stepList.map((s) => {
                      const ms = (s.locate_ms ?? 0) + (s.act_ms ?? 0);
                      const st = stepStatus(s);
                      return (
                        <button key={s.id}
                                className={`replay-seg ${st} ${s.id === activeStep?.id ? 'on' : ''}`}
                                style={{ flex: `${Math.max(ms, 80)} 1 0` }}
                                title={`#${s.step_no} ${s.action || s.step_id} · ${fmtMs(ms)}（${statusLabel(st)}）`}
                                onClick={() => selectTrace(s)} />
                      );
                    })}
                  </div>
                )}
                <div className="timeline">
                  {stepList.map((s) => (
                    <div key={s.id}>
                      <div className={`tl-row ${tlClass(s)} ${s.id === activeStep?.id ? 'on' : ''}`}
                           onClick={() => selectTrace(s)}>
                        <span className="no">#{s.step_no}</span>
                        <span className="act">{s.action || s.step_id}</span>
                        <DriverBadge driver={s.driver} degradedFrom={degradedFrom(s)} />
                        <span className="ms">{fmtMs((s.locate_ms ?? 0) + (s.act_ms ?? 0))}</span>
                        <Badge status={stepStatus(s)} />
                      </div>
                      {s.source_text && <div className="tl-source">└ 原文：{s.source_text}</div>}
                    </div>
                  ))}
                  {stepList.length === 0 && <Empty text="该用例没有步骤轨迹" />}
                </div>
              </div>

              <div className="right">
                {!activeStep ? <Empty text="左侧选择一步查看详情" /> : (
                  <div className="col">
                    <div className="panel">
                      <h3>
                        步骤 #{activeStep.step_no} · {activeStep.action}
                        <span style={{ marginLeft: 8 }}>
                          <DriverBadge driver={activeStep.driver} />
                        </span>
                        <span className="faint" style={{ marginLeft: 8 }}>
                          定位 {fmtMs(activeStep.locate_ms)} / 动作 {fmtMs(activeStep.act_ms)}
                        </span>
                      </h3>
                      <dl className="kv">
                        <dt>step_id</dt><dd>{activeStep.step_id}</dd>
                        <dt>原文对照</dt><dd>{activeStep.source_text || '—'}</dd>
                        <dt>目标描述符</dt><dd>{activeStep.target ? JSON.stringify(activeStep.target) : '—'}</dd>
                        <dt>校验</dt><dd>{activeStep.verify_result ?? '—'}{activeStep.verify_detail ? ` · ${activeStep.verify_detail}` : ''}</dd>
                        <dt>时间</dt><dd title={activeStep.at}>{fmtTime(activeStep.at)}</dd>
                      </dl>
                    </div>

                    {ctxEntry && activeExec && (
                      <div className="panel">
                        <h3>用例上下文（人审维护）
                          <span className="spacer" />
                          <button className="btn sm"
                                  onClick={() => window.location.assign(
                                    `#/kb?tab=ctx&caseId=${encodeURIComponent(activeExec.case_id)}`)}>
                            去维护
                          </button>
                        </h3>
                        <dl className="kv">
                          <dt>场景</dt><dd>{ctxEntry.payload.scene || '—'}</dd>
                          <dt>已知坑</dt><dd>{(ctxEntry.payload.pitfalls ?? []).join('；') || '—'}</dd>
                          <dt>备注</dt><dd>{ctxEntry.payload.notes || '—'}</dd>
                        </dl>
                      </div>
                    )}

                    {activeStep.error && (
                      <div className="panel">
                        <h3>失败信息</h3>
                        <Badge status={activeStep.error.type === 'verify_failed' ? 'fail' : 'blocked'} text={activeStep.error.type ?? 'error'} />
                        <pre className="mono" style={{ whiteSpace: 'pre-wrap', margin: '6px 0 0' }}>{JSON.stringify(activeStep.error, null, 2)}</pre>
                      </div>
                    )}

                    <div className="panel">
                      <h3>
                        截图对照
                        <span className="spacer" />
                        <span className="faint" style={{ fontWeight: 400 }}>
                          {diffHint(shotDiff)}
                        </span>
                      </h3>
                      <div className="shots">
                        <Shot root={shotRoot} runId={run.id} caseExecId={activeStep.case_exec_id}
                              stepNo={activeStep.step_no} phase="before" onZoom={openZoom}
                              relOverride={activeStep.screenshot_before_path ?? undefined} />
                        <Shot root={shotRoot} runId={run.id} caseExecId={activeStep.case_exec_id}
                              stepNo={activeStep.step_no} phase="after" onZoom={openZoom}
                              relOverride={activeStep.screenshot_path ?? undefined} />
                        {wantsDiff && (
                          <Shot root={shotRoot} runId={run.id} caseExecId={activeStep.case_exec_id}
                                stepNo={activeStep.step_no} phase="diff" onZoom={openZoom} />
                        )}
                      </div>
                      <div className="hstack" style={{ marginTop: 'var(--sp-4)' }}>
                        <div className="segmented">
                          <button className={!wantsDiff ? 'on' : ''}
                                  onClick={() => setView('split')}>并列对照</button>
                          <button className={wantsDiff ? 'on' : ''}
                                  onClick={() => setView('diff')}>差异高亮</button>
                        </div>
                        {wantsDiff && diffBusy && <span className="faint">计算差异…</span>}
                      </div>
                    </div>

                    {activeStep.receipt && (
                      <div className="panel">
                        <h3>
                          设备指令回执
                          <span className="spacer" />
                          <Badge status={activeStep.receipt.ok === false ? 'fail' : 'pass'}
                                 text={activeStep.receipt.ok === false ? '失败' : '成功'} />
                        </h3>
                        <dl className="kv">
                          <dt>指令</dt>
                          <dd className="mono">{activeStep.receipt.cmd ?? '—'}</dd>
                          <dt>耗时</dt>
                          <dd>{activeStep.receipt.ms != null ? fmtMs(activeStep.receipt.ms) : '—'}</dd>
                          {activeStep.receipt.error && (
                            <>
                              <dt>错误</dt><dd>{activeStep.receipt.error}</dd>
                            </>
                          )}
                        </dl>
                      </div>
                    )}

                    {activeStep.device_state !== null && activeStep.device_state !== undefined && (
                      <div className="panel">
                        <h3>模拟器状态快照（device_state_json）</h3>
                        <pre className="mono" style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{JSON.stringify(activeStep.device_state, null, 2)}</pre>
                      </div>
                    )}

                    {activeExec?.root_cause && (
                      <div className="panel">
                        <h3>归因与证据链</h3>
                        <dl className="kv">
                          <dt>根因</dt><dd>{activeExec.root_cause.category ?? '—'}（来源 {activeExec.root_cause.source ?? '—'}，置信 {activeExec.root_cause.confidence ?? '—'}）{activeExec.root_cause.needs_review ? ' ⚠ 待人审' : ''}</dd>
                          <dt>依据</dt><dd>{activeExec.root_cause.evidence ?? '—'}</dd>
                          <dt>建议</dt><dd>{activeExec.root_cause.suggestion ?? '—'}</dd>
                        </dl>
                        {(activeExec.root_cause.similar?.length ?? 0) > 0 && (
                          <div style={{ marginTop: 6 }}>
                            <div className="faint">
                              相似历史（同族签名在本知识库中的出现次数）
                            </div>
                            <table className="grid">
                              <thead>
                                <tr>
                                  <th>签名</th><th>分类</th>
                                  <th className="num">命中</th><th>建议</th><th />
                                </tr>
                              </thead>
                              <tbody>
                                {activeExec.root_cause.similar!.map((s) => (
                                  <tr key={s.signature}>
                                    <td className="mono" title={s.signature}>{shortSig(s.signature, 20)}</td>
                                    <td className="muted">{s.category ?? '—'}</td>
                                    <td className="num mono">{s.hits ?? 0} 次</td>
                                    <td className="muted">{s.suggestion ?? '—'}</td>
                                    <td>
                                      {s.entry_id ? (
                                        <button className="btn sm" title="到知识库审阅这条历史"
                                                onClick={() => window.location.assign(`#/kb?id=${s.entry_id}`)}>
                                          审阅
                                        </button>
                                      ) : null}
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        )}
                        <div style={{ marginTop: 6 }}>
                          <button className="btn" onClick={() => window.location.assign('#/kb')}>去知识库审阅</button>
                        </div>
                      </div>
                    )}

                    {activeExec?.history && (
                      <div className="panel">
                        <h3>
                          快速定位（该签名历史）
                          <span className="spacer" />
                          <span className="faint" style={{ fontWeight: 400 }}>
                            出现 {activeExec.history.count} 次
                          </span>
                        </h3>
                        <dl className="kv">
                          <dt>根因分布</dt>
                          <dd>{activeExec.history.categories
                            .map((c) => `${c.category} ${c.count}`).join(' / ') || '—'}</dd>
                          <dt>最近一次</dt>
                          <dd>
                            run#{activeExec.history.last.run_id} / {activeExec.history.last.case_id}
                            {activeExec.history.last.step_no ? ` / step#${activeExec.history.last.step_no}` : ''}
                            {activeExec.history.last.at ? `（${fmtTime(activeExec.history.last.at)}）` : ''}
                          </dd>
                          <dt>建议</dt><dd>{activeExec.history.suggestion || '—'}</dd>
                        </dl>
                        <button className="btn sm" title="跳到最近一次出现该签名的执行"
                                onClick={() => {
                                  const last = activeExec.history?.last;
                                  if (!last?.run_id) return;
                                  window.location.assign(deepLink(
                                    last.run_id, last.case_exec_id ?? null, last.step_no ?? null));
                                }}>
                          看最近一次
                        </button>
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function activeCaseId(cases: CaseExecRow[]): number {
  return cases[0]?.id ?? 0;
}

/** 可直开的深链（设计 14.5：报告里可复制，打开即定位到具体步骤）。 */
function deepLink(runId: number, caseExecId: number | null, stepNo: number | null): string {
  const q = new URLSearchParams({ runId: String(runId) });
  if (caseExecId) q.set('caseExecId', String(caseExecId));
  if (stepNo) q.set('stepNo', String(stepNo));
  return `${window.location.origin}${window.location.pathname}#/run?${q.toString()}`;
}

/** 步骤状态是**推导**出来的：step_trace 不落 status 列，由 error/verify_result 反推
 *  （与 engine/executor.py 的判定口径一致，避免两处各写一套语义）。 */
export function stepStatus(s: StepTrace): string {
  if (s.status) return s.status;
  const errType = s.error?.type;
  if (errType === 'aborted') return 'skipped';
  if (errType === 'verify_failed') return 'fail';
  if (errType) return errType === 'device_error' ? 'blocked' : 'fail';
  if (s.verify_result === 'fail') return 'fail';
  if (s.verify_result === 'unknown') return 'blocked';
  return 'pass';
}

function tlClass(s: StepTrace): string {
  const st = stepStatus(s);
  if (st === 'fail') return 'fail';
  if (st === 'blocked') return 'blocked';
  return '';
}

/** 降级徽章来源：尝试链里第一个失败的驱动（设计 14.4 的 `uia→vis`）。 */
function degradedFrom(s: StepTrace): string | null {
  const chain = s.driver_chain ?? [];
  if (chain.length < 2 || !s.driver) return null;
  const failed = chain.find((a) => a.ok === false)?.driver;
  return failed && failed !== s.driver ? failed : null;
}

/** 驱动与降级统计（引擎 stats.drivers）：降级率 + 各驱动成败 + 降级路径。 */
function DriverStatsPanel({ stats }: { stats?: DriverStats }) {
  if (!stats?.steps) return null;
  const drivers = Object.entries(stats.by_driver ?? {});
  const rate = stats.downgrade_rate;
  return (
    <div className="panel">
      <h3>驱动与降级
        <span className="spacer" />
        <span className="faint" style={{ fontWeight: 400 }}>
          降级 {stats.degraded_steps ?? 0}/{stats.steps} 步
          {typeof rate === 'number' ? `（${(rate * 100).toFixed(1)}%）` : ''}
          {(stats.failed_steps ?? 0) > 0 ? ` · 全失败 ${stats.failed_steps} 步` : ''}
        </span>
      </h3>
      <table className="grid">
        <thead>
          <tr><th>驱动</th><th className="num">尝试</th><th className="num">成功</th>
            <th className="num">失败</th><th className="num">成功率</th>
            <th className="num">平均耗时</th></tr>
        </thead>
        <tbody>
          {drivers.map(([name, d]) => (
            <tr key={name}>
              <td><DriverBadge driver={name} /></td>
              <td className="num mono">{d.attempts ?? 0}</td>
              <td className="num mono">{d.success ?? 0}</td>
              <td className="num mono">{d.fail ?? 0}</td>
              <td className="num mono">
                {typeof d.success_rate === 'number'
                  ? `${(d.success_rate * 100).toFixed(0)}%` : '—'}
              </td>
              <td className="num mono muted">{fmtMs(d.avg_ms)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {(stats.transitions?.length ?? 0) > 0 && (
        <div className="hstack" style={{ marginTop: 8, flexWrap: 'wrap' }}>
          <span className="faint">降级路径：</span>
          {(stats.transitions ?? []).map((t) => (
            <span className="chip" key={`${t.from}-${t.to}`}
                  title={`${t.count} 次；其中最终成功 ${t.resolved ?? 0} 次`}>
              {t.from}→{t.to} ×{t.count}（成功 {t.resolved ?? 0}）
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

/** env_compat 通过率矩阵：行=用例、列=被测版本、格子=通过/次数（设计 9.1）。 */
function EnvCompatPanel({ entries }: { entries: EnvCompatEntry[] }) {
  if (entries.length === 0) return null;
  const versions = entries.map((e) => `${e.payload.test_app_version ?? '?'} · ${e.payload.driver_mode ?? '默认'}`);
  const caseIds = Array.from(new Set(
    entries.flatMap((e) => e.payload.case_ids ?? [])));
  return (
    <div className="panel">
      <h3>历史环境通过率（env_compat）
        <span className="spacer" />
        <span className="faint" style={{ fontWeight: 400 }}>
          按版本、DSL 与执行配置分别统计 · 格子=通过/次数
        </span>
      </h3>
      <table className="grid">
        <thead>
          <tr><th>用例</th>
            {versions.map((v, i) => <th key={`${v}-${i}`} className="num mono" title={entries[i].signature}>{v}</th>)}</tr>
        </thead>
        <tbody>
          {caseIds.map((cid) => (
            <tr key={cid}>
              <td className="mono">{cid}</td>
              {entries.map((e) => {
                const cell = e.payload.matrix?.[cid];
                const total = cell?.total ?? 0;
                const pass = cell?.pass ?? 0;
                const ratio = total > 0 ? pass / total : null;
                return (
                  <td key={e.id} className="num mono"
                      title={cell
                        ? `最近状态 ${cell.last_status ?? '—'}（run#${cell.last_run_id ?? '—'}）`
                        : '无记录'}
                      style={ratio === null ? undefined : {
                        color: ratio >= 0.9 ? 'var(--s-pass)'
                          : ratio >= 0.5 ? '#b57f18' : 'var(--s-fail)',
                      }}>
                    {total > 0 ? `${pass}/${total}` : '—'}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ---------------------------------------------------------------- 稳定性 / 回归对比 */

const STABILITY_LABEL: Record<string, string> = {
  stable: '稳定', flaky: '抖动', broken: '必现失败', unknown: '样本不足',
};
const STABILITY_BADGE: Record<string, string> = {
  stable: 'pass', flaky: 'blocked', broken: 'fail', unknown: 'skip',
};
const STATUS_COLOR: Record<string, string> = {
  pass: 'var(--s-pass)', fail: 'var(--s-fail)', error: 'var(--s-fail)',
  blocked: '#e0a33a', skip: '#b9c1cb',
};

/** 稳定性：结论徽章 + 最近执行条带 + 通过数；悬浮给出窗口/翻转/连续。 */
function StabilityCell({ entry }: { entry?: StabilityEntry }) {
  if (!entry || !entry.runs) return <span className="faint">—</span>;
  const tip = `最近 ${entry.runs} 次执行 ${entry.pass} 次通过（新→旧：${entry.recent.join(' ')}）`
    + (entry.retried ? `；其中 ${entry.retried} 次靠复跑才通过` : '')
    + `；翻转 ${entry.flips} 次；当前连续 ${entry.streak.len} 次${statusLabel(entry.streak.status)}`;
  return (
    <span className="hstack" style={{ gap: 'var(--sp-2)' }} title={tip}>
      <span className={`badge ${STABILITY_BADGE[entry.level] ?? 'skip'}`}>
        {STABILITY_LABEL[entry.level] ?? entry.level}
      </span>
      <span className="hstack" style={{ gap: 2 }}>
        {entry.recent.map((s, i) => (
          <i key={i} title={statusLabel(s)} style={{
            display: 'inline-block', width: 8, height: 8, borderRadius: 2,
            background: STATUS_COLOR[s] ?? '#8a94a3',
          }} />
        ))}
      </span>
      <span className="faint mono">{entry.pass}/{entry.runs}</span>
    </span>
  );
}

/** 回归对比：与基线 run 的用例级差异（新增失败 / 持续失败 / 已修复）。 */
function RunComparePanel({ baselines, baseRunId, onSelect, compare, caseIds, onOpenCase }: {
  baselines: RunRow[];
  baseRunId: number | null;
  onSelect: (id: number) => void;
  compare: AsyncState<RunCompare | null>;
  caseIds: Set<string>;
  onOpenCase: (caseId: string) => void;
}) {
  if (baselines.length === 0) return null;
  const data = compare.data;
  const rows = data ? [
    ...data.new_failures.map((c) => ({
      kind: '新增失败', tone: 'fail', caseId: c.case_id,
      base: c.base_status, target: c.target_status,
      note: c.signature ? shortSig(c.signature, 18) : '—',
    })),
    ...data.still_failing.map((c) => ({
      kind: '持续失败', tone: 'blocked', caseId: c.case_id,
      base: c.base_status, target: c.target_status,
      note: c.same_cause === true ? '同一失败原因'
        : c.same_cause === false ? '换了失败原因' : '—',
    })),
    ...data.fixed.map((c) => ({
      kind: '已修复', tone: 'pass', caseId: c.case_id,
      base: c.base_status, target: c.target_status,
      note: c.base_signature ? shortSig(c.base_signature, 18) : '—',
    })),
  ] : [];
  const shown = rows.slice(0, 12);
  return (
    <div className="panel">
      <h3>
        回归对比
        <span className="spacer" />
        <span className="muted" style={{ fontWeight: 400 }}>基线</span>
        <select value={baseRunId ?? ''} onChange={(e) => onSelect(Number(e.target.value))}>
          {baselines.map((r) => (
            <option key={r.id} value={r.id}>
              run #{r.id}（{fmtTime(r.started_at)} · {statusLabel(r.status)}）
            </option>
          ))}
        </select>
      </h3>
      {compare.error && <Msg kind="error">对比失败：{compare.error}</Msg>}
      {!data && !compare.error && <Empty text={compare.loading ? '对比中…' : '暂无对比数据'} />}
      {data && (
        <>
          <div className="hstack" style={{ gap: 8, flexWrap: 'wrap' }}>
            <span className="chip">
              通过率 {pct(data.base.pass_rate)} → {pct(data.target.pass_rate)}
              {data.pass_rate_delta !== null && (
                <b style={{ color: data.pass_rate_delta >= 0 ? 'var(--s-pass)' : 'var(--s-fail)' }}>
                  {data.pass_rate_delta >= 0 ? '+' : ''}{(data.pass_rate_delta * 100).toFixed(1)}%
                </b>
              )}
            </span>
            <span className="chip">新增失败 {data.counts.new_failures}</span>
            <span className="chip">持续失败 {data.counts.still_failing}</span>
            <span className="chip">已修复 {data.counts.fixed}</span>
            {(data.counts.added > 0 || data.counts.removed > 0) && (
              <span className="chip">用例增减 +{data.counts.added} / -{data.counts.removed}</span>
            )}
            {data.base.test_app_version !== data.target.test_app_version && (
              <span className="chip">
                被测版本 {data.base.test_app_version ?? '?'} → {data.target.test_app_version ?? '?'}
              </span>
            )}
          </div>
          {rows.length === 0 ? (
            <Empty text="两次运行的用例结果完全一致" />
          ) : (
            <table className="grid" style={{ marginTop: 'var(--sp-4)' }}>
              <thead>
                <tr><th>变化</th><th>用例</th><th>基线</th><th>本次</th><th>说明</th></tr>
              </thead>
              <tbody>
                {shown.map((r) => (
                  <tr key={`${r.kind}-${r.caseId}`}
                      className={caseIds.has(r.caseId) ? 'clickable' : ''}
                      title={caseIds.has(r.caseId) ? '在本次 run 中查看该用例' : '该用例不在本次 run 中'}
                      onClick={() => caseIds.has(r.caseId) && onOpenCase(r.caseId)}>
                    <td><span className={`badge ${r.tone}`}>{r.kind}</span></td>
                    <td className="mono">{r.caseId}</td>
                    <td><Badge status={r.base} /></td>
                    <td><Badge status={r.target} /></td>
                    <td className="mono faint">{r.note}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {rows.length > shown.length && (
            <div className="faint" style={{ marginTop: 6 }}>
              还有 {rows.length - shown.length} 条未展开
            </div>
          )}
        </>
      )}
    </div>
  );
}

function pct(v: number | null): string {
  return v === null || v === undefined ? '—' : `${(v * 100).toFixed(1)}%`;
}

/* ---------------------------------------------------------------- 复跑（flake 过滤） */

/** 复跑提示：每次尝试的状态/耗时/签名（悬浮在「复跑N次」上）。 */
function retryTooltip(c: CaseExecRow): string {
  const items = c.retry ?? [];
  if (items.length === 0) return `共执行 ${c.attempts ?? 1} 次`;
  return items.map(attemptLabel).join(' → ');
}

function attemptLabel(a: RetryAttempt): string {
  const ms = a.duration_ms ? ` ${a.duration_ms}ms` : '';
  const sig = a.failure_signature ? `（${shortSig(a.failure_signature, 12)}）` : '';
  return `第${a.attempt}次 ${a.status}${ms}${sig}`;
}

/** 「第几次执行」切换按钮的悬浮说明。 */
function attemptHint(c: CaseExecRow | null, n: number): string {
  const item = (c?.retry ?? []).find((a) => a.attempt === n);
  return item ? attemptLabel(item) : `第 ${n} 次执行`;
}

/** 差异区右侧的说明文案：把"算不出来"的原因直接说清，避免用户以为是功能坏了。 */
function diffHint(diff: ShotDiffResult | null): string {
  if (!diff) return '';
  if (diff.rel === null) return diff.detail ?? '无法计算差异';
  const s = diff.stats ?? {};
  if (s.changed === -1) {
    return `画面尺寸变化：${s.size_before ?? '?'} → ${s.size_after ?? '?'}（无法逐像素对齐，已整图提示）`;
  }
  const ratio = typeof s.ratio === 'number' ? `${(s.ratio * 100).toFixed(2)}%` : '—';
  return `变化像素 ${ratio}，共 ${s.boxes ?? 0} 处高亮区${diff.cached ? '（缓存）' : ''}`;
}

/** 单张截图：不存在时静默隐藏（不显示破图）；点击进灯箱看细节。
 *
 *  ``relOverride``：step_trace 落库的真实路径。复跑的截图带 attempt 前缀
 *  （a2_3_after.png），按 (run, case, step, phase) 反推会读成第 1 次尝试的图。
 */
function Shot({ root, runId, caseExecId, stepNo, phase, onZoom, relOverride }: {
  root?: string; runId: number; caseExecId: number; stepNo: number;
  phase: 'before' | 'after' | 'diff';
  onZoom: (src: string, caption: string) => void;
  relOverride?: string;
}) {
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let alive = true;
    setSrc(null);
    setFailed(false);
    const rel = relOverride
      ?? (phase === 'diff'
        ? `${runId}/${caseExecId}/diff_${stepNo}.png`
        : shotRel(runId, caseExecId, stepNo, phase));
    readAssetDataUrl(assetPath(root, rel))
      .then((url) => { if (alive) setSrc(url); })
      .catch(() => { if (alive) setFailed(true); });
    return () => { alive = false; };
  }, [root, runId, caseExecId, stepNo, phase, relOverride]);

  if (failed) return null;
  const captions = {
    before: 'before（操作前）',
    after: 'after（操作后）',
    diff: '差异高亮（红框=变化区）',
  };
  return (
    <figure>
      <figcaption>
        <span>{captions[phase]}</span>
        {src && (
          <>
            <span className="spacer" />
            <button className="copy" title="放大查看"
                    onClick={() => onZoom(src, `run#${runId} · step #${stepNo} · ${captions[phase]}`)}>
              ⤢
            </button>
          </>
        )}
      </figcaption>
      {src
        ? <img src={src} alt={captions[phase]} onClick={() => onZoom(src, `run#${runId} · step #${stepNo} · ${captions[phase]}`)} />
        : <div className="skeleton" style={{ width: 320, height: 200, margin: 8 }} />}
    </figure>
  );
}
