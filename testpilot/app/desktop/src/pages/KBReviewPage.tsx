/** KBReview · 知识库审阅（设计 14.3）：左队列列表 + 右详情（双栏）。
 *
 *  两个 Tab：
 *  - **失败待审**：人审是 LLM 归因的**唯一生效闸门**；驳回只留档不删；处置策略决定命中后的引擎行为；
 *  - **用例上下文**（case_ctx）：人审维护的前置依赖（模拟器场景）/已知坑/备注，
 *    执行前预取注入（场景预加载 + LLM 归因参考），链路：KBReview -> kb.caseCtxSave -> 执行期消费。
 */

import { useEffect, useState } from 'react';

import { call } from '../bridge';
import { Badge, CopyId, Empty, Msg } from '../components/Bits';
import { pushToast } from '../components/Ui';
import { fmtTime, shortSig } from '../format';
import { msg, useAsync } from '../hooks';
import type { CaseCtxEntry, KbEntry } from '../types';

const DISPOSITIONS = ['block_known', 'retry_ready', 'skip', 'none'];

const DISPOSITION_HINT: Record<string, string> = {
  block_known: '直接判阻塞（已知失败，不再重试）',
  retry_ready: '按 retry 策略重试一次',
  skip: '跳过该步骤并继续',
  none: '不做特殊处置（仅作为归因参考）',
};

type Mode = 'failure' | 'ctx';

export function KBReviewPage({ initialTab, initialCase, initialId }: {
  initialTab?: string;
  initialCase?: string;
  /** 深链 #/kb?id=N：RunDetail「审阅」跳过来时直接选中该条目 */
  initialId?: string;
} = {}) {
  const [mode, setMode] = useState<Mode>(initialTab === 'ctx' ? 'ctx' : 'failure');
  return (
    <div className="main">
      <div className="page-head">
        <h2>知识库审阅</h2>
        <span className="hint">
          LLM 归因必须人审后才生效（防止幻觉污染知识库）；用例上下文由人审直接维护
        </span>
        {mode === 'failure' && (
          <span className="faint" style={{ fontSize: 'var(--fs-sm)' }}>
            快捷键：<span className="mono">A</span> 通过 · <span className="mono">R</span> 驳回 ·{' '}
            <span className="mono">J/K</span> 上下条
          </span>
        )}
        <span className="spacer" />
      </div>

      <div className="tabs">
        <button className={mode === 'failure' ? 'on' : ''}
                onClick={() => setMode('failure')}>失败待审</button>
        <button className={mode === 'ctx' ? 'on' : ''}
                onClick={() => setMode('ctx')}>用例上下文</button>
      </div>

      {mode === 'failure'
        ? <FailureQueue initialId={initialId} />
        : <CaseCtxPanel initialCase={initialCase} />}
    </div>
  );
}

/* ---------------------------------------------------------------- 失败待审 */

