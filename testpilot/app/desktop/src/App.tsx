/** 壳主视图：顶部导航（48px）+ 全宽内容区 + 常驻引擎状态徽章（设计 14.2）。
 *
 *  全局交互入口都收敛在这里：
 *  - Ctrl/Cmd+K 命令面板（页面/run/用例搜索跳转 + 全局动作）—— 取代旧的
 *    window.prompt 输 run 号（无搜索、风格脱节）；
 *  - Alt+1~6 直达主页面；`?` 打开快捷键速查；
 *  - 主题切换（浅/深）持久化到 localStorage，首帧前由 index.html 内联脚本定值。
 */

import { useEffect, useState } from 'react';

import { CommandPalette, ShortcutsHelp } from './components/Palette';
import { CaseJobsPanel } from './components/CaseJobsPanel';
import { Telemetry } from './components/Telemetry';
import {
  IconCases, IconKB, IconMoon, IconRuns, IconSearch, IconSettings,
  IconSun,
} from './components/Icons';
import { call } from './bridge';
import { WorkflowPage } from './pages/WorkflowPage';
import { AutomationPage } from './pages/AutomationPage';
import { HomePage } from './pages/HomePage';
import { Toaster } from './components/Ui';
import { useEngine, useRoute, navigate, useEngineEvents } from './hooks';
import { ensureTheme, toggleTheme } from './theme';
import { CasesPage } from './pages/CasesPage';
import { ChangeAnalysisPage } from './pages/ChangeAnalysisPage';
import { DslReviewPage } from './pages/DslReviewPage';
import { KBReviewPage } from './pages/KBReviewPage';
import { RunDetailPage } from './pages/RunDetailPage';
import { RunsPage } from './pages/RunsPage';
import { SettingsPage } from './pages/SettingsPage';
import { SuitesPage } from './pages/SuitesPage';
import { TasksPage } from './pages/TasksPage';

const NAV = [
  { key: 'home', label: '开始测试', icon: <IconRuns /> },
  { key: 'automation', label: '自动化探索', icon: <IconCases /> },
  { key: 'cases', label: '用例维护', icon: <IconCases /> },
  { key: 'runs', label: '执行与结果', icon: <IconRuns /> },
  { key: 'kb', label: '问题与知识', icon: <IconKB /> },
  { key: 'settings', label: '设置', icon: <IconSettings /> },
];

