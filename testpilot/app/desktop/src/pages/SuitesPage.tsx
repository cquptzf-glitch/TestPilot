/** Suites · 测试方案（设计 14.3）：左方案列表 + 右详情（有序用例、前置条件、驱动模式），一键建任务。 */

import { useEffect, useMemo, useState } from 'react';

import { call } from '../bridge';
import { Badge, Chips, Empty, Msg } from '../components/Bits';
import { ConfirmBar, PromptModal, pushToast } from '../components/Ui';
import { msg, navigate, useAsync } from '../hooks';
import type { CaseRow, SuiteRow } from '../types';

const MODES = ['auto', 'uia_first', 'vision_first', 'uia_only', 'vision_only'];

/** 对话框状态：非 null 即打开（分别对应新建方案 / 建任务）。 */
type Dialog = null | { kind: 'create' | 'task' | 'taskrun' };

export function SuitesPage() {
  const list = useAsync(() => call<{ suites: SuiteRow[] }>('suite.list'), []);
  const allCases = useAsync(() => call<{ cases: CaseRow[] }>('case.list'), []);
  const suites = list.data?.suites ?? [];
  const [currentId, setCurrentId] = useState<number | null>(null);
  const [draft, setDraft] = useState<{ name: string; caseIds: string[]; preconditions: string[]; mode: string } | null>(null);
  const [caseInput, setCaseInput] = useState('');
  const [notice, setNotice] = useState<{ kind: 'error' | 'info' | 'warn'; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);

  useEffect(() => {
    if (currentId === null && suites.length > 0) setCurrentId(suites[0].id);
  }, [suites, currentId]);

  const current = suites.find((s) => s.id === currentId) ?? null;

  /** 草稿与后端当前版本是否有差异（决定「未保存」提示与保存按钮的强调态）。 */
  const dirty = useMemo(() => {
    if (!current || !draft) return false;
    return draft.name !== current.name
      || JSON.stringify(draft.caseIds) !== JSON.stringify(current.case_ids)
      || JSON.stringify(draft.preconditions) !== JSON.stringify(current.preconditions)
      || String(current.driver_config?.mode ?? 'auto') !== draft.mode;
  }, [current, draft]);

  useEffect(() => {
    if (!current) { setDraft(null); return; }
    setDraft({
      name: current.name,
      caseIds: [...current.case_ids],
      preconditions: [...current.preconditions],
      mode: String(current.driver_config?.mode ?? 'auto'),
    });
  }, [current?.id, current?.name, JSON.stringify(current?.case_ids)]);   // eslint-disable-line react-hooks/exhaustive-deps

  const refresh = () => { list.reload(); };

  const save = async () => {
    if (!current || !draft) return;
    setBusy(true); setNotice(null);
    try {
      const out = await call<SuiteRow>('suite.update', {
        suiteId: current.id,
        name: draft.name,
        caseIds: draft.caseIds,
        preconditions: draft.preconditions,
        driverConfig: { mode: draft.mode },
      });
      const bad = out.not_runnable ?? [];
      setNotice(bad.length > 0
        ? { kind: 'warn', text: `已保存；其中 ${bad.length} 条用例当前不可执行：${bad.map((b) => `${b.case_id}(${b.reason})`).join('、')}` }
        : { kind: 'info', text: '方案已保存' });
      refresh();
    } catch (e) {
      setNotice({ kind: 'error', text: `保存失败：${msg(e)}` });
    } finally { setBusy(false); }
  };

  const createSuite = async (name: string) => {
    const caseIds = caseInput.split(/[,，\s]+/).filter(Boolean);
    setDialog(null);
    setBusy(true); setNotice(null);
    try {
      const out = await call<SuiteRow>('suite.create', { name, caseIds });
      pushToast('pass', `方案已创建：#${out.id} ${out.name}`);
      setCurrentId(out.id);
      setCaseInput('');
      refresh();
    } catch (e) {
      setNotice({ kind: 'error', text: `创建失败：${msg(e)}` });
    } finally { setBusy(false); }
  };

  const createTask = async (name: string) => {
    if (!current) return;
    setDialog(null);
    setBusy(true); setNotice(null);
    try {
      const out = await call<{ task_id: number; sync_status: string }>('task.create', {
        name, suiteId: current.id,
      });
      pushToast('pass', `任务已创建：#${out.task_id}（${out.sync_status}）`);
      navigate('tasks', { taskId: out.task_id });
    } catch (e) {
      setNotice({
        kind: 'error',
        text: `建任务失败：${msg(e)}（存在不可执行用例时，可到任务页用强制创建）`,
      });
    } finally { setBusy(false); }
  };

  /** 一键「建任务并执行」：省去"建任务 -> 切任务页 -> 点执行"三步。
   *  task.run 是阻塞 RPC：执行期间按钮互斥，完成后直接跳 run 详情。 */
  const createTaskAndRun = async (name: string) => {
    if (!current) return;
    setDialog(null);
    setBusy(true); setNotice(null);
    try {
      const t = await call<{ task_id: number }>('task.create', {
        name, suiteId: current.id,
      });
      pushToast('pass', `任务已创建：#${t.task_id}，开始执行…`);
      const stats = await call<{ run_id?: number }>('task.run', { taskId: t.task_id });
      const rid = Number(stats?.run_id ?? 0);
      if (rid > 0) navigate('run', { runId: rid });
      else navigate('tasks', { taskId: t.task_id });
    } catch (e) {
      setNotice({ kind: 'error', text: `建任务/执行失败：${msg(e)}` });
    } finally { setBusy(false); }
  };

  const deleteSuite = async () => {
    if (!current) return;
    setBusy(true);
    try {
      await call('suite.delete', { suiteId: current.id });
      pushToast('pass', `已删除方案「${current.name}」`);
      setConfirmDelete(false);
      setCurrentId(null);
      refresh();
    } catch (e) {
      setNotice({ kind: 'error', text: `删除失败：${msg(e)}` });
    } finally { setBusy(false); }
  };

  const move = (index: number, delta: number) => {
    if (!draft) return;
    const next = [...draft.caseIds];
    const target = index + delta;
    if (target < 0 || target >= next.length) return;
    [next[index], next[target]] = [next[target], next[index]];
    setDraft({ ...draft, caseIds: next });
  };

  return (
    <div className="main">
      <div className="page-head">
        <h2>测试方案</h2>
        <span className="hint">方案是有序用例集 + 前置条件 + 方案级驱动配置；建任务时展开为快照</span>
      </div>

      <div className="toolbar">
        <input type="text" style={{ width: 300 }} value={caseInput} placeholder="用例 ID，逗号分隔"
               onChange={(e) => setCaseInput(e.target.value)} />
        <button className="btn primary" disabled={busy} onClick={() => setDialog({ kind: 'create' })}>
          新建方案
        </button>
        <span className="spacer" />
        <button className="btn" onClick={refresh}>刷新</button>
      </div>

      {notice && <Msg kind={notice.kind}>{notice.text}</Msg>}
      {list.error && <Msg kind="error">读取方案失败：{list.error}</Msg>}

      <div className="row split">
        <div className="left">
          <table className="grid">
            <thead><tr><th>方案</th><th className="num">用例</th></tr></thead>
            <tbody>
              {suites.map((s) => (
                <tr key={s.id} className={`clickable ${s.id === currentId ? 'on' : ''}`} onClick={() => setCurrentId(s.id)}>
                  <td>{s.name}<div className="faint mono">#{s.id}</div></td>
                  <td className="num">{s.case_ids.length}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {suites.length === 0 && <Empty text="还没有方案：填用例 ID 后点『新建方案』" />}
        </div>

        <div className="right">
          {!current || !draft ? <Empty text="左侧选择一个方案" /> : (
            <div className="panel">
              <div className="hstack" style={{ marginBottom: 8 }}>
                <input type="text" style={{ width: 240 }} value={draft.name}
                       onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
                <select value={draft.mode} onChange={(e) => setDraft({ ...draft, mode: e.target.value })}>
                  {MODES.map((m) => <option key={m} value={m}>{m}</option>)}
                </select>
                {dirty && <span className="badge blocked" title="草稿与已保存版本不一致">未保存</span>}
                <button className="btn" disabled={busy || !dirty} onClick={save}>保存修改</button>
                <button className="btn" disabled={busy}
                        onClick={() => setDialog({ kind: 'task' })}>仅创建任务</button>
                <button className="btn primary" disabled={busy || dirty || !!current.not_runnable?.length}
                        title={dirty ? '请先保存方案修改' : current.not_runnable?.length ? '请先审核方案中的用例' : '使用当前方案开始测试'}
                        onClick={() => void createTaskAndRun(`${current.name} · ${new Date().toLocaleString('zh-CN')}`)}>开始测试 →</button>
                <span className="spacer" />
                <button className="btn danger" disabled={busy}
                        onClick={() => setConfirmDelete(true)}>删除</button>
              </div>

              <div className="row split">
                <div className="left">
                  <h3>有序用例</h3>
                  <table className="grid">
                    <thead><tr><th className="num">#</th><th>用例</th><th /></tr></thead>
                    <tbody>
                      {draft.caseIds.map((cid, i) => (
                        <tr key={`${cid}-${i}`}>
                          <td className="num">{i + 1}</td>
                          <td className="mono">{cid}</td>
                          <td style={{ whiteSpace: 'nowrap' }}>
                            <button className="copy" title="上移" onClick={() => move(i, -1)}>↑</button>
                            <button className="copy" title="下移" onClick={() => move(i, 1)}>↓</button>
                            <button className="copy" title="移出" onClick={() => setDraft({
                              ...draft, caseIds: draft.caseIds.filter((_, j) => j !== i),
                            })}>×</button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <div className="hstack" style={{ marginTop: 6 }}>
                    <input type="text" placeholder="追加用例 ID（输入可自动补全）" list="tp-all-cases"
                           onKeyDown={(e) => {
                             if (e.key !== 'Enter') return;
                             const v = (e.target as HTMLInputElement).value.trim();
                             if (!v) return;
                             setDraft({ ...draft, caseIds: [...draft.caseIds, ...v.split(/[,，\s]+/).filter(Boolean)] });
                             (e.target as HTMLInputElement).value = '';
                           }} />
                    <datalist id="tp-all-cases">
                      {(allCases.data?.cases ?? []).map((c) => (
                        <option key={c.case_id} value={c.case_id}>{c.title || c.case_id}</option>
                      ))}
                    </datalist>
                  </div>
                </div>

                <div className="right">
                  <h3>前置条件（模拟器场景等；缺省由成员用例汇总）</h3>
                  <Chips items={draft.preconditions} empty="（空）" />
                  <div className="hstack" style={{ marginTop: 6 }}>
                    <input type="text" placeholder="新增前置条件" onKeyDown={(e) => {
                      if (e.key !== 'Enter') return;
                      const v = (e.target as HTMLInputElement).value.trim();
                      if (!v) return;
                      setDraft({ ...draft, preconditions: [...draft.preconditions, v] });
                      (e.target as HTMLInputElement).value = '';
                    }} />
                  </div>
                  <div style={{ marginTop: 10 }}>
                    <div className="faint">可执行性</div>
                    {(current.not_runnable ?? []).length === 0
                      ? <span className="badge pass">全部可执行</span>
                      : (current.not_runnable ?? []).map((b) => (
                        <div key={b.case_id}>
                          <Badge status="blocked" /> <span className="mono">{b.case_id}</span> <span className="muted">{b.reason}</span>
                        </div>
                      ))}
                  </div>
                </div>
              </div>

              {confirmDelete && (
                <ConfirmBar
                  text={`确认删除方案「${current.name}」？此操作不可撤销。`}
                  busy={busy}
                  onConfirm={() => void deleteSuite()}
                  onCancel={() => setConfirmDelete(false)}
                />
              )}
            </div>
          )}
        </div>
      </div>

      <PromptModal
        open={dialog?.kind === 'create'}
        title="新建测试方案"
        label="方案名称"
        placeholder="例：冒烟回归"
        hint={caseInput.trim()
          ? `将纳入用例：${caseInput.trim()}`
          : '未填用例也可先建空方案，稍后在详情页添加'}
        confirmText="创建"
        busy={busy}
        onCancel={() => setDialog(null)}
        onSubmit={(name) => void createSuite(name)}
      />

      <PromptModal
        open={dialog?.kind === 'taskrun'}
        title="建任务并执行"
        label="任务名称"
        defaultValue={current ? `${current.name}-${new Date().toISOString().slice(0, 10)}` : ''}
        hint={current
          ? `以方案「${current.name}」（${current.case_ids.length} 条用例）建任务并立即执行；`
            + '执行为阻塞调用，期间本页按钮互斥'
          : undefined}
        confirmText="创建并执行"
        busy={busy}
        onCancel={() => setDialog(null)}
        onSubmit={(name) => void createTaskAndRun(name)}
      />

      <PromptModal
        open={dialog?.kind === 'task'}
        title="从方案创建任务"
        label="任务名称"
        defaultValue={current ? `${current.name}-${new Date().toISOString().slice(0, 10)}` : ''}
        hint={current
          ? `来源方案「${current.name}」（${current.case_ids.length} 条用例），建后可在任务页执行`
          : undefined}
        confirmText="创建并前往"
        busy={busy}
        onCancel={() => setDialog(null)}
        onSubmit={(name) => void createTask(name)}
      />
    </div>
  );
}
