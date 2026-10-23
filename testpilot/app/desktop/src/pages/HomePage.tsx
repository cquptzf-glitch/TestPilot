import { call } from '../bridge';
import { Badge, Msg } from '../components/Bits';
import { navigate, useAsync, useEngineEvents } from '../hooks';
import { fmtRelative } from '../format';
import type { CaseRow, RunRow } from '../types';

export function HomePage() {
  const data = useAsync(async () => {
    const [cases, runs, pending] = await Promise.all([
      call<{ cases: CaseRow[] }>('case.list'),
      call<{ runs: RunRow[] }>('run.list', { limit: 8 }),
      call<{ entries: unknown[] }>('kb.pending'),
    ]);
    return { cases: cases.cases, runs: runs.runs, pending: pending.entries };
  }, []);
  useEngineEvents(['run.progress'], (_event, payload) => {
    if ((payload as { phase?: string }).phase === 'finished') data.reload();
  });
  const d = data.data;
  const draft = d?.cases.filter((c) =>
    c.status !== 'rejected' && c.status !== 'deprecated'
    && (c.status !== 'active' || c.sync_status !== 'synced' || !c.steps?.length)) ?? [];
  const active = d?.cases.filter((c) =>
    c.status === 'active' && c.sync_status === 'synced' && !!c.steps?.length) ?? [];
  const latest = d?.runs[0];
  const actions = [
    { n: '01', title: '准备测试用例', text: '从禅道同步，核对步骤与预期结果', button: '管理用例', route: 'cases' },
    { n: '02', title: '确认并执行', text: '生成并核对 DSL，直接执行所选用例', button: '开始测试', route: 'home' },
    { n: '03', title: '检查测试结果', text: '查看失败原因、截图和每一步的轨迹', button: '查看结果', route: 'runs' },
  ];
  return <div className="main home-page">
    <div className="home-heading"><div><div className="eyebrow">TEST WORKSPACE</div><h1>让每次回归，都有据可查。</h1><p>选择用例，确认步骤，开始今天的测试。</p></div><button className="btn primary large" onClick={() => navigate('home')}>开始测试 <span>↗</span></button></div>
    {data.error && <Msg kind="error">{data.error}<button className="btn" onClick={data.reload}>重试</button></Msg>}
    <div className="overview-metrics">
      {[['可执行用例', active.length, '已审核，可直接开始测试', 'home'],
        ['待审核用例', draft.length, '执行前只需核对这些用例', 'dsl'],
        ['最近执行', d?.runs.length ?? 0, '查看最近结果与失败轨迹', 'runs'],
        ['待处理知识', d?.pending.length ?? 0, '确认根因，积累测试经验', 'kb']].map(([label, value, detail, route]) =>
        <button className="overview-metric" key={String(label)} onClick={() => navigate(String(route))}><span>{label}</span><strong>{data.loading && !d ? '—' : value}<small>↗</small></strong><p>{detail}</p></button>)}
    </div>
    <div className="home-section-title"><h2>开始一轮测试</h2><span>三个步骤，完成测试闭环</span></div>
    <div className="workflow-cards">{actions.map((a) => <article key={a.n} className="workflow-card"><span className="workflow-number">{a.n}</span><h3>{a.title}</h3><p>{a.text}</p><button className="text-action" onClick={() => navigate(a.route)}>{a.button}<span>→</span></button></article>)}</div>
    <div className="home-bottom">
      <section className="panel recent-panel"><div className="home-section-title"><h2>最近执行</h2><button className="text-action" onClick={() => navigate('runs')}>全部记录 →</button></div>
        {d?.runs.length ? d.runs.map((r) => <button className="recent-run" key={r.id} onClick={() => navigate('run', { runId: r.id })}><span className="run-symbol">↗</span><span><strong>回归测试 #{r.id}</strong><small>{fmtRelative(r.started_at)}</small></span><span className="spacer" /><span className="recent-summary">{r.stats.pass ?? 0} / {r.stats.total ?? 0} 通过</span><Badge status={r.status === 'completed' ? (r.stats.fail || r.stats.error ? 'fail' : r.stats.blocked ? 'blocked' : 'pass') : r.status} /></button>) : <div className="home-empty"><strong>{data.loading ? '正在加载…' : '还没有执行记录'}</strong><p>从“开始测试”选择用例并执行，结果和轨迹会保存在这里。</p></div>}
      </section>
      <section className="panel attention-panel"><div className="eyebrow">NEXT UP</div><h2>下一步</h2>{draft.length ? <><p>有 <strong>{draft.length}</strong> 条用例等待审核。核对后即可用于自动执行。</p><button className="btn" onClick={() => navigate('dsl', { caseId: draft[0].case_id })}>审核第一条 →</button></> : <><p>{latest ? '查看最近一次执行，确认未通过项与待复核结果。' : '同步或添加用例后，可直接从“开始测试”进入审核与执行。'}</p><button className="btn" onClick={() => latest ? navigate('run', { runId: latest.id }) : navigate('cases')}>{latest ? '打开最新结果' : '添加测试用例'} →</button></>}</section>
    </div>
  </div>;
}