function FailureQueue({ initialId }: { initialId?: string }) {
  const queue = useAsync(() => call<{ entries: KbEntry[] }>('kb.pending'), []);
  const entries = queue.data?.entries ?? [];
  const [currentId, setCurrentId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ kind: 'error' | 'info'; text: string } | null>(null);

  useEffect(() => {
    if (currentId !== null || entries.length === 0) return;
    // 深链带来的条目优先；待审队列里找不到（已审掉）就留在队列首条
    const wanted = initialId
      ? entries.find((e) => String(e.id) === initialId)
      : undefined;
    setCurrentId(wanted?.id ?? entries[0].id);
  }, [entries, currentId, initialId]);

  const current = entries.find((e) => e.id === currentId) ?? null;
  const [review, setReview] = useState({ category: 'unknown', suggestion: '', evidence: '' });
  useEffect(() => {
    setReview({ category: String(current?.payload?.category ?? 'unknown'),
                suggestion: String(current?.payload?.suggestion ?? ''),
                evidence: String(current?.payload?.evidence ?? '') });
  }, [current?.id]);

  // advance=false 用于"就地修改"类动作（改处置策略）：改动后仍停在当前条目，
  // 只有"通过/驳回"这种审完即走的动作才自动前进到下一条
  const act = async (label: string, fn: () => Promise<unknown>,
                     toastKind: 'pass' | 'warn' = 'pass',
                     advance = true) => {
    setBusy(true); setNotice(null);
    try {
      await fn();
      pushToast(toastKind, `${label}成功`);
      if (advance) {
        // 审完自动选下一条：人审是批量动作，不该每审一条都要手动再点一次
        const idx = entries.findIndex((e) => e.id === currentId);
        const next = entries[idx + 1] ?? entries[idx - 1] ?? null;
        setCurrentId(next ? next.id : null);
      }
      queue.reload();
    } catch (e) {
      setNotice({ kind: 'error', text: `${label}失败：${msg(e)}` });
    } finally { setBusy(false); }
  };

  // 键盘流：A 通过 / R 驳回 / J·K 切换（批量人审时鼠标点选太慢）
  useEffect(() => {
    if (!current || busy) return;
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName;
      if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
      const key = e.key.toLowerCase();
      if (key === 'a') { e.preventDefault(); void act('通过生效', () => call('kb.confirm', { entryId: current.id })); }
      if (key === 'r') { e.preventDefault(); void act('驳回', () => call('kb.reject', { entryId: current.id }), 'warn'); }
      if (key === 'j' || key === 'k') {
        e.preventDefault();
        const idx = entries.findIndex((x) => x.id === current.id);
        const next = key === 'j' ? entries[idx + 1] : entries[idx - 1];
        if (next) setCurrentId(next.id);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current?.id, busy, entries]);

  const position = current ? entries.findIndex((e) => e.id === current.id) + 1 : 0;

  return (
    <>
      <div className="hstack" style={{ marginBottom: 'var(--sp-3)' }}>
        <span className="hint">
          待审 {entries.length} 条 · 处置策略决定命中后的引擎行为
        </span>
        {current && <span className="chip">第 {position} / {entries.length} 条</span>}
      </div>

      {notice && <Msg kind={notice.kind}>{notice.text}</Msg>}
      {queue.error && <Msg kind="error">读取待审队列失败：{queue.error}</Msg>}

      <div className="row split">
        <div className="left">
          <table className="grid">
            <thead><tr><th>签名</th><th>分类</th><th className="num">置信</th></tr></thead>
            <tbody>
              {entries.map((e) => (
                <tr key={e.id} className={`clickable ${e.id === currentId ? 'on' : ''}`} onClick={() => setCurrentId(e.id)}>
                  <td className="mono" title={e.signature}>{shortSig(e.signature, 12)}
                    <div className="faint">#{e.id} · {fmtTime(e.updated_at)}</div></td>
                  <td><Badge status="pending" text={e.payload?.category ?? e.type} /></td>
                  <td className="num mono">{e.confidence?.toFixed?.(2) ?? e.confidence}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {entries.length === 0 && !queue.loading && <Empty text="没有待审条目：跑一次有失败的回归后会自动入队" />}
          <div style={{ marginTop: 8 }}>
            <button className="btn" onClick={() => queue.reload()}>刷新</button>
          </div>
        </div>

        <div className="right">
          {!current ? <Empty text="左侧选择一条待审记录" /> : (
            <div className="panel">
              <h3>审阅详情 <span className="faint">#{current.id}</span></h3>
              <dl className="kv">
                <dt>签名</dt><dd><CopyId text={current.signature} /></dd>
                <dt>类型</dt><dd>{current.type}</dd>
                <dt>根因草稿</dt><dd>{current.payload?.category ?? '—'}
                  <span className="faint">（置信 {current.confidence}）</span></dd>
                <dt>错误类型</dt><dd>{current.payload?.error_type ?? '—'}</dd>
                <dt>动作 / 目标</dt><dd>{current.payload?.action ?? '—'} / {current.payload?.target_key ?? '—'}</dd>
                <dt>页面</dt><dd>{current.payload?.page_key ?? '—'}</dd>
                <dt>处置建议</dt><dd>{current.payload?.suggestion ?? '—'}</dd>
                <dt>证据</dt><dd>{current.payload?.evidence ?? '—'}</dd>
              </dl>

              <details className="review-correction"><summary>修订根因与建议</summary>
                <label>根因类别<select value={review.category} onChange={(e) => setReview({ ...review, category: e.target.value })}>
                  {['app_crash', 'uia_change', 'timing', 'env', 'dsl_error', 'product_bug', 'unknown'].map((c) => <option key={c} value={c}>{c}</option>)}
                </select></label>
                <label>建议<textarea value={review.suggestion} onChange={(e) => setReview({ ...review, suggestion: e.target.value })} /></label>
                <label>证据<textarea value={review.evidence} onChange={(e) => setReview({ ...review, evidence: e.target.value })} /></label>
                <button className="btn" disabled={busy} onClick={() => act('保存修订', () => call('kb.setDisposition', { entryId: current.id, disposition: current.payload?.disposition ?? 'none', ...review }), 'pass', false)}>保存修订</button>
              </details>
              <div style={{ marginTop: 8 }}>
                <div className="faint">处置策略（决定命中后的引擎行为）</div>
                <div className="hstack" style={{ marginTop: 4 }}>
                  <select
                    value={current.payload?.disposition ?? 'none'}
                    disabled={busy}
                    onChange={(e) => act('更新处置策略', () => call('kb.setDisposition', {
                      entryId: current.id, disposition: e.target.value,
                    }), 'pass', false)}
                  >
                    {DISPOSITIONS.map((d) => <option key={d} value={d}>{d}</option>)}
                  </select>
                  <span className="muted">{DISPOSITION_HINT[current.payload?.disposition ?? 'none']}</span>
                </div>
              </div>

              <div className="hstack" style={{ marginTop: 12 }}>
                <button className="btn primary" disabled={busy} title="快捷键 A"
                        onClick={() => act('通过生效', () => call('kb.confirm', { entryId: current.id }))}>
                  ✓ 通过生效
                </button>
                <button className="btn danger" disabled={busy} title="快捷键 R"
                        onClick={() => act('驳回', () => call('kb.reject', { entryId: current.id }), 'warn')}>
                  ✗ 驳回（留档不删）
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </>
  );
}

/* ---------------------------------------------------------------- 用例上下文 */

/** case_ctx 维护：场景（前置依赖）/ 已知坑 / 人审备注。
 *
 *  语义与引擎一致：字段留空 = 清除该字段；保存按字段增量合并（不清掉没动的字段）。
 */
function CaseCtxPanel({ initialCase }: { initialCase?: string }) {
  const list = useAsync(() => call<{ entries: CaseCtxEntry[] }>('kb.caseCtxList'), []);
  const entries = list.data?.entries ?? [];
  const [caseId, setCaseId] = useState(initialCase ?? '');
  const [scene, setScene] = useState('');
  const [pitfalls, setPitfalls] = useState('');
  const [notes, setNotes] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ kind: 'error' | 'info'; text: string } | null>(null);

  const load = (e: CaseCtxEntry) => {
    setCaseId(e.case_id);
    setScene(e.payload.scene ?? '');
    setPitfalls((e.payload.pitfalls ?? []).join('\n'));
    setNotes(e.payload.notes ?? '');
    setNotice(null);
  };

  // 深链（#/kb?tab=ctx&caseId=TC-1）或列表首次加载后自动选中
  useEffect(() => {
    if (entries.length === 0) return;
    const wanted = initialCase
      ? entries.find((e) => e.case_id === initialCase
        || String(e.zentao_case_id) === initialCase)
      : null;
    if (wanted) { load(wanted); return; }
    if (!caseId && !initialCase) load(entries[0]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entries.length, initialCase]);

  const save = async (remove = false) => {
    const target = caseId.trim();
    if (!target) {
      setNotice({ kind: 'error', text: '请先填写用例 ID（本地 TC-xxx 或禅道数字 ID）' });
      return;
    }
    setBusy(true); setNotice(null);
    try {
      if (remove) {
        await call('kb.caseCtxDelete', { caseId: target });
      } else {
        await call('kb.caseCtxSave', { caseId: target, scene, pitfalls, notes });
      }
      pushToast('pass', remove ? '用例上下文已删除' : '用例上下文已保存');
      if (remove) { setScene(''); setPitfalls(''); setNotes(''); }
      list.reload();
    } catch (e) {
      setNotice({ kind: 'error', text: `${remove ? '删除' : '保存'}失败：${msg(e)}` });
    } finally { setBusy(false); }
  };

  return (
    <>
      <div className="hstack" style={{ marginBottom: 'var(--sp-3)' }}>
        <span className="hint">
          已维护 {entries.length} 条 · 场景在执行前预加载；已知坑/备注供 LLM 归因参考
        </span>
        <span className="spacer" />
        <button className="btn" onClick={() => list.reload()}>刷新</button>
      </div>

      {notice && <Msg kind={notice.kind}>{notice.text}</Msg>}
      {list.error && <Msg kind="error">读取用例上下文失败：{list.error}</Msg>}

      <div className="row split">
        <div className="left">
          <table className="grid">
            <thead>
              <tr><th>用例</th><th>标题</th><th>场景 / 已知坑</th><th>更新</th></tr>
            </thead>
            <tbody>
              {entries.map((e) => (
                <tr key={e.id} className={`clickable ${e.case_id === caseId ? 'on' : ''}`}
                    onClick={() => load(e)}>
                  <td className="mono">{e.case_id}
                    <div className="faint">#{e.id} · 禅道 {e.zentao_case_id ?? '—'}</div></td>
                  <td>{e.case_title ?? '—'}</td>
                  <td className="faint">
                    {e.payload.scene ? <span className="chip">{e.payload.scene}</span> : null}
                    {(e.payload.pitfalls ?? []).length > 0
                      ? ` 坑 ${(e.payload.pitfalls ?? []).length} 条`
                      : (e.payload.scene ? '' : '—')}
                  </td>
                  <td className="faint">{fmtTime(e.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {entries.length === 0 && !list.loading && (
            <Empty text="还没有用例上下文：在右侧填用例 ID 后保存（用例需先入库）" />
          )}
        </div>

        <div className="right">
          <div className="panel">
            <h3>{entries.some((e) => e.case_id === caseId) ? '编辑上下文' : '新建上下文'}
              <span className="spacer" />
              <span className="faint" style={{ fontWeight: 400 }}>
                留空保存 = 清除该字段
              </span>
            </h3>
            <div className="stack" style={{ gap: 'var(--sp-3)' }}>
              <div className="field">
                <label>用例 ID</label>
                <input type="text" value={caseId} placeholder="TC-1024 或 1024"
                       onChange={(e) => setCaseId(e.target.value)} />
                <span className="hint">支持本地 TC-xxx 与禅道数字 ID（需已 pull 入库）</span>
              </div>
              <div className="field">
                <label>模拟器场景（前置依赖，可选）</label>
                <input type="text" value={scene} placeholder="如 normal_sample_a"
                       onChange={(e) => setScene(e.target.value)} />
                <span className="hint">每次执行及复跑前自动加载场景；加载失败将阻塞用例</span>
              </div>
              <div className="field">
                <label>已知坑（每行一条）</label>
                <textarea value={pitfalls} style={{ minHeight: 120 }}
                          placeholder={'预热需要 3 秒\n偶发首次点击不生效'}
                          onChange={(e) => setPitfalls(e.target.value)} />
              </div>
              <div className="field">
                <label>人审备注</label>
                <textarea value={notes} style={{ minHeight: 80 }}
                          placeholder="给下一个看这条用例的人/模型留的话"
                          onChange={(e) => setNotes(e.target.value)} />
              </div>
              <div className="hstack">
                <button className="btn primary" disabled={busy} onClick={() => save(false)}>
                  保存
                </button>
                <button className="btn danger" disabled={busy}
                        onClick={() => save(true)}>
                  删除该用例的上下文
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
