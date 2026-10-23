/** DslReview · DSL 审阅（设计 14.3）：左禅道原文只读 + 右 DSL 步骤（source_text 对照、
 *  不确定步骤高亮）；支持直接编辑 JSON 草稿后导入（dsl.save），并通过/驳回。
 *
 *  UI 约定：
 *  - 「待核」步骤必须**一眼可见**——LLM 解析不可靠处正是人审要盯的地方，不能被表格淹没。
 *  - 驳回是不可逆语义（留档但标记 rejected），走内联确认条而非大弹窗（设计 14.4）。
 */

import { useEffect, useMemo, useRef, useState } from 'react';

import { call, caseJob } from '../bridge';
import { StepEditor } from '../components/StepEditor';
import { Badge, Empty, Msg } from '../components/Bits';
import { Collapsible, ConfirmBar, Segmented, Skeleton, pushToast } from '../components/Ui';
import { msg, navigate, useAsync, useCaseJobBusy } from '../hooks';
import type { CaseDetail, CaseRow } from '../types';

type View = 'steps' | 'json';

interface DslStepView {
  id: string;
  action: string;
  uncertain?: boolean;
  value?: string;
  args?: Record<string, unknown>;
  source_text?: string;
  target?: { key?: string };
  verify?: { type?: string; expect?: string; op?: string };
}

