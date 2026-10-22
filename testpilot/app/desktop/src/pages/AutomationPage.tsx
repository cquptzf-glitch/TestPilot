import { useEffect, useMemo, useState } from 'react';
import type { MouseEvent as ReactMouseEvent } from 'react';

import { call } from '../bridge';
import { Msg } from '../components/Bits';
import { msg, navigate } from '../hooks';
import type {
  AutomationDraft, AutomationHistory, AutomationHistoryStep, AutomationObservation,
  AutomationSessionSummary, AutomationWindow,
} from '../types';

type InvokeReply<T> = { tool: string; result: T; history_count: number };
type ExploreMode = 'uia' | 'vision' | 'device';

const DEFAULT_sessionId = 'desktop-default';
const AUTOMATION_SESSION_KEY_PREFIX = 'tp-automation-session-v2:';
const LEGACY_AUTOMATION_SESSION_KEY = 'tp-automation-session-v1';

const KEY_PRESETS = [
  { label: 'Enter', keys: '{ENTER}' },
  { label: 'Tab', keys: '{TAB}' },
  { label: 'Esc', keys: '{ESC}' },
  { label: 'Ctrl+A', keys: '^a' },
  { label: 'Ctrl+C', keys: '^c' },
  { label: 'Ctrl+V', keys: '^v' },
  { label: 'Ctrl+S', keys: '^s' },
  { label: 'F5', keys: '{F5}' },
] as const;

function savedSessionKey(sessionId: string): string {
  return AUTOMATION_SESSION_KEY_PREFIX + sessionId;
}

