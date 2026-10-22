import { useEffect, useMemo, useState } from 'react';

import { call } from '../bridge';
import { Msg } from '../components/Bits';
import { msg, navigate } from '../hooks';
import type {
  AutomationDraft, AutomationHistory, AutomationObservation, AutomationWindow,
} from '../types';

type InvokeReply<T> = { tool: string; result: T; history_count: number };

function exactTitle(title: string): string {
  return '^' + title.replace(/[.*+?^$()|[\]\\{}]/g, '\\$&') + '$';
}

export function AutomationPage() {
  const [windows, setWindows] = useState<AutomationWindow[]>([]);
  const [selectedWindow, setSelectedWindow] = useState('');
  const [exe, setExe] = useState('');
  const [titlePattern, setTitlePattern] = useState('');
  const [observation, setObservation] = useState<AutomationObservation | null>(null);
  const [queryResult, setQueryResult] = useState<AutomationObservation | null>(null);
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null);
  const [query, setQuery] = useState('');
  const [controlType, setControlType] = useState('');
  const [actionValue, setActionValue] = useState('');
  const [history, setHistory] = useState<AutomationHistory | null>(null);
  const [historyCount, setHistoryCount] = useState(0);
  const [draft, setDraft] = useState<AutomationDraft | null>(null);
  const [caseId, setCaseId] = useState('TC-EXPLORE');
  const [caseTitle, setCaseTitle] = useState('自动化探索路径');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [lastResult, setLastResult] = useState<Record<string, unknown> | null>(null);

  const current = queryResult ?? observation;
  const elements = current?.matches ?? current?.elements ?? [];
  const selected = useMemo(
    () => elements.find((e) => e.element_index === selectedIndex) ?? null,
    [elements, selectedIndex],
  );
  const highlight = useMemo(() => {
    if (queryResult || !selected || !observation) return null;
    const base = observation.screenshot_rect ?? observation.window_rect;
    const rect = selected.rect;
    if (!base || base.length !== 4 || !rect || rect.length !== 4 || base[2] <= 0 || base[3] <= 0)
      return null;
    const left = ((rect[0] - base[0]) / base[2]) * 100;
    const top = ((rect[1] - base[1]) / base[3]) * 100;
    const width = (rect[2] / base[2]) * 100;
    const height = (rect[3] / base[3]) * 100;
    if (left > 100 || top > 100 || left + width < 0 || top + height < 0) return null;
    return {
      left: Math.max(0, left) + '%',
      top: Math.max(0, top) + '%',
      width: Math.max(0.4, Math.min(100 - Math.max(0, left), width)) + '%',
      height: Math.max(0.4, Math.min(100 - Math.max(0, top), height)) + '%',
    };
  }, [queryResult, selected, observation]);

  const invoke = async <T,>(tool: string, args: Record<string, unknown> = {}): Promise<T> => {
    const out = await call<InvokeReply<T>>('automation.invoke', { tool, args });
    setHistoryCount(out.history_count);
    return out.result;
  };

  const refreshHistory = async () => {
    const out = await call<{ session: AutomationHistory }>('automation.history');
    setHistory(out.session);
    setHistoryCount(out.session.steps.length);
  };

  useEffect(() => { void refreshHistory().catch(() => {}); }, []);

  const run = async (label: string, fn: () => Promise<void>) => {
    if (busy) return;
    setBusy(label);
    setError('');
    try {
      await fn();
      await refreshHistory();
    } catch (e) {
      setError(msg(e));
    } finally {
      setBusy('');
    }
  };

  const observeNow = async (preserveKey?: string) => {
    const out = await invoke<AutomationObservation>('ui.observe', {
      include_ax: true,
      include_screenshot: true,
      max_nodes: 500,
      max_depth: 16,
    });
    setObservation(out);
    setQueryResult(null);
    const replacement = preserveKey
      ? (out.elements ?? []).find((item) => item.stable_key === preserveKey)
      : undefined;
    setSelectedIndex(replacement?.element_index ?? null);
  };

  const loadWindows = () => run('正在读取桌面窗口…', async () => {
    const out = await invoke<{ windows: AutomationWindow[] }>('app.list', { limit: 200 });
    setWindows(out.windows);
    if (!selectedWindow && out.windows.length) setSelectedWindow(out.windows[0].title);
  });

  const attach = () => run('正在附加窗口…', async () => {
    const title = titlePattern.trim() || selectedWindow.trim();
    if (!title) throw new Error('请先选择窗口或填写标题正则');
    await invoke('app.attach', {
      title_re: titlePattern.trim() ? titlePattern.trim() : exactTitle(title),
      timeout_s: 10,
    });
    await observeNow();
  });

  const launch = () => run('正在启动被测程序…', async () => {
    if (!exe.trim()) throw new Error('请填写程序路径；仅附加已运行窗口请使用“附加窗口”');
    const args: Record<string, unknown> = { exe: exe.trim(), timeout_s: 30 };
    if (titlePattern.trim()) args.title_re = titlePattern.trim();
    await invoke('app.launch', args);
    await observeNow();
  });

  const observe = () => run('正在观察界面…', observeNow);

  const doQuery = () => run('正在查询元素…', async () => {
    if (!query.trim() && !controlType.trim()) throw new Error('请输入文本或控件类型');
    const out = await invoke<AutomationObservation>('ui.query', {
      text: query.trim() || undefined,
      control_type: controlType.trim() || undefined,
      limit: 100,
    });
    setQueryResult(out);
    setSelectedIndex(null);
  });

  const elementArgs = (): Record<string, unknown> => {
    if (!selected || !current) throw new Error('请先选择一个 UI 元素');
    return {
      element_index: selected.element_index,
      snapshot_id: current.snapshot_id,
    };
  };

  const action = (tool: string, extra: Record<string, unknown> = {}, refresh = true) =>
    run('正在执行 ' + tool + '…', async () => {
      const keepKey = selected?.stable_key;
      const result = await invoke<Record<string, unknown>>(tool, { ...elementArgs(), ...extra });
      if (refresh) await observeNow(keepKey);
      setLastResult(result);
    });

  const removeHistoryStep = (index: number) =>
    run('正在移除探索步骤…', async () => {
      await call('automation.removeStep', { index });
      setDraft(null);
    });

  const generateDraft = () => run('正在生成 DSL 草稿…', async () => {
    const out = await call<AutomationDraft>('automation.draft', {
      caseId: caseId.trim() || 'TC-EXPLORE',
      title: caseTitle.trim() || '自动化探索路径',
      format: 'json',
    });
    setDraft(out);
  });

  const importDraft = () => run('正在保存待审用例…', async () => {
    if (!draft) throw new Error('请先生成 DSL 草稿');
    if (!draft.case.steps.length) throw new Error('当前探索历史没有可固化的执行步骤');
    const all = await call<{ cases: { case_id: string; status: string }[] }>('case.list');
    const existing = all.cases.find((item) => item.case_id === draft.case.case_id);
    if (existing?.status === 'active')
      throw new Error('该用例 ID 已存在且已生效，请换一个用例 ID，避免覆盖正式回归用例');
    await call('case.import', {
      text: draft.text,
      format: 'json',
      caseId: draft.case.case_id,
    });
    navigate('dsl', { caseId: draft.case.case_id });
  });

  const reset = () => run('正在清空探索会话…', async () => {
    await call('automation.reset', {
      caseId: caseId.trim() || 'TC-EXPLORE',
      title: caseTitle.trim() || '自动化探索路径',
    });
    setObservation(null);
    setQueryResult(null);
    setSelectedIndex(null);
    setHistory(null);
    setHistoryCount(0);
    setDraft(null);
    setLastResult(null);
  });

  return <div className="main automation-page">
    <div className="page-head">
      <div>
        <h2>自动化探索</h2>
        <p className="muted">选择目标窗口，观察 UIA 结构，直接操作控件，并把成功路径固化为待审 DSL。</p>
      </div>
      <span className="spacer" />
      <span className="chip">会话 {historyCount} 步</span>
      <button className="btn ghost" disabled={!!busy} onClick={() => void reset()}>清空会话</button>
    </div>

    {busy && <Msg kind="info">{busy}</Msg>}
    {error && <Msg kind="error">{error}</Msg>}

    <section className="panel automation-target">
      <div className="automation-section-head">
        <div><strong>1. 选择被测窗口</strong><span className="muted">支持附加已运行窗口或启动程序</span></div>
        <button className="btn" disabled={!!busy} onClick={() => void loadWindows()}>刷新窗口</button>
      </div>
      <div className="automation-target-grid">
        <label>可见窗口
          <select value={selectedWindow} onChange={(e) => setSelectedWindow(e.target.value)}>
            <option value="">请选择窗口</option>
            {windows.map((w) => <option key={String(w.handle) + '-' + w.title} value={w.title}>
              {(w.title || '(无标题)') + (w.pid ? ' · PID ' + w.pid : '')}
            </option>)}
          </select>
        </label>
        <label>标题正则（可选）
          <input value={titlePattern} onChange={(e) => setTitlePattern(e.target.value)}
                 placeholder="例如：.*仪器控制软件.*" />
        </label>
        <label>程序路径（启动时填写）
          <input value={exe} onChange={(e) => setExe(e.target.value)}
                 placeholder="D:\\Software\\Instrument.exe" />
        </label>
        <div className="automation-target-actions">
          <button className="btn primary" disabled={!!busy || (!selectedWindow && !titlePattern.trim())}
                  onClick={() => void attach()}>附加窗口</button>
          <button className="btn" disabled={!!busy || !exe.trim()}
                  onClick={() => void launch()}>启动程序</button>
          <button className="btn" disabled={!!busy || !observation}
                  onClick={() => void observe()}>重新观察</button>
        </div>
      </div>
    </section>

    <section className="automation-workspace">
      <div className="panel automation-elements">
        <div className="automation-section-head">
          <div>
            <strong>2. UI 元素</strong>
            <span className="muted">
              {current ? 'snapshot #' + current.snapshot_id + ' · ' + elements.length + ' 个元素' : '先附加窗口并观察'}
            </span>
          </div>
          {current?.truncated && <span className="badge blocked">已截断</span>}
        </div>
        <div className="automation-query">
          <input value={query} onChange={(e) => setQuery(e.target.value)}
                 placeholder="按名称 / 当前值搜索" onKeyDown={(e) => {
                   if (e.key === 'Enter') void doQuery();
                 }} />
          <input value={controlType} onChange={(e) => setControlType(e.target.value)}
                 placeholder="控件类型，如 Button" />
          <button className="btn" disabled={!!busy} onClick={() => void doQuery()}>查询</button>
          {queryResult && <button className="btn ghost" onClick={() => {
            setQueryResult(null); setSelectedIndex(null);
          }}>返回完整树</button>}
        </div>
        {queryResult && <div className="automation-query-note">当前是定向查询结果；旧截图属于前一个 snapshot，因此不与当前元素坐标混用。</div>}
        <div className="automation-element-list">
          {elements.map((el) => <button key={el.element_index}
            className={selectedIndex === el.element_index ? 'automation-element selected' : 'automation-element'}
            style={{ paddingLeft: 10 + Math.min(el.depth, 8) * 12 }}
            onClick={() => { setSelectedIndex(el.element_index); setActionValue(el.value ?? ''); }}>
            <span className="automation-index">{el.element_index}</span>
            <span className="automation-element-main">
              <strong>{el.name || el.automation_id || '(未命名元素)'}</strong>
              <small>{(el.control_type || 'Unknown') + (el.automation_id ? ' · #' + el.automation_id : '')}</small>
            </span>
            {el.value && <span className="automation-value">{el.value}</span>}
          </button>)}
          {!elements.length && <div className="automation-empty">暂无 UI 元素。附加窗口后会自动观察界面。</div>}
        </div>
      </div>

      <div className="automation-right">
        <div className="panel automation-preview">
          <div className="automation-section-head">
            <div><strong>界面预览</strong><span className="muted">{observation?.window_title || '当前窗口'}</span></div>
            {observation?.screenshot_bytes ? <span className="chip">{Math.round(observation.screenshot_bytes / 1024)} KB</span> : null}
          </div>
          {!queryResult && observation?.png_base64
            ? <div className="automation-canvas">
                <img src={'data:image/png;base64,' + observation.png_base64} alt="被测窗口截图" />
                {highlight && <div className="automation-highlight" style={highlight}>
                  <span>{selected?.name || selected?.automation_id || selected?.control_type || '元素'}</span>
                </div>}
              </div>
            : <div className="automation-preview-empty">{queryResult ? '查询模式不展示旧截图' : '暂无截图'}</div>}
        </div>

        <div className="panel automation-actions">
          <div className="automation-section-head">
            <div><strong>3. 操作选中元素</strong><span className="muted">{selected?.stable_key || '未选择元素'}</span></div>
          </div>
          {selected ? <>
            <div className="automation-meta">
              <code>{JSON.stringify(selected.selector ?? {})}</code>
              <span>{selected.rect?.join(', ')}</span>
            </div>
            <input value={actionValue} onChange={(e) => setActionValue(e.target.value)}
                   placeholder="输入 / 选择 / 按键的值" />
            <div className="automation-action-grid">
              <button className="btn primary" disabled={!selected.actions.includes('click')}
                      onClick={() => void action('ui.click')}>点击</button>
              <button className="btn" onClick={() => void action('ui.read', {}, false)}>读取</button>
              <button className="btn" onClick={() => void action('assert.ui', {
                expect: actionValue, op: 'equals',
              }, false)}>断言文本</button>
              <button className="btn" disabled={!selected.actions.includes('input')}
                      onClick={() => void action('ui.input', { text: actionValue, clear: true })}>输入</button>
              <button className="btn" disabled={!selected.actions.includes('set_value')}
                      onClick={() => void action('ui.set_value', { value: actionValue })}>设值并校验</button>
              <button className="btn" disabled={!selected.actions.includes('select')}
                      onClick={() => void action('ui.select', { value: actionValue })}>选择</button>
              <button className="btn" onClick={() => void action('ui.keypress', { keys: actionValue })}>发送按键</button>
              <button className="btn" disabled={!selected.actions.includes('toggle')}
                      onClick={() => void action('ui.toggle', { checked: true })}>设为选中</button>
              <button className="btn" disabled={!selected.actions.includes('toggle')}
                      onClick={() => void action('ui.toggle', { checked: false })}>设为未选中</button>
            </div>
            {lastResult && <pre className="automation-result">{JSON.stringify(lastResult, null, 2)}</pre>}
          </> : <div className="automation-empty">从左侧选择元素后可执行动作。</div>}
        </div>
      </div>
    </section>

    <section className="automation-bottom">
      <div className="panel automation-history">
        <div className="automation-section-head">
          <div><strong>4. 操作历史</strong><span className="muted">观察步骤保留证据，但不会写入正式 DSL</span></div>
          <button className="btn ghost" onClick={() => void refreshHistory()}>刷新</button>
        </div>
        <div className="automation-history-list">
          {(history?.steps ?? []).map((s) => <div key={s.index} className={s.ok ? 'automation-history-row' : 'automation-history-row failed'}>
            <span>{s.index}</span><code>{s.tool}</code><span>{s.ms} ms</span>
            <span className="muted">{s.driver || '—'}</span>
            <span>{s.ok ? '成功' : s.error || '失败'}</span>
            <button className="btn ghost small" disabled={!!busy}
                    onClick={() => void removeHistoryStep(s.index)}>移除</button>
          </div>)}
          {!history?.steps.length && <div className="automation-empty">暂无操作记录。</div>}
        </div>
      </div>

      <div className="panel automation-draft">
        <div className="automation-section-head">
          <div><strong>5. 固化为 DSL</strong><span className="muted">临时 element_index 会转换成稳定 locator 候选</span></div>
        </div>
        <div className="form-2col">
          <label>用例 ID<input value={caseId} onChange={(e) => setCaseId(e.target.value)} /></label>
          <label>标题<input value={caseTitle} onChange={(e) => setCaseTitle(e.target.value)} /></label>
        </div>
        <div className="hstack">
          <button className="btn" disabled={!!busy || historyCount === 0} onClick={() => void generateDraft()}>生成草稿</button>
          <button className="btn primary" disabled={!draft || draft.case.steps.length === 0 || !!busy}
                  onClick={() => void importDraft()}>保存并进入审核</button>
        </div>
        {draft && <textarea className="automation-draft-text" readOnly value={draft.text} />}
      </div>
    </section>
  </div>;
}