export default function App() {
  const route = useRoute();
  const engine = useEngine();
  const page = route.page;
  const section = page === 'overview' ? 'home'
    : ['dsl', 'changes'].includes(page) ? 'cases'
    : ['run', 'tasks', 'suites'].includes(page) ? 'runs' : page;
  const [running, setRunning] = useState<number | null>(null);
  const [stopping, setStopping] = useState(false);
  useEffect(() => {
    call<{ runs: { id: number; status: string }[] }>('run.list', { limit: 30 })
      .then((r) => setRunning(r.runs.find((v) => v.status === 'running')?.id ?? null))
      .catch(() => {});
  }, [engine.status?.rpcConnected]);
  useEngineEvents(['run.progress'], (_event, payload) => {
    const p = payload as { run_id: number; phase: string };
    if (p.phase === 'start') {
      setRunning(p.run_id);
      if (['tasks', 'suites', 'home'].includes(page)) navigate('run', { runId: p.run_id });
    }
    if (p.phase === 'finished') { setRunning(null); setStopping(false); }
  });
  const stop = async () => {
    setStopping(true);
    try {
      const out = await call<{ stopping: boolean }>('task.abort');
      if (!out.stopping) {
        // run 可能已在点击瞬间结束；立即回读，避免按钮永久停在“正在停止…”。
        setStopping(false);
        const current = await call<{ runs: { id: number; status: string }[] }>(
          'run.list', { limit: 30 });
        setRunning(current.runs.find((v) => v.status === 'running')?.id ?? null);
      }
    } catch {
      setStopping(false);
    }
  };

  const [paletteOpen, setPaletteOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [theme, setThemeState] = useState(ensureTheme);

  // 全局键盘：Ctrl+K 面板 / Alt+数字 切页 / ? 速查（输入框里不抢键）
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName;
      const typing = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setPaletteOpen((v) => !v);
        return;
      }
      if (e.altKey && !e.ctrlKey && !e.metaKey && /^[1-6]$/.test(e.key)) {
        const item = NAV[Number(e.key) - 1];
        if (item) { e.preventDefault(); navigate(item.key); }
        return;
      }
      if (!typing && e.key === '?' ) {
        e.preventDefault();
        setHelpOpen((v) => !v);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const switchTheme = () => setThemeState(toggleTheme());

  const runId = Number(route.params.get('runId') ?? 0);

  return (
    <>
      <div className="workspace">
      <aside className="sidebar">
        <a className="workspace-brand" href="#/home"><span className="brand-symbol">T</span><span>TestPilot<small>自动化测试工作台</small></span></a>
        <div className="nav-caption">工作空间</div>
        <nav className="side-links" aria-label="主导航">
          {NAV.map((item, i) => <a key={item.key} href={`#/${item.key}`}
            className={section === item.key ? 'on' : ''} title={`${item.label} · Alt+${i + 1}`}>
            {item.icon}<span>{item.label}</span>{section === item.key && <span className="nav-active-mark" />}
          </a>)}
        </nav>
        <div className="sidebar-note"><span className="status-orb" />从用例到测试结论<small>审核、执行、追踪，一处完成</small></div>
        <button className="sidebar-search" onClick={() => setPaletteOpen(true)}><IconSearch />搜索与快捷操作<kbd>⌃ K</kbd></button>
      </aside>
      <div className="workspace-body">
        <header className="app-topbar">
          <div className="breadcrumb">工作空间 <span>/</span> <strong>{NAV.find((n) => n.key === section)?.label ?? '测试详情'}</strong></div>
          <div className="topbar-actions">
            {import.meta.env.DEV && <span className="preview-label">演示模式 · 模拟数据</span>}
            <button className="icon-button" title="快捷键" onClick={() => setHelpOpen(true)}>?</button>
            <button className="icon-button" title="切换主题" onClick={switchTheme}>{theme === 'dark' ? <IconSun size={16} /> : <IconMoon size={16} />}</button>
            <Telemetry engine={engine} />
          </div>
        </header>
        {running && <div className="execution-bar" role="status"><span className="pulse-dot" /><strong>测试 #{running} 正在执行</strong><span className="spacer" /><button className="btn" onClick={() => navigate('run', { runId: running })}>查看进度</button><button className="btn danger" disabled={stopping} onClick={() => void stop()}>{stopping ? '正在停止…' : '停止执行'}</button></div>}
        {section === 'cases' && <div className="section-tabs">
          <a className={page === 'cases' ? 'on' : ''} href="#/cases">用例库</a>
          <a className={page === 'changes' ? 'on' : ''} href="#/changes">SVN 变更生成</a>
          <a className={page === 'dsl' ? 'on' : ''} href="#/dsl">审核与编辑</a>
        </div>}
        {section === 'runs' && page !== 'run' && <div className="section-tabs">
          <a className={page === 'runs' ? 'on' : ''} href="#/runs">执行记录</a>
          <a className={page === 'tasks' ? 'on' : ''} href="#/tasks">测试任务</a>
          <a className={page === 'suites' ? 'on' : ''} href="#/suites">测试方案</a>
        </div>}
        <CaseJobsPanel compact={page === 'home'} />
        {page === 'home' && <WorkflowPage key={route.params.get('selection') ?? 'default'} selection={route.params.get('selection') ?? undefined} running={running} />}
        {page === 'automation' && <AutomationPage />}
        {page === 'overview' && <HomePage />}

      {page === 'cases' && <CasesPage selection={route.params.get('selection') ?? undefined} />}
      {page === 'changes' && <ChangeAnalysisPage />}
      {page === 'suites' && <SuitesPage />}
      {page === 'runs' && <RunsPage />}
      {page === 'run' && (runId > 0
        ? <RunDetailPage
            runId={runId}
            engine={engine}
            initialCaseExec={route.params.get('caseExecId') ?? undefined}
            initialStepNo={route.params.get('stepNo') ?? undefined}
          />
        : <div className="main"><div className="msg warn">缺少 runId（用 #/run?runId=1 形式打开）</div></div>)}
      {page === 'kb' && (
        <KBReviewPage
          initialTab={route.params.get('tab') ?? undefined}
          initialCase={route.params.get('caseId') ?? undefined}
          initialId={route.params.get('id') ?? undefined}
        />
      )}
      {page === 'dsl' && <DslReviewPage caseId={route.params.get('caseId') ?? undefined} queue={route.params.get('queue') ?? undefined} />}
      {page === 'tasks' && <TasksPage taskId={Number(route.params.get('taskId') ?? 0) || undefined} />}
      {page === 'settings' && <SettingsPage engine={engine} />}

      </div>
      </div>
      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        onToggleTheme={switchTheme}
      />
      <ShortcutsHelp open={helpOpen} onClose={() => setHelpOpen(false)} />
      <Toaster />
    </>
  );
}