function readSavedSession(sessionId: string): AutomationHistory | null {
  try {
    let raw = localStorage.getItem(savedSessionKey(sessionId));
    if (!raw && sessionId === DEFAULT_sessionId)
      raw = localStorage.getItem(LEGACY_AUTOMATION_SESSION_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as AutomationHistory;
    return value && Array.isArray(value.steps) ? value : null;
  } catch {
    return null;
  }
}

function saveSession(sessionId: string, session: AutomationHistory): void {
  try { localStorage.setItem(savedSessionKey(sessionId), JSON.stringify(session)); }
  catch { /* localStorage quota/private mode: engine session still works */ }
}

function removeSavedSession(sessionId: string): void {
  try { localStorage.removeItem(savedSessionKey(sessionId)); } catch { /* ignore */ }
}

function exactTitle(title: string): string {
  return '^' + title.replace(/[.*+?^$()|[\]\\{}]/g, '\\$&') + '$';
}

export function AutomationPage() {
  const [sessionId, setSessionId] = useState(DEFAULT_sessionId);
  const [sessions, setSessions] = useState<AutomationSessionSummary[]>([]);
  const [newSessionId, setNewSessionId] = useState('');
  const [windows, setWindows] = useState<AutomationWindow[]>([]);
  const [selectedWindow, setSelectedWindow] = useState('');
  const [exe, setExe] = useState('');
  const [titlePattern, setTitlePattern] = useState('');
  const [observation, setObservation] = useState<AutomationObservation | null>(null);
  const [queryResult, setQueryResult] = useState<AutomationObservation | null>(null);
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null);
  const [query, setQuery] = useState('');
  const [controlType, setControlType] = useState('');
  const [actionableOnly, setActionableOnly] = useState(false);
  const [treeActionFilter, setTreeActionFilter] = useState('');
  const [treeControlFilter, setTreeControlFilter] = useState('');
  const [collapsedElements, setCollapsedElements] = useState<Set<number>>(new Set());
  const [actionValue, setActionValue] = useState('');
  const [verifyDiff, setVerifyDiff] = useState(false);
  const [diffThreshold, setDiffThreshold] = useState('0.005');
  const [history, setHistory] = useState<AutomationHistory | null>(null);
  const [historyCount, setHistoryCount] = useState(0);
  const [draft, setDraft] = useState<AutomationDraft | null>(null);
  const [caseId, setCaseId] = useState('TC-EXPLORE');
  const [caseTitle, setCaseTitle] = useState('自动化探索路径');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [lastResult, setLastResult] = useState<Record<string, unknown> | null>(null);
  const [toolNames, setToolNames] = useState<Set<string>>(new Set());

  const [exploreMode, setExploreMode] = useState<ExploreMode>('uia');
  const [visualDesc, setVisualDesc] = useState('');
  const [visualTemplate, setVisualTemplate] = useState('');
  const [visualKey, setVisualKey] = useState('');
  const [visualRect, setVisualRect] = useState<number[] | null>(null);
  const [visualRegion, setVisualRegion] = useState<number[] | null>(null);
  const [visualRegionStart, setVisualRegionStart] = useState<[number, number] | null>(null);
  const [visionExpect, setVisionExpect] = useState('');
  const [visionResult, setVisionResult] = useState<Record<string, unknown> | null>(null);

  const [sceneId, setSceneId] = useState('normal_sample_a');
  const [devicePath, setDevicePath] = useState('/state/run');
  const [deviceExpect, setDeviceExpect] = useState('completed');
  const [stateOp, setStateOp] = useState<'equals' | 'contains' | 'regex'>('equals');
  const [deviceCmd, setDeviceCmd] = useState('query_state');
  const [deviceArgs, setDeviceArgs] = useState('{"path":"/state/run"}');
  const [deviceResult, setDeviceResult] = useState<Record<string, unknown> | null>(null);

  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [editingArgs, setEditingArgs] = useState('');

  const current = queryResult ?? observation;
  const elements = current?.matches ?? current?.elements ?? [];
  const elementByIndex = useMemo(
    () => new Map(elements.map((item) => [item.element_index, item])),
    [elements],
  );
  const childCounts = useMemo(() => {
    const counts = new Map<number, number>();
    for (const item of elements) {
      if (item.parent_index != null)
        counts.set(item.parent_index, (counts.get(item.parent_index) ?? 0) + 1);
    }
    return counts;
  }, [elements]);
  const controlTypes = useMemo(
    () => Array.from(new Set(elements.map((item) => item.control_type).filter(Boolean))).sort(),
    [elements],
  );
  const actionTypes = useMemo(
    () => Array.from(new Set(elements.flatMap((item) => item.actions ?? []))).sort(),
    [elements],
  );
  const visibleElements = useMemo(() => elements.filter((item) => {
    if (actionableOnly && !(item.actions ?? []).length) return false;
    if (treeActionFilter && !(item.actions ?? []).includes(treeActionFilter)) return false;
    if (treeControlFilter && item.control_type !== treeControlFilter) return false;
    let parent = item.parent_index;
    while (parent != null) {
      if (collapsedElements.has(parent)) return false;
      parent = elementByIndex.get(parent)?.parent_index ?? null;
    }
    return true;
  }), [actionableOnly, collapsedElements, elementByIndex, elements,
       treeActionFilter, treeControlFilter]);
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
    const safeLeft = Math.max(0, left);
    const safeTop = Math.max(0, top);
    return {
      left: safeLeft + '%',
      top: safeTop + '%',
      width: Math.max(0.4, Math.min(100 - safeLeft, width)) + '%',
      height: Math.max(0.4, Math.min(100 - safeTop, height)) + '%',
    };
  }, [queryResult, selected, observation, exploreMode, visualRect]);

  const visualRegionStyle = useMemo(() => {
    const base = observation?.screenshot_rect ?? observation?.window_rect;
    if (!visualRegion || !base || base.length !== 4 || base[2] <= 0 || base[3] <= 0)
      return null;
    return {
      left: (visualRegion[0] / base[2]) * 100 + '%',
      top: (visualRegion[1] / base[3]) * 100 + '%',
      width: (visualRegion[2] / base[2]) * 100 + '%',
      height: (visualRegion[3] / base[3]) * 100 + '%',
    };
  }, [observation, visualRegion]);


  const invoke = async <T,>(tool: string, args: Record<string, unknown> = {}): Promise<T> => {
    const out = await call<InvokeReply<T>>('automation.invoke', {
      sessionId, tool, args,
    });
    setHistoryCount(out.history_count);
    return out.result;
  };

  const refreshSessions = async () => {
    const out = await call<{ sessions: AutomationSessionSummary[] }>('automation.sessions');
    setSessions(out.sessions);
  };

  const applyHistory = (session: AutomationHistory, targetSessionId = sessionId) => {
    setHistory(session);
    setHistoryCount(session.steps.length);
    setCaseId(session.case_id || 'TC-EXPLORE');
    setCaseTitle(session.title || '自动化探索路径');
    saveSession(targetSessionId, session);
  };

  const refreshHistory = async (targetSessionId = sessionId) => {
    const out = await call<{ session: AutomationHistory }>('automation.history', {
      sessionId: targetSessionId,
    });
    applyHistory(out.session, targetSessionId);
    await refreshSessions();
  };

  const clearLiveAutomationState = () => {
    setObservation(null);
    setQueryResult(null);
    setSelectedIndex(null);
    setVisualRect(null);
    setVisualRegion(null);
    setVisualRegionStart(null);
    setDraft(null);
    setLastResult(null);
    setDeviceResult(null);
    setVisionResult(null);
    setEditingIndex(null);
    setEditingArgs('');
  };

  const switchAutomationSession = async (targetSessionId: string) => {
    const target = targetSessionId.trim();
    if (!/^[A-Za-z0-9._-]{1,64}$/.test(target)) {
      setError('会话 ID 仅允许 1~64 位字母、数字、点、下划线和连字符');
      return;
    }
    if (busy) return;
    setBusy('正在切换探索会话…');
    setError('');
    try {
      let out = await call<{ session: AutomationHistory }>('automation.history', {
        sessionId: target,
      });
      const saved = readSavedSession(target);
      if (out.session.steps.length === 0 && saved?.steps.length) {
        out = await call<{ session: AutomationHistory }>('automation.restore', {
          sessionId: target,
          session: saved,
        });
      }
      const tools = await call<{ tools: { name: string }[] }>('automation.tools', {
        sessionId: target,
      });
      setSessionId(target);
      clearLiveAutomationState();
      applyHistory(out.session, target);
      setToolNames(new Set(tools.tools.map((item) => item.name)));
      await refreshSessions();
    } catch (e) {
      setError(msg(e));
    } finally {
      setBusy('');
    }
  };

  const createAutomationSession = () => {
    const target = newSessionId.trim();
    if (!target) {
      setError('请输入新的会话 ID');
      return;
    }
    setNewSessionId('');
    void switchAutomationSession(target);
  };

  const closeAutomationSession = async () => {
    if (sessionId === DEFAULT_AUTOMATION_SESSION_ID) return;
    if (busy) return;
    setBusy('正在关闭探索会话…');
    setError('');
    try {
      await call('automation.closeSession', { sessionId });
      removeSavedSession(sessionId);
      await switchAutomationSession(DEFAULT_AUTOMATION_SESSION_ID);
    } catch (e) {
      setError(msg(e));
      setBusy('');
    }
  };

  useEffect(() => {
    void switchAutomationSession(DEFAULT_AUTOMATION_SESSION_ID);
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
    setCollapsedElements(new Set());
    const replacement = preserveKey
      ? (out.elements ?? []).find((item) => item.stable_key === preserveKey)
      : undefined;
    setSelectedIndex(replacement?.element_index ?? null);
  };

  const loadWindows = () => run('正在读取桌面窗口…', async () => {
    const out = await invoke<{ windows: AutomationWindow[] }>('app.list', { limit: 200 });
    setWindows(out.windows);
    if (!selectedWindow && out.windows.length) setSelectedWindow(String(out.windows[0].handle));
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

  const doQuery = () => run('正在查询元素…', async () => {
    if (!query.trim() && !controlType.trim()) throw new Error('请输入文本或控件类型');
    const out = await invoke<AutomationObservation>('ui.query', {
      text: query.trim() || undefined,
      control_type: controlType.trim() || undefined,
      limit: 100,
    });
    setQueryResult(out);
    setCollapsedElements(new Set());
    setSelectedIndex(null);
  });

  const elementArgs = (): Record<string, unknown> => {
    if (!selected || !current) throw new Error('请先选择一个 UI 元素');
    return { element_index: selected.element_index, snapshot_id: current.snapshot_id };
  };

  const diffArgs = (): Record<string, unknown> => {
    if (!verifyDiff) return {};
    const threshold = Number(diffThreshold);
    if (!Number.isFinite(threshold) || threshold < 0 || threshold > 1)
      throw new Error('画面变化阈值必须在 0~1 之间');
    return { verify_diff: true, diff_threshold: threshold };
  };

  const action = (tool: string, extra: Record<string, unknown> = {}, refresh = true) =>
    run('正在执行 ' + tool + '…', async () => {
      const keepKey = selected?.stable_key;
      const result = await invoke<Record<string, unknown>>(tool, {
        ...elementArgs(), ...extra, ...diffArgs(),
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
      region: visualRegion ?? undefined,
      timeout_ms: 10000,
    };
  };

  const visualAction = (tool: 'ui.locate' | 'ui.click' | 'ui.input') =>
    run('正在执行视觉自动化…', async () => {
      const args = { ...visualArgs(), ...(tool === 'ui.locate' ? {} : diffArgs()) };
      if (tool === 'ui.input') args.text = actionValue;
      const result = await invoke<Record<string, unknown>>(tool, args);
      const rect = result.rect;
      setVisualRect(Array.isArray(rect) ? rect.map(Number) : null);
      setLastResult(result);
      if (tool !== 'ui.locate') await observeNow();
    });

  const saveVisualTemplate = () => run('正在保存视觉模板…', async () => {
    if (!visualRegion) throw new Error('请先在截图上拖拽框选模板区域');
    const name = visualKey.trim() || visualDesc.trim() || 'visual_template';
    const result = await invoke<Record<string, unknown>>('screen.save_template', {
      name, region: visualRegion,
    });
    const template = String(result.template ?? '');
    if (template) setVisualTemplate(template);
    setLastResult(result);
  });

  const visualAssert = () => run('正在执行视觉断言…', async () => {
    if (!visionExpect.trim()) throw new Error('请填写视觉期望');
    const result = await invoke<Record<string, unknown>>('assert.vision', {
      expect: visionExpect.trim(), timeout_ms: 10000,
    });
    setVisionResult(result);
  });

  const deviceAction = (tool: string, args: Record<string, unknown>) =>
    run('正在执行 ' + tool + '…', async () => {
      const result = await invoke<Record<string, unknown>>(tool, args);
      setDeviceResult(result);
    });

  const callDevice = () => {
    let args: Record<string, unknown>;
    try {
      const parsed = JSON.parse(deviceArgs || '{}') as unknown;
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed))
        throw new Error('设备指令参数必须是 JSON 对象');
      args = parsed as Record<string, unknown>;
    } catch (e) {
      setError('设备指令参数不是合法 JSON 对象：' + msg(e));
      return;
    }
    void deviceAction('device.call', { cmd: deviceCmd.trim(), args });
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
      sessionId: sessionId,
      index: editingIndex,
      args: args as Record<string, unknown>,
    });
    setEditingIndex(null);
    setEditingArgs('');
    setDraft(null);
  });

  const moveHistoryStep = (index: number, toIndex: number) =>
    run('正在调整步骤顺序…', async () => {
      await call('automation.moveStep', {
        sessionId: sessionId, index, toIndex,
      });
      setDraft(null);
    });

  const rerunHistoryStep = (index: number, args?: Record<string, unknown>) =>
    run('正在重新执行探索步骤…', async () => {
      const result = await call<{ result: Record<string, unknown> }>('automation.rerunStep', {
        index, ...(args ? { args } : {}),
      });
      setLastResult(result.result);
      setDraft(null);
    });

  const saveAndRerunHistoryStep = () => {
    if (editingIndex == null) return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(editingArgs || '{}');
    } catch {
      setError('步骤参数必须是合法 JSON');
      return;
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      setError('步骤参数必须是 JSON 对象');
      return;
    }
    void run('正在保存并重新执行探索步骤…', async () => {
      await call('automation.updateStep', {
        index: editingIndex,
        args: parsed as Record<string, unknown>,
      });
      const out = await call<{ result: Record<string, unknown> }>('automation.rerunStep', {
        index: editingIndex,
        args: parsed as Record<string, unknown>,
      });
      setLastResult(out.result);
      setEditingIndex(null);
      setEditingArgs('');
      setDraft(null);
    });
  };

  const removeHistoryStep = (index: number) =>
    run('正在移除探索步骤…', async () => {
      await call('automation.removeStep', {
        sessionId: sessionId, index,
      });
      if (editingIndex === index) {
        setEditingIndex(null);
        setEditingArgs('');
      }
      setDraft(null);
    });

  const generateDraft = () => run('正在生成 DSL 草稿…', async () => {
    const out = await call<AutomationDraft>('automation.draft', {
      sessionId: sessionId,
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
      sessionId: sessionId,
      caseId: caseId.trim() || 'TC-EXPLORE',
      title: caseTitle.trim() || '自动化探索路径',
    });
    setObservation(null);
    setQueryResult(null);
    setSelectedIndex(null);
    setVisualRect(null);
    setVisualRegion(null);
    setVisualRegionStart(null);
    setHistory(null);
    setHistoryCount(0);
    setDraft(null);
    setLastResult(null);
    setDeviceResult(null);
    setVisionResult(null);
    setEditingIndex(null);
    setEditingArgs('');
    removeSavedSession(sessionId);
  });

  const screenshotPoint = (
    event: ReactMouseEvent<HTMLImageElement>,
  ): [number, number] | null => {
    const base = observation?.screenshot_rect ?? observation?.window_rect;
    if (!base || base.length !== 4 || base[2] <= 0 || base[3] <= 0) return null;
    const box = event.currentTarget.getBoundingClientRect();
    if (box.width <= 0 || box.height <= 0) return null;
    const x = Math.max(0, Math.min(base[2],
      ((event.clientX - box.left) / box.width) * base[2]));
    const y = Math.max(0, Math.min(base[3],
      ((event.clientY - box.top) / box.height) * base[3]));
    return [Math.round(x), Math.round(y)];
  };

  const startVisualRegion = (event: ReactMouseEvent<HTMLImageElement>) => {
    if (exploreMode !== 'vision' || queryResult) return;
    const point = screenshotPoint(event);
    if (point) setVisualRegionStart(point);
  };

  const finishVisualRegion = (event: ReactMouseEvent<HTMLImageElement>) => {
    if (exploreMode !== 'vision' || !visualRegionStart || queryResult) return;
    const end = screenshotPoint(event);
    const start = visualRegionStart;
    setVisualRegionStart(null);
    if (!end) return;
    const x = Math.min(start[0], end[0]);
    const y = Math.min(start[1], end[1]);
    const w = Math.abs(start[0] - end[0]);
    const h = Math.abs(start[1] - end[1]);
    if (w < 4 || h < 4) return;
    setVisualRegion([x, y, w, h]);
    setVisualRect(null);
  };

  const selectElementFromScreenshot = (event: ReactMouseEvent<HTMLImageElement>) => {
    if (exploreMode !== 'uia' || !observation?.elements?.length || queryResult) return;
    const base = observation.screenshot_rect ?? observation.window_rect;
    if (!base || base.length !== 4) return;
    const box = event.currentTarget.getBoundingClientRect();
    if (box.width <= 0 || box.height <= 0) return;

    const x = base[0] + ((event.clientX - box.left) / box.width) * base[2];
    const y = base[1] + ((event.clientY - box.top) / box.height) * base[3];
    const hits = observation.elements
      .filter((item) => {
        const rect = item.rect;
        return rect?.length === 4
          && x >= rect[0] && x <= rect[0] + rect[2]
          && y >= rect[1] && y <= rect[1] + rect[3];
      })
      .sort((a, b) => {
        if (a.depth !== b.depth) return b.depth - a.depth;
        const areaA = Math.max(1, a.rect[2] * a.rect[3]);
        const areaB = Math.max(1, b.rect[2] * b.rect[3]);
        return areaA - areaB;
      });
    const hit = hits[0];
    if (!hit) return;
    setExploreMode('uia');
    setQueryResult(null);
    setSelectedIndex(hit.element_index);
    setActionValue(hit.value ?? '');
  };

  const preview = <div className="panel automation-preview">
    <div className="automation-section-head">
      <div><strong>界面预览</strong><span className="muted">{observation?.window_title || '当前窗口'}</span></div>
      {observation?.screenshot_bytes
        ? <span className="chip">{Math.round(observation.screenshot_bytes / 1024)} KB</span>
        : null}
    </div>
    {!queryResult && observation?.png_base64
      ? <div className="automation-canvas">
          <img src={'data:image/png;base64,' + observation.png_base64}
               alt="被测窗口截图"
               title={exploreMode === 'vision'
                 ? '拖拽框选 Vision 搜索区域'
                 : '点击截图可反查并选择 UIA 元素'}
               onClick={selectElementFromScreenshot}
               onMouseDown={startVisualRegion}
               onMouseUp={finishVisualRegion} />
          {exploreMode === 'vision' && visualRegionStyle &&
            <div className="automation-region" style={visualRegionStyle}>
              <span>搜索区域</span>
            </div>}
          {highlight && <div className="automation-highlight" style={highlight}>
            <span>{exploreMode === 'vision'
              ? (visualKey || visualDesc || visualTemplate || '视觉定位')
              : (selected?.name || selected?.automation_id || selected?.control_type || '元素')}</span>
          </div>}
        </div>
      : <div className="automation-preview-empty">
          {queryResult ? '查询模式不展示旧截图' : '暂无截图'}
        </div>}
    <div className="automation-vision-assert">
      <input value={visionExpect} onChange={(e) => setVisionExpect(e.target.value)}
             placeholder="视觉期望，例如：页面显示“检测完成”弹窗" />
      <button className="btn"
              disabled={!!busy || !visionExpect.trim() || !toolNames.has('assert.vision')}
              onClick={() => void visualAssert()}>
        视觉断言并记录
      </button>
    </div>
    {visionResult && <pre className="automation-vision-result">{JSON.stringify(visionResult, null, 2)}</pre>}
  </div>;

  return <div className="main automation-page">
    <div className="page-head">
      <div>
        <h2>自动化探索</h2>
        <p className="muted">观察、操作、断言并把成功路径固化为待审 DSL；临时索引不会进入正式用例。</p>
      </div>
      <span className="spacer" />
      <div className="automation-session-switcher">
        <select value={sessionId} disabled={!!busy}
                aria-label="自动化探索会话"
                onChange={(e) => void switchAutomationSession(e.target.value)}>
          {!sessions.some((item) => item.session_id === sessionId) &&
            <option value={sessionId}>{sessionId}</option>}
          {sessions.map((item) => <option key={item.session_id} value={item.session_id}>
            {item.session_id} · {item.history_count} 步
          </option>)}
        </select>
        <input value={newSessionId} disabled={!!busy}
               onChange={(e) => setNewSessionId(e.target.value)}
               onKeyDown={(e) => { if (e.key === 'Enter') createAutomationSession(); }}
               placeholder="新会话 ID" />
        <button className="btn ghost" disabled={!!busy || !newSessionId.trim()}
                onClick={createAutomationSession}>新建/切换</button>
        <button className="btn ghost"
                disabled={!!busy || sessionId === DEFAULT_AUTOMATION_SESSION_ID}
                onClick={() => void closeAutomationSession()}>关闭会话</button>
      </div>
      <span className="chip">会话 {historyCount} 步</span>
      <button className="btn ghost" disabled={!!busy} onClick={() => void reset()}>清空会话</button>
    </div>

    {busy && <Msg kind="info">{busy}</Msg>}
    {error && <Msg kind="error">{error}</Msg>}

    <section className="panel automation-target">
      <div className="automation-section-head">
        <div><strong>1. 选择被测窗口</strong><span className="muted">支持 HWND 精确附加或直接启动程序</span></div>
        <button className="btn" disabled={!!busy || !toolNames.has('app.list')}
                onClick={() => void loadWindows()}>刷新窗口</button>
      </div>
      <div className="automation-target-grid">
        <label>可见窗口
          <select value={selectedWindow} onChange={(e) => setSelectedWindow(e.target.value)}>
            <option value="">请选择窗口</option>
            {windows.map((item) => <option key={String(item.handle) + '-' + item.title}
                                           value={String(item.handle)}>
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
                  disabled={!!busy || !toolNames.has('app.attach') || (!selectedWindow && !titlePattern.trim())}
                  onClick={() => void attach()}>附加窗口</button>
          <button className="btn" disabled={!!busy || !toolNames.has('app.launch') || !exe.trim()}
                  onClick={() => void launch()}>启动程序</button>
          <button className="btn" disabled={!!busy || !observation}
                  onClick={() => void run('正在观察界面…', () => observeNow())}>重新观察</button>
        </div>
      </div>
    </section>

    <div className="automation-mode-tabs" role="tablist" aria-label="自动化探索模式">
      {([
        ['uia', 'UIA 控件', '结构化元素 · 快速稳定'],
        ['vision', '视觉定位', '自绘/Qt Quick 无障碍兜底'],
        ['device', '设备状态', '场景 · 指令 · 状态断言'],
      ] as const).map(([key, label, hint]) =>
        <button key={key} role="tab" aria-selected={exploreMode === key}
                className={exploreMode === key ? 'selected' : ''}
                onClick={() => setExploreMode(key)}>
          <strong>{label}</strong><small>{hint}</small>
        </button>)}
    </div>

    {exploreMode === 'uia' && <section className="automation-workspace">
      <div className="panel automation-elements">
        <div className="automation-section-head">
          <div>
            <strong>2. UI 元素</strong>
            <span className="muted">
              {current ? 'snapshot #' + current.snapshot_id + ' · ' + elements.length + ' 个元素'
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
          <button className="btn" disabled={!!busy} onClick={() => void doQuery()}>查询</button>
          <label className="automation-filter-check">
            <input type="checkbox" checked={actionableOnly}
                   onChange={(e) => setActionableOnly(e.target.checked)} />
            只看可操作
          </label>
          <select value={treeControlFilter}
                  aria-label="元素树控件类型过滤"
                  onChange={(e) => setTreeControlFilter(e.target.value)}>
            <option value="">全部控件类型</option>
            {controlTypes.map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
          <select value={treeActionFilter}
                  aria-label="元素树动作过滤"
                  onChange={(e) => setTreeActionFilter(e.target.value)}>
            <option value="">全部动作</option>
            {actionTypes.map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
          {queryResult && <button className="btn ghost" onClick={() => {
            setQueryResult(null);
            setSelectedIndex(null);
          }}>返回完整树</button>}
        </div>
        {queryResult && <div className="automation-query-note">
          当前是定向查询结果；旧截图属于前一个 snapshot，因此不与当前元素坐标混用。
        </div>}
        <div className="automation-element-list">
          {visibleElements.map((item) => {
            const childCount = childCounts.get(item.element_index) ?? 0;
            const collapsed = collapsedElements.has(item.element_index);
            return <div key={item.element_index}
              className={selectedIndex === item.element_index
                ? 'automation-element selected' : 'automation-element'}
              style={{ paddingLeft: 6 + Math.min(item.depth, 8) * 12 }}>
              {childCount > 0
                ? <button type="button" className="automation-tree-toggle"
                    aria-label={(collapsed ? '展开' : '折叠') + (item.name || item.control_type)}
                    title={collapsed ? '展开子元素' : '折叠子元素'}
                    onClick={() => setCollapsedElements((current) => {
                      const next = new Set(current);
                      if (next.has(item.element_index)) next.delete(item.element_index);
                      else next.add(item.element_index);
                      return next;
                    })}>
                    {collapsed ? '▸' : '▾'}
                  </button>
                : <span className="automation-tree-toggle placeholder" />}
              <button type="button" className="automation-element-select"
                onClick={() => {
                  setSelectedIndex(item.element_index);
                  setActionValue(item.value ?? '');
                }}>
                <span className="automation-index">{item.element_index}</span>
                <span className="automation-element-main">
                  <strong>{item.name || item.automation_id || '(未命名元素)'}</strong>
                  <small>{(item.control_type || 'Unknown')
                    + (item.automation_id ? ' · #' + item.automation_id : '')
                    + (childCount ? ' · ' + childCount + ' 子项' : '')}</small>
                </span>
                {item.selector_quality && <span
                  className={'selector-quality ' + item.selector_quality}
                  title={item.selector_reason || '定位稳定性'}>
                  {item.selector_score != null ? Math.round(item.selector_score * 100) : '—'}
                </span>}
                {item.value && <span className="automation-value">{item.value}</span>}
              </button>
            </div>;
          })}
          {!visibleElements.length && <div className="automation-empty">
            暂无 UI 元素。附加窗口后会自动观察界面。
          </div>}
        </div>
      </div>

      <div className="automation-right">
        {preview}
        <div className="panel automation-actions">
          <div className="automation-section-head">
            <div><strong>3. 操作选中元素</strong>
              <span className="muted">{selected?.stable_key || '未选择元素'}</span></div>
          </div>
          {selected ? <>
            <div className="automation-meta">
              <code>{JSON.stringify(selected.selector ?? {})}</code>
              <span>{selected.rect?.join(', ')}</span>
            </div>
            {selected.selector_quality && <div className={'automation-selector-note ' + selected.selector_quality}>
              <strong>定位稳定性 {selected.selector_score != null
                ? Math.round(selected.selector_score * 100) + '%' : '—'}</strong>
              <span>{selected.selector_reason || '暂无稳定性说明'}</span>
            </div>}
            <input value={actionValue} onChange={(e) => setActionValue(e.target.value)}
                   placeholder="输入 / 选择 / 按键 / 文本断言值" />
            <div className="automation-region-control">
          <span>{visualRegion
            ? '当前搜索区域：' + visualRegion.join(', ')
            : '可直接在右侧截图上拖拽框选搜索区域'}</span>
          {visualRegion && <button className="btn ghost small"
                                   onClick={() => setVisualRegion(null)}>
            清除区域
          </button>}
        </div>
        <div className="automation-diff-option">
              <label><input type="checkbox" checked={verifyDiff}
                            onChange={(e) => setVerifyDiff(e.target.checked)} />
                验证画面发生变化
              </label>
              <input value={diffThreshold} disabled={!verifyDiff}
                     onChange={(e) => setDiffThreshold(e.target.value)}
                     aria-label="画面变化阈值" />
              <small>变化像素比阈值，默认 0.005（0.5%）</small>
            </div>
            <div className="automation-key-presets" aria-label="常用按键快捷选择">
              <span className="muted">快捷按键：</span>
              {KEY_PRESETS.map((preset) => <button key={preset.label} type="button"
                className="btn ghost small" disabled={!!busy}
                title={"填入 pywinauto 键序列：" + preset.keys}
                onClick={() => setActionValue(preset.keys)}>{preset.label}</button>)}
            </div>
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
    </section>}

    {exploreMode === 'vision' && <section className="automation-workspace automation-vision-workspace">
      <div className="panel automation-visual-tools">
        <div className="automation-section-head">
          <div><strong>2. 视觉定位</strong><span className="muted">适合自绘控件或 Accessibility 不完整的页面</span></div>
        </div>
        <label>语义描述
          <input value={visualDesc} onChange={(e) => setVisualDesc(e.target.value)}
                 placeholder="例如：右下角蓝色“开始检测”按钮" />
        </label>
        <label>模板路径（可选，优先）
          <input value={visualTemplate} onChange={(e) => setVisualTemplate(e.target.value)}
                 placeholder="templates/start_button.png" />
        </label>
        <label>稳定语义键（可选）
          <input value={visualKey} onChange={(e) => setVisualKey(e.target.value)}
                 placeholder="btn.start_detect" />
        </label>
        <label>输入值（视觉输入时使用）
          <input value={actionValue} onChange={(e) => setActionValue(e.target.value)}
                 placeholder="需要写入控件的文本" />
        </label>
        <div className="automation-diff-option">
          <label><input type="checkbox" checked={verifyDiff}
                        onChange={(e) => setVerifyDiff(e.target.checked)} />
            验证动作后画面发生变化
          </label>
          <input value={diffThreshold} disabled={!verifyDiff}
                 onChange={(e) => setDiffThreshold(e.target.value)}
                 aria-label="视觉动作画面变化阈值" />
          <small>通过后会直接固化为 screenshot_diff verify</small>
        </div>
        <div className="automation-region-control">
          <span>{visualRegion
            ? '当前模板/搜索区域：' + visualRegion.join(', ')
            : '在右侧截图拖拽框选区域，可直接保存为模板'}</span>
          <button className="btn ghost small"
                  disabled={!!busy || !visualRegion || !toolNames.has('screen.save_template')}
                  onClick={() => void saveVisualTemplate()}>
            保存选区为模板
          </button>
        </div>
        <div className="hstack">
          <button className="btn" disabled={!!busy}
                  onClick={() => void visualAction('ui.locate')}>定位并高亮</button>
          <button className="btn primary" disabled={!!busy}
                  onClick={() => void visualAction('ui.click')}>视觉点击</button>
          <button className="btn" disabled={!!busy || !actionValue}
                  onClick={() => void visualAction('ui.input')}>视觉输入</button>
        </div>
        <p className="hint">模板路径存在时优先按模板定位；否则使用视觉语义描述。成功动作会把视觉 locator 固化进 DSL。</p>
        {lastResult && <pre className="automation-result">{JSON.stringify(lastResult, null, 2)}</pre>}
      </div>
      <div className="automation-right">
        {preview}
      </div>
    </section>}

    {exploreMode === 'device' && <section className="panel automation-device">
      <div className="automation-section-head">
        <div><strong>2. 设备与状态</strong><span className="muted">模拟器场景、指令、状态查询与可固化断言</span></div>
        {!toolNames.has('device.state') && <span className="badge blocked">模拟器不可用</span>}
      </div>
      <div className="automation-device-grid">
        <div className="automation-device-card">
          <strong>场景</strong>
          <input value={sceneId} onChange={(e) => setSceneId(e.target.value)}
                 placeholder="normal_sample_a" />
          <button className="btn"
                  disabled={!!busy || !toolNames.has('device.scene') || !sceneId.trim()}
                  onClick={() => void deviceAction('device.scene', { scene_id: sceneId.trim() })}>
            加载场景
          </button>
        </div>
        <div className="automation-device-card">
          <strong>状态</strong>
          <input value={devicePath} onChange={(e) => setDevicePath(e.target.value)}
                 placeholder="/state/run" />
          <input value={deviceExpect} onChange={(e) => setDeviceExpect(e.target.value)}
                 placeholder="期望值，如 completed" />
          <select value={stateOp} onChange={(e) =>
            setStateOp(e.target.value as 'equals' | 'contains' | 'regex')}>
            <option value="equals">等于</option>
            <option value="contains">包含</option>
            <option value="regex">正则</option>
          </select>
          <div className="hstack">
            <button className="btn"
                    disabled={!!busy || !toolNames.has('device.state') || !devicePath.trim()}
                    onClick={() => void deviceAction('device.state', { path: devicePath.trim() })}>
              查询状态
            </button>
            <button className="btn primary"
                    disabled={!!busy || !toolNames.has('assert.state')
                      || !devicePath.trim() || !deviceExpect}
                    onClick={() => void deviceAction('assert.state', {
                      path: devicePath.trim(), expect: deviceExpect, op: stateOp,
                    })}>
              断言并记录
            </button>
          </div>
        </div>
        <div className="automation-device-card command">
          <strong>设备指令</strong>
          <input value={deviceCmd} onChange={(e) => setDeviceCmd(e.target.value)}
                 placeholder="指令名" />
          <textarea value={deviceArgs} onChange={(e) => setDeviceArgs(e.target.value)}
                    placeholder='{"key":"value"}' />
          <button className="btn"
                  disabled={!!busy || !toolNames.has('device.call') || !deviceCmd.trim()}
                  onClick={callDevice}>执行指令</button>
        </div>
        <pre className="automation-device-result">
          {deviceResult ? JSON.stringify(deviceResult, null, 2)
            : '执行设备操作后在这里查看结果。'}
        </pre>
      </div>
    </section>}

    <section className="automation-bottom">
      <div className="panel automation-history">
        <div className="automation-section-head">
          <div><strong>4. 操作历史</strong><span className="muted">可调整顺序/参数；观测步骤不会进入正式 DSL</span></div>
          <button className="btn ghost" onClick={() => void refreshHistory()}>刷新</button>
        </div>
        <div className="automation-history-list">
          {(history?.steps ?? []).map((step, index, all) =>
            <div key={step.index}
                 className={step.ok ? 'automation-history-row' : 'automation-history-row failed'}>
              <span>{step.index}</span>
              <code>{step.tool}</code>
              <span>{step.ms} ms</span>
              <span className="muted">{step.driver || '—'}</span>
              <span>{step.ok ? '成功' : step.error || '失败'}</span>
              <span className="automation-history-actions">
                <button className="btn ghost small" disabled={!!busy || index === 0}
                        onClick={() => void moveHistoryStep(step.index, step.index - 1)}>↑</button>
                <button className="btn ghost small" disabled={!!busy || index === all.length - 1}
                        onClick={() => void moveHistoryStep(step.index, step.index + 1)}>↓</button>
                <button className="btn ghost small" disabled={!!busy}
                        onClick={() => void rerunHistoryStep(step.index)}>重跑</button>
                <button className="btn ghost small" disabled={!!busy}
                        onClick={() => editHistoryStep(step)}>编辑</button>
                <button className="btn ghost small" disabled={!!busy}
                        onClick={() => void removeHistoryStep(step.index)}>移除</button>
              </span>
            </div>)}
          {!history?.steps.length && <div className="automation-empty">暂无操作记录。</div>}
        </div>
        {editingIndex != null && <div className="automation-history-editor">
          <div className="automation-section-head">
            <div><strong>编辑步骤 #{editingIndex}</strong>
              <span className="muted">只修改固化参数，不伪造新的执行结果</span></div>
          </div>
          <textarea value={editingArgs} onChange={(e) => setEditingArgs(e.target.value)} />
          <div className="hstack">
            <button className="btn primary" disabled={!!busy}
                    onClick={() => void saveHistoryStep()}>仅保存参数</button>
            <button className="btn" disabled={!!busy}
                    onClick={saveAndRerunHistoryStep}>保存并重跑</button>
            <button className="btn" onClick={() => {
              setEditingIndex(null);
              setEditingArgs('');
            }}>取消</button>
          </div>
        </div>}
      </div>

      <div className="panel automation-draft">
        <div className="automation-section-head">
          <div><strong>5. 固化为 DSL</strong><span className="muted">临时 element_index 自动转换成稳定 locator 候选</span></div>
        </div>
        <div className="form-2col">
          <label>用例 ID<input value={caseId} onChange={(e) => setCaseId(e.target.value)} /></label>
          <label>标题<input value={caseTitle} onChange={(e) => setCaseTitle(e.target.value)} /></label>
        </div>
        <div className="hstack">
          <button className="btn" disabled={!!busy || historyCount === 0}
                  onClick={() => void generateDraft()}>生成草稿</button>
          <button className="btn primary"
                  disabled={!draft || draft.case.steps.length === 0 || !!busy}
                  onClick={() => void importDraft()}>保存并进入审核</button>
        </div>
        {draft && <textarea className="automation-draft-text" readOnly value={draft.text} />}
      </div>
    </section>
  </div>;
}
