/** Runs · 执行记录（设计 14.3）：全宽表格 + 通过率堆叠条 + KB 命中 + LLM 成本。
 *
 *  交互约定：
 *  - 有 run 在执行时，订阅引擎事件**节流刷新**（2s）——列表自己会动，不用人盯着手动刷；
 *  - 文本搜索直接过滤（任务名 / run 号），列头可点击排序；
 *  - 时间列显示相对时间（新旧立判），悬浮给绝对时间。
 */

import { useMemo, useRef, useState } from 'react';

import { call } from '../bridge';
import { Badge, Empty, Msg, PassBar, TrendChart } from '../components/Bits';
import { fmtDuration, fmtRelative, fmtTime, passRate, statusLabel } from '../format';
import { navigate, useAsync, useEngineEvents } from '../hooks';
import type { RunRow, TaskRow } from '../types';

type SortKey = 'id' | 'task' | 'pass' | 'fail' | 'kb' | 'tokens' | 'started' | 'duration';

export function RunsPage() {
  const runs = useAsync(() => call<{ runs: RunRow[] }>('run.list', { limit: 200 }), []);
  const tasks = useAsync(() => call<{ tasks: TaskRow[] }>('task.list'), []);
  const [status, setStatus] = useState('');
  const [query, setQuery] = useState('');
  // 只看有问题的 run（失败/阻塞/中止）：日常排查最常用，省去人工逐行扫
  const [onlyProblems, setOnlyProblems] = useState(false);
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({ key: 'id', desc: true });
  const lastReload = useRef(0);

  const taskName = useMemo(() => {
    const map = new Map<number, string>();
    for (const t of tasks.data?.tasks ?? []) map.set(t.id, t.name);
    return map;
  }, [tasks.data]);

  const allRows = runs.data?.runs ?? [];
  const hasRunning = allRows.some((r) => r.status === 'running');

  // 执行中的 run 由引擎事件推动刷新（节流 2s）；run 收尾事件绕过节流——
  // 否则最后一条事件落进节流窗口被丢弃后，列表会永远停在"执行中"
  useEngineEvents(['run.progress', 'case.finished'], (_ev, payload) => {
    const p = payload as { phase?: string };
    if (p?.phase === 'finished') {
      lastReload.current = Date.now();
      runs.reload();
      return;
    }
    if (!hasRunning || Date.now() - lastReload.current < 2000) return;
    lastReload.current = Date.now();
    runs.reload();
  });

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    const filtered = allRows.filter((r) => {
      if (status && r.status !== status) return false;
      if (q) {
        const hay = `#${r.id} ${r.id} ${taskName.get(r.task_id) ?? ''} 任务${r.task_id}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      if (!onlyProblems) return true;
      const pr = passRate(r.stats);
      return pr.fail > 0 || pr.blocked > 0 || r.status === 'aborted';
    });
    const dir = sort.desc ? -1 : 1;
    const num = (v: number | null | undefined) => v ?? -1;
    return [...filtered].sort((a, b) => {
      switch (sort.key) {
        case 'id': return dir * (a.id - b.id);
        case 'task': return dir * (a.task_id - b.task_id);
        case 'pass': return dir * (passRate(a.stats).rate - passRate(b.stats).rate);
        case 'fail': return dir * (passRate(a.stats).fail - passRate(b.stats).fail);
        case 'kb': return dir * ((a.stats.kb_hit_cases ?? 0) - (b.stats.kb_hit_cases ?? 0));
        case 'tokens': return dir * ((a.stats.llm_usage?.tokens ?? 0) - (b.stats.llm_usage?.tokens ?? 0));
        case 'duration': {
          const da = durationMs(a); const db = durationMs(b);
          return dir * (num(da) - num(db));
        }
        case 'started':
        default: {
          const da = Date.parse(a.started_at ?? ''); const dbv = Date.parse(b.started_at ?? '');
          return dir * (num(Number.isNaN(da) ? null : da) - num(Number.isNaN(dbv) ? null : dbv));
        }
      }
    });
  }, [allRows, status, query, onlyProblems, sort, taskName]);

  // 顶部汇总：一眼看出近期整体健康度，不用逐个 run 点进去
  const summary = useMemo(() => {
    let pass = 0; let fail = 0; let blocked = 0; let skip = 0; let tokens = 0; let kb = 0;
    for (const r of allRows) {
      const pr = passRate(r.stats);
      pass += pr.pass; fail += pr.fail; blocked += pr.blocked; skip += pr.skip;
      tokens += r.stats.llm_usage?.tokens ?? 0;
      kb += r.stats.kb_hit_cases ?? 0;
    }
    const total = pass + fail + blocked + skip;
    return { pass, fail, blocked, skip, total, tokens, kb };
  }, [allRows]);

  // 趋势：最近 30 次 run 的通过率走势（回答"在变好还是变差"——计数卡回答不了）
  const trend = useMemo(() => allRows.slice(0, 30).slice().reverse().map((r) => {
    const pr = passRate(r.stats);
    return {
      id: r.id, rate: pr.total > 0 ? pr.pass / pr.total : 0, status: r.status,
      title: `run #${r.id}（${statusLabel(r.status)}）通过率 `
        + `${Math.round((pr.total > 0 ? pr.pass / pr.total : 0) * 100)}% · ${fmtTime(r.started_at)}`,
    };
  }), [allRows]);

  const th = (key: SortKey, label: string, numeric = false) => (
    <th className={numeric ? 'num sortable' : 'sortable'}
        onClick={() => setSort((s) => ({ key, desc: s.key === key ? !s.desc : true }))}>
      {label}
      <span className="sort-mark">{sort.key === key ? (sort.desc ? '▼' : '▲') : ''}</span>
    </th>
  );

  return (
    <div className="main wide">
      <div className="page-head">
        <h2>执行记录</h2>
        <span className="hint">点行进入详情：概览 / 用例明细 / 轨迹回放</span>
        {hasRunning && (
          <span className="hstack" title="有 run 正在执行，列表自动刷新">
            <span className="dot busy" />
            <span className="muted">执行中（自动刷新）</span>
          </span>
        )}
      </div>

      {/* 紧凑统计条 + 走势图：计数卡堆只回答"现在多少"，折线才能回答"在变好还是变差" */}
      <div className="panel health-strip" style={{ marginBottom: 'var(--sp-4)' }}>
        <div className="statline">
          <span className="stat"><b>{allRows.length}</b><span>run 总数</span></span>
          <span className="stat tone-pass">
            <b>{summary.total > 0 ? `${Math.round((summary.pass / summary.total) * 100)}%` : '—'}</b>
            <span>累计通过率 {summary.pass}/{summary.total}</span>
          </span>
          <span className="stat tone-fail"><b>{summary.fail}</b><span>失败</span></span>
          <span className="stat tone-blocked"><b>{summary.blocked}</b><span>阻塞</span></span>
          <span className="stat tone-accent"><b>{summary.kb}</b><span>KB 命中</span></span>
          <span className="stat"><b>{summary.tokens.toLocaleString()}</b><span>LLM token</span></span>
        </div>
        <div className="trend-wrap">
          <TrendChart points={trend} />
        </div>
      </div>

      <div className="toolbar">
        <input
          type="search"
          style={{ width: 220 }}
          placeholder="搜索 run 号 / 任务名"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">全部状态</option>
          <option value="running">执行中</option>
          <option value="completed">已完成</option>
          <option value="aborted">已中止</option>
        </select>
        <label className="hstack muted">
          <input type="checkbox" checked={onlyProblems}
                 onChange={(e) => setOnlyProblems(e.target.checked)} />
          只看有失败的
        </label>
        <span className="chip">显示 {rows.length} / {allRows.length}</span>
        <span className="spacer" />
        <button className="btn" onClick={() => { runs.reload(); tasks.reload(); }}>刷新</button>
      </div>

      {runs.error && <Msg kind="error">读取 run 失败：{runs.error}</Msg>}

      <table className="grid">
        <thead>
          <tr>
            {th('id', 'Run')}{th('task', '任务')}{th('started', '开始')}
            <th>状态</th>{th('pass', '通过率')}{th('fail', '失败', true)}
            {th('kb', 'KB 命中', true)}{th('tokens', 'LLM token', true)}
            <th>禅道回写</th>{th('duration', '耗时', true)}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const pr = passRate(r.stats);
            const hit = r.stats.kb_hit_cases ?? 0;
            const running = r.status === 'running';
            return (
              <tr key={r.id} className={`clickable ${r.status === 'aborted' ? 'row-blocked' : ''}`}
                  onClick={() => navigate('run', { runId: r.id })}>
                <td className="mono">#{r.id}</td>
                <td className="truncate" style={{ maxWidth: 220 }} title={taskName.get(r.task_id) ?? ''}>
                  {taskName.get(r.task_id) ?? `任务#${r.task_id}`}
                  <span className="faint mono"> #{r.task_id}</span>
                </td>
                <td className="mono muted" title={r.started_at}>{fmtRelative(r.started_at)}</td>
                <td>
                  <span className="hstack">
                    {running && <span className="dot busy" />}
                    <Badge status={r.status} />
                  </span>
                </td>
                <td><PassBar pass={pr.pass} fail={pr.fail} blocked={pr.blocked}
                             skip={pr.skip} total={pr.total} /></td>
                <td className="num" style={pr.fail > 0 ? { color: 'var(--s-fail)', fontWeight: 600 } : undefined}>
                  {pr.fail || '—'}
                </td>
                <td className="num">{hit > 0 ? `${hit}/${pr.total}` : '—'}</td>
                <td className="num muted">{(r.stats.llm_usage?.tokens ?? 0).toLocaleString()}</td>
                <td>{r.stats.writeback
                  ? <Badge status={writebackTone(r.stats.writeback)}
                           text={statusLabel(writebackLabel(r.stats.writeback))} />
                  : <span className="faint">—</span>}</td>
                <td className="num mono muted" title={`${fmtTime(r.started_at)} ~ ${fmtTime(r.finished_at)}`}>
                  {fmtDuration(r.started_at, r.finished_at) !== '—'
                    ? fmtDuration(r.started_at, r.finished_at)
                    : running ? '进行中…' : '—'}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {rows.length === 0 && !runs.loading && (
        <Empty
          text={allRows.length > 0 ? '当前筛选条件下没有记录' : '还没有执行记录：先建任务并执行一次回归'}
          action={allRows.length === 0
            ? <button className="btn primary" onClick={() => navigate('tasks')}>去任务页</button>
            : <button className="btn" onClick={() => { setQuery(''); setStatus(''); setOnlyProblems(false); }}>清除筛选</button>}
        />
      )}
    </div>
  );
}

function durationMs(r: RunRow): number | null {
  if (!r.started_at || !r.finished_at) return null;
  const a = Date.parse(r.started_at); const b = Date.parse(r.finished_at);
  if (Number.isNaN(a) || Number.isNaN(b)) return null;
  return b - a;
}

function writebackLabel(v: string): string {
  if (v === 'local_only') return 'local';
  return v;
}

function writebackTone(v: string): string {
  if (v === 'synced') return 'synced';
  if (v === 'sync_pending') return 'sync_pending';
  if (v === 'skipped') return 'skip';
  return 'local';
}
