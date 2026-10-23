/** 命令面板（Ctrl/Cmd+K）：页面跳转、run/用例深链、全局动作的唯一入口。
 *
 *  为什么要做它：原本 Ctrl+K 用 `window.prompt` 输 run 号 —— 不能搜索、
 *  不知道有哪些 run、风格与壳完全脱节。面板把"跳转到某条 run / 某条用例 /
 *  换主题 / 看日志"收敛成一个可搜索的列表，键盘流（↑↓ Enter Esc）完整。
 *
 *  数据策略：run/用例清单在**首次打开时**拉取并缓存 30 秒（面板是低频入口，
 *  不值得常驻轮询；打开瞬间的一次请求换来"输 #12 直达"的体验）。
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';

import { call } from '../bridge';
import {
  IconCases, IconDsl, IconKB, IconMoon, IconRuns, IconSearch, IconSettings,
  IconSuites, IconTasks,
} from './Icons';
import { fmtRelative, statusLabel } from '../format';
import { navigate } from '../hooks';
import type { CaseRow, RunRow } from '../types';

interface Cmd {
  key: string;
  icon: ReactNode;
  label: string;
  sub?: string;
  keywords: string;
  group: string;
  action: () => void;
}

const PAGES: Cmd[] = [
  { key: 'p-home', icon: <IconRuns />, label: '开始测试', keywords: 'home 开始 测试 回归',
    group: '页面', action: () => navigate('home') },
  { key: 'p-overview', icon: <IconRuns />, label: '运行概览', keywords: 'overview 首页 概览 指标',
    group: '页面', action: () => navigate('overview') },
  { key: 'p-automation', icon: <IconDsl />, label: '自动化探索',
    keywords: 'automation explore computer use UIA 自动化 探索 录制',
    group: '页面', action: () => navigate('automation') },
  { key: 'p-cases', icon: <IconCases />, label: '用例管理', keywords: 'cases 拉取 同步 用例',
    group: '页面', action: () => navigate('cases') },
  { key: 'p-suites', icon: <IconSuites />, label: '测试方案', keywords: 'suites 方案 套件',
    group: '页面', action: () => navigate('suites') },
  { key: 'p-runs', icon: <IconRuns />, label: '执行记录', keywords: 'runs 执行 历史 报告',
    group: '页面', action: () => navigate('runs') },
  { key: 'p-kb', icon: <IconKB />, label: '知识库审阅', keywords: 'kb 知识 人审 待审',
    group: '页面', action: () => navigate('kb') },
  { key: 'p-dsl', icon: <IconDsl />, label: 'DSL 审阅', keywords: 'dsl 草稿 解析 审阅',
    group: '页面', action: () => navigate('dsl') },
  { key: 'p-tasks', icon: <IconTasks />, label: '任务', keywords: 'tasks 任务 执行 新建',
    group: '页面', action: () => navigate('tasks') },
  { key: 'p-settings', icon: <IconSettings />, label: '设置', keywords: 'settings 设置 配置',
    group: '页面', action: () => navigate('settings') },
];

/** 打开引擎日志抽屉（Telemetry 监听同一事件；避免把抽屉状态提升到 App）。 */
export function toggleEngineLog(): void {
  window.dispatchEvent(new CustomEvent('tp:toggle-log'));
}