export function DslReviewPage({ caseId, queue, embedded = false, onAccepted, onBack }: {
  caseId?: string; queue?: string; embedded?: boolean;
  onAccepted?: (caseId: string) => void; onBack?: () => void;
}) {
  const list = useAsync(() => call<{ cases: CaseRow[] }>('case.list'), []);
  const cases = list.data?.cases ?? [];
  const [active, setActive] = useState<string | null>(caseId ?? null);
  useEffect(() => { if (caseId) setActive(caseId); }, [caseId]);
  const reviewQueue = useMemo(() => Array.from(new Set(queue?.split(',').filter(Boolean) ?? [])), [queue]);
  const [switchTo, setSwitchTo] = useState<string | null>(null);
  const jobBusy = useCaseJobBusy();
  const [text, setText] = useState('');
  const [editTools, setEditTools] = useState(false);
  const [view, setView] = useState<View>('steps');
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmReject, setConfirmReject] = useState(false);
  const [notice, setNotice] = useState<{ kind: 'error' | 'info' | 'warn'; text: string } | null>(null);

  useEffect(() => {
    if (!active && cases.length > 0) {
      const stale = cases.find((c) => c.sync_status !== 'synced' || c.status === 'draft');
      setActive((stale ?? cases[0]).case_id);
    }
  }, [cases, active]);

  const detail = useAsync(
    () => (active ? call<CaseDetail>('case.detail', { caseId: active }) : Promise.resolve(null)),
    [active],
  );
  const data = detail.data?.case_id === active ? detail.data : null;

  // 载入导出文本作为可编辑草稿（人审修改后再导回）。
  // 用户有未保存编辑时，version/sync_status 变化触发的重载**不覆盖**草稿
  // （否则详情刷新会把正在编辑的内容静默冲掉）；只有切换用例才强制重载。
  const editedRef = useRef(false);
  const [originalText, setOriginalText] = useState('');
  const loadedForRef = useRef<string | null>(null);
  useEffect(() => {
    if (!active) { setText(''); editedRef.current = false; loadedForRef.current = null; return; }
    let alive = true;
    if (loadedForRef.current !== active) {
      setText(''); setOriginalText(''); editedRef.current = false;
    }
    call<{ content: string }>('case.export', { caseId: active, format: 'json' })
      .then((out) => {
        if (!alive) return;
        if (editedRef.current && loadedForRef.current === active) return;
        setText(out.content);
        setOriginalText(out.content);
        editedRef.current = false;
        loadedForRef.current = active;
      })
      .catch((e: unknown) => { if (alive) setNotice({ kind: 'error', text: `导出草稿失败：${msg(e)}` }); });
    return () => { alive = false; };
  }, [active, data?.version, data?.sync_status, data?.revision]);

  const uncertain = useMemo(() => new Set(data?.uncertain_steps ?? []), [data]);
  const steps = useMemo(() => {
    try {
      if (loadedForRef.current === active) return (JSON.parse(text).steps ?? []) as DslStepView[];
    } catch { /* Keep the last loaded step view while editing invalid JSON. */ }
    return (data?.steps ?? []) as unknown as DslStepView[];
  }, [data, text, active]);

  /** 上次人审通过时冻结的步骤（设计 3.6 三版对照的第三版）。
   *
   *  version=0（从未人审）时引擎返回 null —— 此时没有"已确认版本"可对照，
   *  不能当成"有一版是空的"来渲染。 */
  const acceptedSteps = useMemo(
    () => (data?.accepted_steps ?? []) as unknown as DslStepView[],
    [data],
  );

  const flagged = useMemo(
    () => steps.filter((s, i) => s.uncertain || uncertain.has(s.id ?? String(i))).length,
    [steps, uncertain],
  );

  // 草稿是否与后端当前版本一致（判断「有没有未保存改动」）
  const draftDirty = useMemo(() => {
    if (!data) return false;
    try {
      return JSON.stringify(JSON.parse(text)) !== JSON.stringify(JSON.parse(originalText));
    } catch { return true; }
  }, [text, data, originalText]);

  const act = async (label: string, fn: () => Promise<unknown>, refresh = true, keepToast = false) => {
    setBusy(label); setNotice(null);
    try {
      const out = await fn();
      if (label.includes('保存')) {
        editedRef.current = false;
        setOriginalText(text);
      }
      if (!keepToast) pushToast('pass', `${label}成功`);
      else setNotice({ kind: 'info', text: `${label}：${JSON.stringify(out)}` });
      if (refresh) { detail.reload(); list.reload(); }
      if (label === '保存并审核' && active) {
        if (onAccepted) {
          onAccepted(active);
        } else if (reviewQueue.length > 0) {
          const current = reviewQueue.indexOf(active);
          const next = current >= 0 ? reviewQueue[current + 1] : undefined;
          if (next) {
            editedRef.current = false;
            setActive(next);
            setNotice({ kind: 'info', text: `已通过 ${active}，继续审核下一条。` });
          } else {
            setNotice({ kind: 'info', text: '本批用例已全部审核完成。' });
          }
        }
      }
    } catch (e) {
      setNotice({ kind: 'error', text: `${label}失败：${msg(e)}` });
    } finally { setBusy(null); setConfirmReject(false); }
  };

  const disabled = busy !== null || jobBusy || !active || detail.loading || loadedForRef.current !== active;
  const position = active ? reviewQueue.indexOf(active) : -1;
  const switchCase = (id: string) => {
    if (draftDirty) { setSwitchTo(id); return; }
    setActive(id); setNotice(null); setConfirmReject(false);
  };

  return (
    <div className={embedded ? "embedded-review" : "main wide"}>
      <div className="page-head">
        <h2>{embedded ? "核对并确认用例" : "DSL 审阅"}</h2>
        <span className="hint">LLM 解析结果必须人审通过才会生效（active 才可执行）</span>
        {data && <Badge status={data.status} />}
        {data && <Badge status={data.sync_status} />}
        {data && (
          <span className="chip" title={data.version === 0 ? '尚未人审通过，不可执行' : '当前生效版本'}>
            {data.version === 0 ? '未生效' : `v${data.version}`}
          </span>
        )}
        {steps.length > 0 && <span className="chip">{steps.length} 步</span>}
        {flagged > 0 && (
          <span className="chip" title="LLM 自评不确定的步骤，务必逐条核对">
            ⚠ 待核 {flagged} 步
          </span>
        )}
      </div>

      <div className="toolbar">
        <button className="btn" disabled={busy !== null} onClick={() => {
          if (draftDirty) { setSwitchTo('@back'); return; }
          if (onBack) onBack(); else navigate('cases', { selection: reviewQueue.join(',') });
        }}>{embedded ? '返回选择用例' : '返回用例库 / 创建任务'}</button>
        {reviewQueue.length > 0 && <>
          <span>批量审核 {position >= 0 ? position + 1 : '—'} / {reviewQueue.length}</span>
          <button className="btn" disabled={busy !== null || position <= 0}
            onClick={() => switchCase(reviewQueue[position - 1])}>上一条</button>
          <button className="btn" disabled={busy !== null || position < 0 || position >= reviewQueue.length - 1}
            onClick={() => switchCase(reviewQueue[position + 1])}>下一条</button>
          <span className="hint">逐条核对原文和步骤，保存并通过后再审核下一条。</span>
        </>}
      </div>
      {switchTo && <ConfirmBar text="当前草稿有未保存修改，切换将放弃这些修改。" confirmText="放弃修改并切换"
        onConfirm={() => {
          editedRef.current = false;
          if (switchTo === '@back') { if (onBack) onBack(); else navigate('cases', { selection: reviewQueue.join(',') }); }
          else setActive(switchTo);
          setSwitchTo(null); setNotice(null);
        }}
        onCancel={() => setSwitchTo(null)} />}
      <div className="toolbar">
        {embedded && <strong>{active} · {data?.title}</strong>}
        {!embedded && <><span className="muted nowrap">用例</span>
        <select value={active ?? ''} onChange={(e) => switchCase(e.target.value)}
                style={{ maxWidth: 460 }}>
          <option value="">（选择用例）</option>
          {cases.map((c) => (
            <option key={c.case_id} value={c.case_id}>
              {c.case_id} · {c.title || '（无标题）'} · {c.status}/{c.sync_status}
            </option>
          ))}
        </select></>}
        <span className="spacer" />
        {embedded && <button className="btn ghost" onClick={() => setEditTools(!editTools)}>{editTools ? '收起修订选项' : '修订选项'}</button>}
        {(!embedded || editTools) && <>
        <button className="btn" disabled={disabled || data?.status === 'rejected' || draftDirty}
                onClick={() => act('重新解析', () => caseJob('case.parse', { caseId: active }), true, true)}>
          重新解析
        </button>
        <button className="btn" disabled={disabled}
                onClick={() => act('保存草稿', () => call('dsl.save', { caseId: active, text, format: 'json' }))}>
          保存草稿
        </button></>}
        <button className="btn primary" disabled={disabled || !data || !text || steps.length === 0 || data.sync_status !== 'synced' || (data.status === 'active' && !draftDirty)}
                onClick={() => act('保存并审核', () => call('dsl.accept', { caseId: active, text, format: 'json', expectedRevision: data?.revision }))}>
          {embedded
            ? '确认此用例，继续下一步 →'
            : reviewQueue.length > 0 && position >= 0 && position < reviewQueue.length - 1
              ? '✓ 保存并通过，下一条 →'
              : '✓ 保存并通过'}
        </button>
        {(!embedded || editTools) && <button className="btn danger" disabled={disabled}
                onClick={() => setConfirmReject(true)}>
          ✗ 驳回
        </button>}
      </div>

      {confirmReject && (
        <ConfirmBar
          text={`确认驳回用例 ${active}？驳回后该 DSL 标记 rejected 且不可执行（草稿留档不删除）。`}
          confirmText="确认驳回"
          busy={busy !== null}
          onConfirm={() => act('驳回', () => call('dsl.reject', { caseId: active }))}
          onCancel={() => setConfirmReject(false)}
        />
      )}

      {busy && <div className="msg info">正在执行：{busy}…</div>}
      {notice && <Msg kind={notice.kind}>{notice.text}</Msg>}
      {detail.error && <Msg kind="error">读取用例失败：{detail.error}</Msg>}

      {!active ? <Empty text="先选择一个用例" /> : detail.loading && !data ? (
        <div className="panel"><Skeleton rows={6} /></div>
      ) : (
        <div className="row split">
          <div className="left" style={{ flex: '0 0 34%', maxWidth: '34%' }}>
            <div className="panel">
              <h3>禅道原文 <span className="faint">（只读）</span></h3>
              <div className="pre-wrap muted scroll-y" style={{ maxHeight: 420, fontSize: 'var(--fs-sm)' }}>
                {data?.source_text || '（无原文）'}
              </div>
            </div>

            {(data?.preconditions?.length ?? 0) > 0 && (
              <div className="panel">
                <h3>前置条件</h3>
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {data?.preconditions?.map((p, i) => <li key={i} className="muted">{p}</li>)}
                </ul>
              </div>
            )}

            {(data?.notes || (data?.raw as { notes?: string } | null)?.notes) && (
              <div className="panel">
                <h3>备注</h3>
                <div className="pre-wrap muted" style={{ fontSize: 'var(--fs-sm)' }}>
                  {data?.notes ?? (data?.raw as { notes?: string } | null)?.notes}
                </div>
              </div>
            )}
          </div>

          <div className="right">
            <div className="panel">
              <div className="hstack" style={{ justifyContent: 'space-between', marginBottom: 'var(--sp-4)' }}>
                <h3 style={{ margin: 0 }}>解析结果</h3>
                <Segmented<View>
                  value={view}
                  onChange={setView}
                  options={[
                    { value: 'steps', label: '步骤表', title: '结构化步骤 + 原文对照' },
                    { value: 'json', label: 'JSON 草稿', title: '直接编辑后保存' },
                  ]}
                />
              </div>

              {view === 'steps' ? (
                <>
                  <StepEditor key={active} text={text} onChange={(value) => {
                    editedRef.current = true;
                    setText(value);
                  }} />
                  {data?.sync_status === 'conflict' && (
                    <Msg kind="warn">
                      禅道原文与本地草稿都已变化（冲突）。请对照左侧「禅道最新原文」与下方
                      「已确认版本」。先保存需保留的修改，再【重新解析】生成包含最新原文的草稿后审核。
                    </Msg>
                  )}
                  <table className="grid">
                    <thead>
                      <tr>
                        <th className="num">#</th><th>动作</th><th>目标</th>
                        <th>值/参数</th><th>校验</th><th>原文对照</th>
                      </tr>
                    </thead>
                    <tbody>
                      {steps.map((s, i) => {
                        const flag = s.uncertain || uncertain.has(s.id ?? String(i));
                        return (
                          <tr key={s.id ?? i} className={flag ? 'row-blocked' : undefined}>
                            <td className="num mono">{i + 1}</td>
                            <td className="mono nowrap">
                              {s.action}
                              {flag && <span className="badge blocked" style={{ marginLeft: 6 }}>待核</span>}
                            </td>
                            <td className="mono faint">{s.target?.key ?? '—'}</td>
                            <td className="mono faint truncate" style={{ maxWidth: 200 }}
                                title={s.value ?? (s.args ? JSON.stringify(s.args) : '')}>
                              {s.value ?? (s.args ? JSON.stringify(s.args) : '—')}
                            </td>
                            <td className="mono faint nowrap">
                              {s.verify?.type && s.verify.type !== 'none'
                                ? `${s.verify.type}:${s.verify.op ?? ''} ${s.verify.expect ?? ''}` : '—'}
                            </td>
                            <td className="faint" title={s.source_text ?? ''}>{s.source_text ?? '—'}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                  {steps.length === 0 && (
                    <Empty text="还没有 DSL 步骤：点【重新解析】让 LLM 从禅道原文生成" />
                  )}
                  {flagged > 0 && (
                    <Msg kind="warn">
                      有 {flagged} 个步骤被标记为不确定（黄底行）。请在步骤编辑器或「JSON 草稿」里核对修正，
                      确认无误再点【通过生效】——LLM 猜测的目标/参数一旦生效会直接把失败写进知识库。
                    </Msg>
                  )}

                  {acceptedSteps.length > 0 && (
                    <Collapsible
                      title={`已确认版本（v${data?.version ?? 0}）`}
                      description="上次人审通过时冻结的步骤；【通过生效】会用它被替换掉"
                      count={`${acceptedSteps.length} 步`}
                      defaultOpen={data?.sync_status === 'conflict'}
                    >
                      <table className="grid">
                        <thead>
                          <tr>
                            <th className="num">#</th><th>动作</th><th>目标</th>
                            <th>值/参数</th><th>原文对照</th>
                          </tr>
                        </thead>
                        <tbody>
                          {acceptedSteps.map((s, i) => (
                            <tr key={s.id ?? i} className="muted">
                              <td className="num mono">{i + 1}</td>
                              <td className="mono nowrap">{s.action}</td>
                              <td className="mono faint">{s.target?.key ?? '—'}</td>
                              <td className="mono faint truncate" style={{ maxWidth: 200 }}
                                  title={s.value ?? (s.args ? JSON.stringify(s.args) : '')}>
                                {s.value ?? (s.args ? JSON.stringify(s.args) : '—')}
                              </td>
                              <td className="faint" title={s.source_text ?? ''}>{s.source_text ?? '—'}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                      <div className="hstack" style={{ marginTop: 6 }}>
                        <button className="btn sm" onClick={() => setView('json')}
                                title="去 JSON 草稿里恢复这一版的内容">
                          在草稿中查看
                        </button>
                        <span className="faint">
                          草稿 {steps.length} 步 → 已确认 {acceptedSteps.length} 步
                          {data?.sync_status === 'conflict' && '（两版不同，请人工确认保留哪一版）'}
                        </span>
                      </div>
                    </Collapsible>
                  )}
                </>
              ) : (
                <>
                  <textarea
                    className="mono"
                    style={{ width: '100%', minHeight: 360, fontSize: 'var(--fs-sm)' }}
                    value={text}
                    spellCheck={false}
                    onChange={(e) => { editedRef.current = true; setText(e.target.value); }}
                  />
                  <div className="hstack" style={{ marginTop: 6 }}>
                    <span className="faint">结构非法会被 pydantic 挡下并报错，不会写坏库。</span>
                    <span className="spacer" />
                    {draftDirty && steps.length > 0 && <span className="chip">有未保存修改；通过时将一并保存</span>}
                    <span className="faint mono">{text.length} 字符</span>
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
