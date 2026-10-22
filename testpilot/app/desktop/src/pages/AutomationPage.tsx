import { useEffect, useMemo, useState } from 'react';

import { call } from '../bridge';
import { Msg } from '../components/Bits';
import { msg, navigate } from '../hooks';
import type {
  AutomationDraft,
  AutomationHistory,
  AutomationHistoryStep,
  AutomationObservation,
  AutomationWindow,
} from '../types';

type InvokeReply<T> = { tool: string; result: T; history_count: number };
type ExploreMode = 'uia' | 'vision' | 'device';
type StateOp = 'equals' | 'contains' | 'regex';

function exactTitle(title: string): string {
  return '^' + title.replace(/[.*+?^$()|[\]\\{}]/g, '\\$&') + '$';
}

function qualityText(quality?: string): string {
  if (quality === 'high') return '高';
  if (quality === 'medium') return '中';
  if (quality === 'low') return '低';
  return '—';
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

  const [exploreMode, setExploreMode] = useState<ExploreMode>('uia');
  const [toolNames, setToolNames] = useState<string[]>([]);
  const [driverNames, setDriverNames] = useState<string[]>([]);
  const [emulatorAvailable, setEmulatorAvailable] = useState(false);

  const [visualDesc, setVisualDesc] = useState('');
  const [visualTemplate, setVisualTemplate] = useState('');
  const [visualKey, setVisualKey] = useState('');
  const [visualRect, setVisualRect] = useState<number[] | null>(null);
  const [visionExpect, setVisionExpect] = useState('');

  const [deviceScene, setDeviceScene] = useState('normal_sample_a');
  const [statePath, setStatePath] = useState('/state/run');
  const [stateExpect, setStateExpect] = useState('completed');
  const [stateOp, setStateOp] = useState<StateOp>('equals');
  const [deviceCmd, setDeviceCmd] = useState('query_state');
  const [deviceArgsText, setDeviceArgsText] = useState('{"path":"/state/run"}');

  const [history, setHistory] = useState<AutomationHistory | null>(null);
  const [historyCount, setHistoryCount] = useState(0);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [editingArgs, setEditingArgs] = useState('');

  const [draft, setDraft] = useState<AutomationDraft | null>(null);
  const [caseId, setCaseId] = useState('TC-EXPLORE');
  const [caseTitle, setCaseTitle] = useState('自动化探索路径');

  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [lastResult, setLastResult] = useState<Record<string, unknown> | null>(null);

  const current = queryResult ?? observation;
  const elements = current?.matches ?? current?.elements ?? [];
  const selected = useMemo(
    () => elements.find((item) => item.element_index === selectedIndex) ?? null,
    [elements, selectedIndex],
  );

  const highlight = useMemo(() => {
    if (queryResult || !observation) return null;
    const rect = exploreMode === 'vision' ? visualRect : selected?.rect;
    const base = observation.screenshot_rect ?? observation.window_rect;
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
  }, [exploreMode, observation, queryResult, selected, visualRect]);

  const invoke = async <T,>(
    tool: string,
    args: Record<string, unknown> = {},
  ): Promise<T> => {
    const out = await call<InvokeReply<T>>('automation.invoke', { tool, args });
    setHistoryCount(out.history_count);
    return out.result;
  };

  const refreshHistory = async () => {
    const out = await call<{ session: AutomationHistory }>('automation.history');
    setHistory(out.session);
    setHistoryCount(out.session.steps.length);
  };

  const refreshCapabilities = async () => {
    const out = await call<{
      tools: { name: string }[];
      drivers?: string[];
      emulator?: boolean;
    }>('automation.tools');
    setToolNames(out.tools.map((item) => item.name));
    setDriverNames(out.drivers ?? []);
    setEmulatorAvailable(Boolean(out.emulator));
  };

  useEffect(() => {
    void refreshHistory().catch(() => {});
    void refreshCapabilities().catch(() => {});
  }, []);

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
    if (!selectedWindow && out.windows.length)
      setSelectedWindow(String(out.windows[0].handle));
  });

  const attach = () => run('正在附加窗口…', async () => {
    const picked = windows.find((item) => String(item.handle) === selectedWindow);
    const title = titlePattern.trim() || picked?.title?.trim() || '';
    if (!picked && !title) throw new Error('请先选择窗口或填写标题正则');
    await invoke('app.attach', {
      handle: picked?.handle,
      title_re: titlePattern.trim() ? titlePattern.trim() : (title ? exactTitle(title) : undefined),
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
    if (!query.trim() && !controlType.trim())
      throw new Error('请输入文本或控件类型');
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

  const elementAction = (
    tool: string,
    extra: Record<string, unknown> = {},
    refresh = true,
  ) => run('正在执行 ' + tool + '…', async () => {
    const keepKey = selected?.stable_key;
    const result = await invoke<Record<string, unknown>>(tool, {
      ...elementArgs(),
      ...extra,
    });
    if (refresh) await observeNow(keepKey);
    setLastResult(result);
  });

  const visualArgs = (): Record<string, unknown> => {
    const desc = visualDesc.trim();
    const template = visualTemplate.trim();
    if (!desc && !template) throw new Error('请填写视觉描述或模板路径');
    return {
      driver: 'vision',
      key: visualKey.trim() || undefined,
      desc: desc || undefined,
      template: template || undefined,
      timeout_ms: 10000,
    };
  };

  const visualAction = (tool: 'ui.locate' | 'ui.click' | 'ui.input') =>
    run('正在执行视觉自动化…', async () => {
      const args = visualArgs();
      if (tool === 'ui.input') args.text = actionValue;
      const result = await invoke<Record<string, unknown>>(tool, args);
      const rect = result.rect;
      setVisualRect(Array.isArray(rect) ? rect.map(Number) : null);
      setLastResult(result);
      if (tool !== 'ui.locate') await observeNow();
    });

  const visualAssert = () => run('正在执行视觉断言…', async () => {
    if (!visionExpect.trim()) throw new Error('请填写视觉期望');
    const result = await invoke<Record<string, unknown>>('assert.vision', {
      expect: visionExpect.trim(),
      timeout_ms: 10000,
    });
    setLastResult(result);
  });

  const deviceAction = (tool: string, args: Record<string, unknown>) =>
    run('正在执行 ' + tool + '…', async () => {
      const result = await invoke<Record<string, unknown>>(tool, args);
      setLastResult(result);
    });

  const parseDeviceArgs = (): Record<string, unknown> => {
    let value: unknown;
    try {
      value = JSON.parse(deviceArgsText || '{}');
    } catch {
      throw new Error('设备指令参数必须是合法 JSON');
    }
    if (!value || typeof value !== 'object' || Array.isArray(value))
      throw new Error('设备指令参数必须是 JSON 对象');
    return value as Record<string, unknown>;
  };

  const editHistoryStep = (step: AutomationHistoryStep) => {
    setEditingIndex(step.index);
    setEditingArgs(JSON.stringify(step.args ?? {}, null, 2));
  };

  const saveHistoryStep = () => run('正在更新探索步骤…', async () => {
    if (editingIndex == null) return;
    let args: unknown;
    try {
      args = JSON.parse(editingArgs || '{}');
    } catch {
      throw new Error('步骤参数必须是合法 JSON');
    }
    if (!args || typeof args !== 'object' || Array.isArray(args))
      throw new Error('步骤参数必须是 JSON 对象');
    await call('automation.updateStep', {
      index: editingIndex,
      args: args as Record<string, unknown>,
    });
    setEditingIndex(null);
    setEditingArgs('');
    setDraft(null);
  });

  const moveHistoryStep = (index: number, toIndex: number) =>
    run('正在调整步骤顺序…', async () => {
      await call('automation.moveStep', { index, toIndex });
      setDraft(null);
    });

  const removeHistoryStep = (index: number) =>
    run('正在移除探索步骤…', async () => {
      await call('automation.removeStep', { index });
      if (editingIndex === index) {
        setEditingIndex(null);
        setEditingArgs('');
      }
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
    if (!draft.case.steps.length)
      throw new Error('当前探索历史没有可固化的执行步骤');

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
    setVisualRect(null);
    setHistory(null);
    setHistoryCount(0);
    setEditingIndex(null);
    setEditingArgs('');
    setDraft(null);
    setLastResult(null);
  });

  const setMode = (mode: ExploreMode) => {
    setExploreMode(mode);
    setQueryResult(null);
    setLastResult(null);
  };

  const visualAvailable = driverNames.includes('vision');
  const uiaAvailable = driverNames.includes('uia');

  return <div className="main automation-page">
    <div className="page-head">
      <div>
        <h2>自动化探索</h2>
        <p className="muted">UIA + 视觉 + 设备状态一处探索，成功路径可直接固化为待审 DSL。</p>
      </div>
      <span className="spacer" />
      <span className="chip">会话 {historyCount} 步</span>
      <button className="btn ghost" disabled={!!busy} onClick={() => void refreshCapabilities()}>
        刷新能力
      </button>
      <button className="btn ghost" disabled={!!busy} onClick={() => void reset()}>
        清空会话
      </button>
    </div>

    {busy && <Msg kind="info">{busy}</Msg>}
    {error && <Msg kind="error">{error}</Msg>}

    <section className="panel automation-target">
      <div className="automation-section-head">
        <div>
          <strong>1. 选择被测窗口</strong>
          <span className="muted">支持 HWND 精确附加，标题正则保留用于 DSL 固化</span>
        </div>
        <button className="btn" disabled={!!busy || !toolNames.includes('app.list')}
                onClick={() => void loadWindows()}>
          刷新窗口
        </button>
      </div>
      <div className="automation-target-grid">
        <label>可见窗口
          <select value={selectedWindow} onChange={(e) => setSelectedWindow(e.target.value)}>
            <option value="">请选择窗口</option>
            {windows.map((item) =>
              <option key={String(item.handle) + '-' + item.title} value={String(item.handle)}>
                {(item.title || '(无标题)') + (item.pid ? ' · PID ' + item.pid : '')}
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
          <button className="btn primary"
                  disabled={!!busy || !toolNames.includes('app.attach') ||
                    (!selectedWindow && !titlePattern.trim())}
                  onClick={() => void attach()}>
            附加窗口
          </button>
          <button className="btn" disabled={!!busy || !toolNames.includes('app.launch') || !exe.trim()}
                  onClick={() => void launch()}>
            启动程序
          </button>
          <button className="btn" disabled={!!busy || !observation || !toolNames.includes('ui.observe')}
                  onClick={() => void observe()}>
            重新观察
          </button>
        </div>
      </div>
    </section>

    <section className="panel automation-modebar">
      <div>
        <strong>2. 选择探索方式</strong>
        <span className="muted">按控件结构、视觉语义或设备状态构建同一条测试路径</span>
      </div>
      <div className="automation-mode-tabs">
        <button className={exploreMode === 'uia' ? 'on' : ''}
                disabled={!uiaAvailable}
                onClick={() => setMode('uia')}>
          UIA 控件
        </button>
        <button className={exploreMode === 'vision' ? 'on' : ''}
                disabled={!visualAvailable}
                onClick={() => setMode('vision')}>
          视觉定位
        </button>
        <button className={exploreMode === 'device' ? 'on' : ''}
                disabled={!emulatorAvailable}
                onClick={() => setMode('device')}>
          设备 / 状态
        </button>
      </div>
    </section>

    <section className="automation-workspace">
      {exploreMode === 'uia' && <div className="panel automation-elements">
        <div className="automation-section-head">
          <div>
            <strong>3. UIA 元素</strong>
            <span className="muted">
              {current
                ? 'snapshot #' + current.snapshot_id + ' · ' + elements.length + ' 个元素'
                : '先附加窗口并观察'}
            </span>
          </div>
          {current?.truncated && <span className="badge blocked">已截断</span>}
        </div>

        <div className="automation-query">
          <input value={query} onChange={(e) => setQuery(e.target.value)}
                 placeholder="按名称 / 当前值搜索"
                 onKeyDown={(e) => { if (e.key === 'Enter') void doQuery(); }} />
          <input value={controlType} onChange={(e) => setControlType(e.target.value)}
                 placeholder="控件类型，如 Button" />
          <button className="btn" disabled={!!busy} onClick={() => void doQuery()}>
            查询
          </button>
          {queryResult && <button className="btn ghost" onClick={() => {
            setQueryResult(null);
            setSelectedIndex(null);
          }}>
            返回完整树
          </button>}
        </div>

        {queryResult && <div className="automation-query-note">
          当前是定向查询结果；旧截图属于前一个 snapshot，因此不与当前元素坐标混用。
        </div>}

        <div className="automation-element-list">
          {elements.map((item) =>
            <button key={item.element_index}
                    className={selectedIndex === item.element_index
                      ? 'automation-element selected'
                      : 'automation-element'}
                    style={{ paddingLeft: 10 + Math.min(item.depth, 8) * 12 }}
                    onClick={() => {
                      setSelectedIndex(item.element_index);
                      setActionValue(item.value ?? '');
                    }}>
              <span className="automation-index">{item.element_index}</span>
              <span className="automation-element-main">
                <strong>{item.name || item.automation_id || '(未命名元素)'}</strong>
                <small>
                  {(item.control_type || 'Unknown') +
                    (item.automation_id ? ' · #' + item.automation_id : '')}
                </small>
              </span>
              <span className={'selector-quality ' + (item.selector_quality ?? 'low')}
                    title={item.selector_reason ?? ''}>
                {qualityText(item.selector_quality)}
              </span>
              {item.value && <span className="automation-value">{item.value}</span>}
            </button>)}
          {!elements.length && <div className="automation-empty">
            暂无 UI 元素。附加窗口后会自动观察界面。
          </div>}
        </div>
      </div>}

      {exploreMode === 'vision' && <div className="panel automation-elements automation-vision-panel">
        <div className="automation-section-head">
          <div>
            <strong>3. 视觉定位</strong>
            <span className="muted">适合 QML Canvas、自绘控件或 Accessibility 不完整区域</span>
          </div>
          {!visualAvailable && <span className="badge blocked">Vision 不可用</span>}
        </div>

        <label>稳定语义键（可选）
          <input value={visualKey} onChange={(e) => setVisualKey(e.target.value)}
                 placeholder="例如：btn.export_result" />
        </label>
        <label>视觉描述
          <textarea value={visualDesc} onChange={(e) => setVisualDesc(e.target.value)}
                    placeholder="例如：结果页右下角蓝色“导出报告”按钮" />
        </label>
        <label>模板路径（与视觉描述二选一）
          <input value={visualTemplate} onChange={(e) => setVisualTemplate(e.target.value)}
                 placeholder="例如：export_report.png" />
        </label>
        <label>输入值
          <input value={actionValue} onChange={(e) => setActionValue(e.target.value)}
                 placeholder="视觉输入时使用" />
        </label>

        <div className="automation-action-grid visual">
          <button className="btn" disabled={!!busy || !visualAvailable}
                  onClick={() => void visualAction('ui.locate')}>
            定位并高亮
          </button>
          <button className="btn primary" disabled={!!busy || !visualAvailable}
                  onClick={() => void visualAction('ui.click')}>
            视觉点击
          </button>
          <button className="btn" disabled={!!busy || !visualAvailable}
                  onClick={() => void visualAction('ui.input')}>
            定位并输入
          </button>
        </div>

        <div className="automation-divider" />

        <label>视觉断言
          <textarea value={visionExpect} onChange={(e) => setVisionExpect(e.target.value)}
                    placeholder="例如：页面显示检测完成弹窗，且状态图标为绿色" />
        </label>
        <button className="btn primary"
                disabled={!!busy || !toolNames.includes('assert.vision') || !visionExpect.trim()}
                onClick={() => void visualAssert()}>
          立即验证并记录断言
        </button>

        <div className="automation-tip">
          视觉操作固化后会自动标记为 vision_only，避免回归时先拿 UIA 解释视觉 locator。
        </div>
      </div>}

      {exploreMode === 'device' && <div className="panel automation-elements automation-device-panel">
        <div className="automation-section-head">
          <div>
            <strong>3. 设备与状态</strong>
            <span className="muted">场景、指令、状态查询与可固化断言</span>
          </div>
          {!emulatorAvailable && <span className="badge blocked">模拟器不可用</span>}
        </div>

        <div className="automation-device-section">
          <strong>场景</strong>
          <input value={deviceScene} onChange={(e) => setDeviceScene(e.target.value)}
                 placeholder="normal_sample_a" />
          <button className="btn" disabled={!!busy || !toolNames.includes('device.scene') || !deviceScene.trim()}
                  onClick={() => void deviceAction('device.scene', {
                    scene_id: deviceScene.trim(),
                  })}>
            加载并记录场景
          </button>
        </div>

        <div className="automation-divider" />

        <div className="automation-device-section">
          <strong>状态查询 / 断言</strong>
          <input value={statePath} onChange={(e) => setStatePath(e.target.value)}
                 placeholder="/state/run" />
          <input value={stateExpect} onChange={(e) => setStateExpect(e.target.value)}
                 placeholder="期望值" />
          <select value={stateOp} onChange={(e) => setStateOp(e.target.value as StateOp)}>
            <option value="equals">等于</option>
            <option value="contains">包含</option>
            <option value="regex">正则</option>
          </select>
          <div className="hstack">
            <button className="btn" disabled={!!busy || !toolNames.includes('device.state') || !statePath.trim()}
                    onClick={() => void deviceAction('device.state', {
                      path: statePath.trim(),
                    })}>
              查询状态
            </button>
            <button className="btn primary"
                    disabled={!!busy || !toolNames.includes('assert.state') || !statePath.trim()}
                    onClick={() => void deviceAction('assert.state', {
                      path: statePath.trim(),
                      expect: stateExpect,
                      op: stateOp,
                    })}>
              验证并记录断言
            </button>
          </div>
        </div>

        <div className="automation-divider" />

        <div className="automation-device-section">
          <strong>设备指令</strong>
          <input value={deviceCmd} onChange={(e) => setDeviceCmd(e.target.value)}
                 placeholder="query_state" />
          <textarea value={deviceArgsText} onChange={(e) => setDeviceArgsText(e.target.value)}
                    placeholder='{"path":"/state/run"}' />
          <button className="btn" disabled={!!busy || !toolNames.includes('device.call') || !deviceCmd.trim()}
                  onClick={() => void deviceAction('device.call', {
                    cmd: deviceCmd.trim(),
                    args: parseDeviceArgs(),
                  })}>
            执行并记录指令
          </button>
        </div>
      </div>}

      <div className="automation-right">
        <div className="panel automation-preview">
          <div className="automation-section-head">
            <div>
              <strong>界面预览</strong>
              <span className="muted">{observation?.window_title || '当前窗口'}</span>
            </div>
            {observation?.screenshot_bytes
              ? <span className="chip">{Math.round(observation.screenshot_bytes / 1024)} KB</span>
              : null}
          </div>

          {!queryResult && observation?.png_base64
            ? <div className="automation-canvas">
                <img src={'data:image/png;base64,' + observation.png_base64} alt="被测窗口截图" />
                {highlight && <div className="automation-highlight" style={highlight}>
                  <span>
                    {exploreMode === 'vision'
                      ? (visualDesc || visualTemplate || '视觉目标')
                      : (selected?.name || selected?.automation_id || selected?.control_type || '元素')}
                  </span>
                </div>}
              </div>
            : <div className="automation-preview-empty">
                {queryResult ? '查询模式不展示旧截图' : '暂无截图'}
              </div>}
        </div>

        <div className="panel automation-actions">
          {exploreMode === 'uia' && <>
            <div className="automation-section-head">
              <div>
                <strong>4. 操作选中元素</strong>
                <span className="muted">{selected?.stable_key || '未选择元素'}</span>
              </div>
            </div>
            {selected ? <>
              <div className="automation-meta">
                <div>
                  <code>{JSON.stringify(selected.selector ?? {})}</code>
                  <small>
                    locator 稳定度 {qualityText(selected.selector_quality)}
                    {selected.selector_score != null ? ' · ' + selected.selector_score.toFixed(2) : ''}
                    {selected.selector_reason ? ' · ' + selected.selector_reason : ''}
                  </small>
                </div>
                <span>{selected.rect?.join(', ')}</span>
              </div>
              <input value={actionValue} onChange={(e) => setActionValue(e.target.value)}
                     placeholder="输入 / 选择 / 按键 / 断言的值" />
              <div className="automation-action-grid">
                <button className="btn primary"
                        disabled={!selected.actions.includes('click') || !toolNames.includes('ui.click')}
                        onClick={() => void elementAction('ui.click')}>
                  点击
                </button>
                <button className="btn" disabled={!toolNames.includes('ui.read')}
                        onClick={() => void elementAction('ui.read', {}, false)}>
                  读取
                </button>
                <button className="btn" disabled={!toolNames.includes('assert.ui')}
                        onClick={() => void elementAction('assert.ui', {
                          expect: actionValue,
                          op: 'equals',
                        }, false)}>
                  断言文本
                </button>
                <button className="btn"
                        disabled={!selected.actions.includes('input') || !toolNames.includes('ui.input')}
                        onClick={() => void elementAction('ui.input', {
                          text: actionValue,
                          clear: true,
                        })}>
                  输入
                </button>
                <button className="btn"
                        disabled={!selected.actions.includes('set_value') || !toolNames.includes('ui.set_value')}
                        onClick={() => void elementAction('ui.set_value', {
                          value: actionValue,
                        })}>
                  设值并校验
                </button>
                <button className="btn"
                        disabled={!selected.actions.includes('select') || !toolNames.includes('ui.select')}
                        onClick={() => void elementAction('ui.select', {
                          value: actionValue,
                        })}>
                  选择
                </button>
                <button className="btn" disabled={!toolNames.includes('ui.keypress')}
                        onClick={() => void elementAction('ui.keypress', {
                          keys: actionValue,
                        })}>
                  发送按键
                </button>
                <button className="btn"
                        disabled={!selected.actions.includes('toggle') || !toolNames.includes('ui.toggle')}
                        onClick={() => void elementAction('ui.toggle', { checked: true })}>
                  设为选中
                </button>
                <button className="btn"
                        disabled={!selected.actions.includes('toggle') || !toolNames.includes('ui.toggle')}
                        onClick={() => void elementAction('ui.toggle', { checked: false })}>
                  设为未选中
                </button>
              </div>
            </> : <div className="automation-empty">从左侧选择元素后可执行动作。</div>}
          </>}

          {exploreMode === 'vision' && <>
            <div className="automation-section-head">
              <div>
                <strong>4. 视觉操作结果</strong>
                <span className="muted">bbox 会同步高亮到截图</span>
              </div>
            </div>
            <div className="automation-empty compact">
              使用左侧视觉描述或模板执行定位、点击、输入或视觉断言。
            </div>
          </>}

          {exploreMode === 'device' && <>
            <div className="automation-section-head">
              <div>
                <strong>4. 设备操作结果</strong>
                <span className="muted">状态/回执会保留在探索历史</span>
              </div>
            </div>
            <div className="automation-empty compact">
              设备查询本身不会进入 DSL；场景、指令和成功断言会固化。
            </div>
          </>}

          {lastResult && <pre className="automation-result">{JSON.stringify(lastResult, null, 2)}</pre>}
        </div>
      </div>
    </section>

    <section className="automation-bottom">
      <div className="panel automation-history">
        <div className="automation-section-head">
          <div>
            <strong>5. 操作历史</strong>
            <span className="muted">可重排、编辑参数或移除；观察/查询步骤不会进入正式 DSL</span>
          </div>
          <button className="btn ghost" onClick={() => void refreshHistory()}>刷新</button>
        </div>

        <div className="automation-history-list">
          {(history?.steps ?? []).map((step) =>
            <div key={step.index} className="automation-history-item">
              <div className={step.ok ? 'automation-history-row' : 'automation-history-row failed'}>
                <span>{step.index}</span>
                <code>{step.tool}</code>
                <span>{step.ms} ms</span>
                <span className="muted">{step.driver || '—'}</span>
                <span>{step.ok ? '成功' : step.error || '失败'}</span>
                <div className="automation-history-actions">
                  <button className="btn ghost small"
                          disabled={!!busy || step.index <= 1}
                          onClick={() => void moveHistoryStep(step.index, step.index - 1)}>↑</button>
                  <button className="btn ghost small"
                          disabled={!!busy || step.index >= historyCount}
                          onClick={() => void moveHistoryStep(step.index, step.index + 1)}>↓</button>
                  <button className="btn ghost small" disabled={!!busy}
                          onClick={() => editHistoryStep(step)}>编辑</button>
                  <button className="btn ghost small" disabled={!!busy}
                          onClick={() => void removeHistoryStep(step.index)}>移除</button>
                </div>
              </div>

              {editingIndex === step.index && <div className="automation-history-editor">
                <textarea value={editingArgs} onChange={(e) => setEditingArgs(e.target.value)} />
                <div className="hstack">
                  <button className="btn primary" disabled={!!busy}
                          onClick={() => void saveHistoryStep()}>
                    保存参数
                  </button>
                  <button className="btn ghost" disabled={!!busy}
                          onClick={() => {
                            setEditingIndex(null);
                            setEditingArgs('');
                          }}>
                    取消
                  </button>
                </div>
                <small className="muted">
                  这里只修改“固化参数”，不会伪造新的执行结果；需要重新验证时请重新执行该动作。
                </small>
              </div>}
            </div>)}

          {!history?.steps.length && <div className="automation-empty">暂无操作记录。</div>}
        </div>
      </div>

      <div className="panel automation-draft">
        <div className="automation-section-head">
          <div>
            <strong>6. 固化为 DSL</strong>
            <span className="muted">临时 element_index 会转换成稳定 locator 候选</span>
          </div>
        </div>
        <div className="form-2col">
          <label>用例 ID
            <input value={caseId} onChange={(e) => setCaseId(e.target.value)} />
          </label>
          <label>标题
            <input value={caseTitle} onChange={(e) => setCaseTitle(e.target.value)} />
          </label>
        </div>
        <div className="hstack">
          <button className="btn" disabled={!!busy || historyCount === 0}
                  onClick={() => void generateDraft()}>
            生成草稿
          </button>
          <button className="btn primary"
                  disabled={!draft || draft.case.steps.length === 0 || !!busy}
                  onClick={() => void importDraft()}>
            保存并进入审核
          </button>
        </div>
        {draft && <textarea className="automation-draft-text" readOnly value={draft.text} />}
      </div>
    </section>
  </div>;
}