export function CommandPalette({ open, onClose, onToggleTheme }: {
  open: boolean;
  onClose: () => void;
  onToggleTheme: () => void;
}) {
  const [query, setQuery] = useState('');
  const [sel, setSel] = useState(0);
  const [runs, setRuns] = useState<RunRow[] | null>(null);
  const [cases, setCases] = useState<CaseRow[] | null>(null);
  const loadedAt = useRef(0);
  const listRef = useRef<HTMLDivElement>(null);

  // 打开时懒加载 run/用例清单（30s 缓存）
  useEffect(() => {
    if (!open) return;
    setQuery('');
    setSel(0);
    if (Date.now() - loadedAt.current < 30_000) return;
    loadedAt.current = Date.now();
    void call<{ runs: RunRow[] }>('run.list', { limit: 100 })
      .then((d) => setRuns(d.runs)).catch(() => setRuns([]));
    void call<{ cases: CaseRow[] }>('case.list')
      .then((d) => setCases(d.cases)).catch(() => setCases([]));
  }, [open]);

  const cmds = useMemo<Cmd[]>(() => {
    const out: Cmd[] = [...PAGES];
    out.push({
      key: 'a-theme', icon: <IconMoon />, label: '切换 深/浅色 主题',
      keywords: 'theme dark light 暗色 深色 主题', group: '动作',
      action: onToggleTheme,
    });
    out.push({
      key: 'a-log', icon: <IconRuns />, label: '打开/收起 引擎日志',
      keywords: 'log 日志 抽屉 排障', group: '动作', action: toggleEngineLog,
    });
    for (const r of runs ?? []) {
      out.push({
        key: `run-${r.id}`, icon: <IconRuns />,
        label: `run #${r.id}${r.status === 'running' ? '（执行中）' : ''}`,
        sub: fmtRelative(r.started_at),
        keywords: `run ${r.id} ${statusLabel(r.status)}`, group: '执行记录',
        action: () => navigate('run', { runId: r.id }),
      });
    }
    for (const c of (cases ?? []).slice(0, 200)) {
      out.push({
        key: `case-${c.case_id}`, icon: <IconCases />,
        label: c.case_id,
        sub: c.title || undefined,
        keywords: `case ${c.case_id} ${c.zentao_case_id ?? ''} ${c.title} ${c.status}`,
        group: '用例（去 DSL 审阅）',
        action: () => navigate('dsl', { caseId: c.case_id }),
      });
    }
    return out;
  }, [runs, cases, onToggleTheme]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return cmds.slice(0, 24);
    const scored: { c: Cmd; s: number }[] = [];
    for (const c of cmds) {
      const hay = `${c.label} ${c.sub ?? ''} ${c.keywords}`.toLowerCase();
      const idx = hay.indexOf(q);
      if (idx < 0) continue;
      scored.push({ c, s: (c.label.toLowerCase().startsWith(q) ? 0 : idx < 20 ? 1 : 2) });
    }
    return scored.sort((a, b) => a.s - b.s).slice(0, 24).map((x) => x.c);
  }, [cmds, query]);

  // 输入变化后回到第一项；选中项滚进可视区
  useEffect(() => { setSel(0); }, [query]);
  useEffect(() => {
    listRef.current?.querySelector('.item.sel')?.scrollIntoView({ block: 'nearest' });
  }, [sel, filtered.length]);

  if (!open) return null;

  const groups: { name: string; items: Cmd[] }[] = [];
  for (const c of filtered) {
    const last = groups[groups.length - 1];
    if (last && last.name === c.group) last.items.push(c);
    else groups.push({ name: c.group, items: [c] });
  }

  const runAction = (c: Cmd) => { onClose(); c.action(); };

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') { e.preventDefault(); onClose(); }
    else if (e.key === 'ArrowDown') {
      e.preventDefault();
      setSel((i) => Math.min(i + 1, filtered.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setSel((i) => Math.max(i - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const c = filtered[sel];
      if (c) runAction(c);
    }
  };

  let flat = -1;
  return (
    <div className="palette-mask" onClick={onClose}>
      <div className="palette" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal>
        <div className="palette-input">
          <IconSearch size={15} />
          <input
            autoFocus
            value={query}
            placeholder="搜索：页面 / run # / 用例 / 动作…"
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={onKey}
          />
        </div>
        <div className="list" ref={listRef}>
          {filtered.length === 0 && <div className="none">没有匹配项</div>}
          {groups.map((g) => (
            <div key={g.name}>
              <div className="group-label">{g.name}</div>
              {g.items.map((c) => {
                flat += 1;
                const i = flat;
                return (
                  <button key={c.key}
                          className={`item ${i === sel ? 'sel' : ''}`}
                          onMouseEnter={() => setSel(i)}
                          onClick={() => runAction(c)}>
                    {c.icon}
                    <span className="truncate">{c.label}</span>
                    {c.sub && <span className="sub">{c.sub}</span>}
                  </button>
                );
              })}
            </div>
          ))}
        </div>
        <footer>
          <span><span className="kbd">↑</span> <span className="kbd">↓</span> 选择</span>
          <span><span className="kbd">Enter</span> 打开</span>
          <span><span className="kbd">Esc</span> 关闭</span>
        </footer>
      </div>
    </div>
  );
}

/** 快捷键速查（导航里按 ? 打开）。 */
export function ShortcutsHelp({ open, onClose }: { open: boolean; onClose: () => void }) {
  if (!open) return null;
  const rows: [string, string][] = [
    ['Ctrl + K', '命令面板：搜索页面 / run / 用例 / 动作'],
    ['Alt + 1 ~ 6', '按顺序切换左侧导航'],
    ['?', '打开本速查'],
    ['A / R', '知识库审阅：通过 / 驳回（失败待审 Tab）'],
    ['J / K', '知识库待审队列 · RunDetail 轨迹上/下一步'],
    ['Esc', '关闭弹层 / 日志抽屉 / 灯箱'],
  ];
  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal" style={{ width: 440 }} onClick={(e) => e.stopPropagation()}>
        <header>键盘快捷键</header>
        <div className="body">
          <table className="grid">
            <tbody>
              {rows.map(([k, v]) => (
                <tr key={k}>
                  <td style={{ width: 110 }}><span className="kbd">{k}</span></td>
                  <td className="muted">{v}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <footer>
          <button className="btn" onClick={onClose}>关闭</button>
        </footer>
      </div>
    </div>
  );
}
