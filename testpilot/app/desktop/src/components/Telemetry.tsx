/** 导航右端常驻的引擎状态徽章组 + 底部日志抽屉（设计 14.2 / 14.5）。
 *
 *  这是**唯一全局告警入口**：sidecar 启停、RPC 断连、模拟器/禅道状态、
 *  DPI 告警、sync_pending 都收敛在这里，不散落到各页面。
 *
 *  交互约定：
 *  - 重启引擎是打断性动作，先经确认对话框（曾发生过"点错重启打断执行"）；
 *  - 日志抽屉被命令面板（Ctrl+K 搜"日志"）与 Esc 双向控制；
 *  - 日志刷新后**贴底滚动**（人看的是最新行），用户手动上翻时不打扰。
 */

import { useEffect, useRef, useState } from 'react';

import { engineLog, engineRestart, shellMetrics } from '../bridge';
import { msg, type EngineContextValue } from '../hooks';
import { fmtMetricMs, pageLoadMs } from '../metrics';
import { Modal } from './Ui';
import type { EngineHealth, EngineStatus, ShellMetrics } from '../types';

function Dot({ tone }: { tone: 'ok' | 'warn' | 'bad' | 'off' }) {
  return <span className={`dot ${tone}`} />;
}

function statusTone(status: EngineStatus | null): 'ok' | 'warn' | 'bad' | 'off' {
  if (!status) return 'off';
  if (status.state === 'running' && status.rpcConnected) return 'ok';
  if (status.state === 'stopped') return 'bad';
  return 'warn';
}

export function Telemetry({ engine }: { engine: EngineContextValue }) {
  const [open, setOpen] = useState(false);
  const [confirmRestart, setConfirmRestart] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const status = engine.status;

  // Esc 关闭日志抽屉；命令面板通过 tp:toggle-log 事件开关（避免状态提升）
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open]);

  useEffect(() => {
    const onToggle = () => setOpen((v) => !v);
    window.addEventListener('tp:toggle-log', onToggle);
    return () => window.removeEventListener('tp:toggle-log', onToggle);
  }, []);

  const restart = async () => {
    setConfirmRestart(false);
    setBusy(true);
    setNote(null);
    try {
      const out = await engineRestart();
      // 监督循环不可用/没有可杀的 sidecar 时壳会回 ok:false：如实提示，
      // 否则用户以为重启成功、引擎却永远不会回来
      setNote(out.ok ? null : `重启未执行：${out.detail ?? 'sidecar 未在运行'}`);
      engine.refresh();
    } catch (e) {
      setNote(`重启失败：${msg(e)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="telemetry">
      <span className="hstack" title={status ? `状态 ${status.state}；pid ${status.pid ?? '-'}；重启 ${status.restarts} 次` : '引擎状态未知'}>
        <Dot tone={statusTone(status)} />
        <span className="muted">{engineLabel(status)}</span>
      </span>
      <span className="hstack" title={engine.health?.emulator.connected ? '模拟器已连接' : '模拟器未连接'}>
        <Dot tone={engine.health?.emulator.connected ? 'ok' : 'off'} />
        <span className="muted">模拟器</span>
      </span>
      <span className="hstack" title={zentaoTitle(engine.health)}>
        <Dot tone={zentaoTone(engine.health)} />
        <span className="muted">禅道</span>
      </span>
      {(engine.health?.warnings?.length ?? 0) > 0 && (
        <span className="hstack" title={engine.health?.warnings?.join('\n')}>
          <Dot tone="warn" /><span className="muted">环境告警 {engine.health?.warnings?.length}</span>
        </span>
      )}
      {engine.healthError && <Dot tone="bad" />}
      {(engine.health?.sync_pending ?? 0) > 0 && (
        <span className="hstack" title="有任务的结果还没回写禅道（补偿入口：任务页「补偿」按钮）">
          <Dot tone="warn" /><span className="muted">待回写 {engine.health?.sync_pending}</span>
        </span>
      )}
      {note && <span className="muted" title={note}>⚠</span>}
      <button className="link" onClick={() => setOpen((v) => !v)}>{open ? '收起日志' : '引擎日志'}</button>
      <button className="link" disabled={busy} onClick={() => setConfirmRestart(true)}>重启引擎</button>

      {open && <LogDrawer onClose={() => setOpen(false)} />}

      <Modal
        open={confirmRestart}
        title="重启引擎"
        onClose={() => setConfirmRestart(false)}
        footer={(
          <>
            <button className="btn" onClick={() => setConfirmRestart(false)}>取消</button>
            <button className="btn danger" onClick={() => void restart()}>确认重启</button>
          </>
        )}
      >
        <div className="msg warn" style={{ margin: 0 }}>
          正在执行的任务会被中断；历史数据不受影响。确定现在重启吗？
        </div>
      </Modal>
    </div>
  );
}

function engineLabel(status: EngineStatus | null): string {
  if (!status) return '引擎未知';
  if (status.state === 'running' && status.rpcConnected) return '引擎就绪';
  if (status.state === 'running') return '引擎等待 RPC';
  if (status.state === 'restarting') return `重启中(${status.restarts})`;
  if (status.state === 'starting') return '启动中';
  return '引擎已停止';
}

function zentaoTone(health: EngineHealth | null): 'ok' | 'warn' | 'off' {
  if (!health) return 'off';
  if (!health.zentao.configured) return 'off';
  const pending = health as EngineHealth & { sync_pending?: number };
  return pending.sync_pending ? 'warn' : 'ok';
}

function zentaoTitle(health: EngineHealth | null): string {
  if (!health) return '健康信息未取到';
  return health.zentao.configured ? '禅道已配置' : '禅道未配置（仅本地任务）';
}

function LogDrawer({ onClose }: { onClose: () => void }) {
  const [lines, setLines] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [metrics, setMetrics] = useState<ShellMetrics | null>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const loading = useRef(false);

  const load = () => {
    // 在途去重：父组件重渲染/StrictMode 双调不该把同一请求连发多次
    if (loading.current) return;
    loading.current = true;
    Promise.all([
      engineLog(300).then(setLines),
      shellMetrics().then(setMetrics).catch(() => setMetrics(null)),
    ])
      .catch((e: unknown) => setError(msg(e)))
      .finally(() => { loading.current = false; });
  };

  // 首次打开时拉一次（副作用进 useEffect，渲染期发请求会无限重渲染）
  useEffect(() => { load(); }, []);

  // 新日志到达后贴底（用户上翻查看历史时不打扰）
  const atBottom = () => {
    const el = bodyRef.current;
    return !el || el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  };
  useEffect(() => {
    const el = bodyRef.current;
    if (el && atBottom()) el.scrollTop = el.scrollHeight;
  }, [lines]);

  return (
    <div className="drawer">
      <header>
        <span>引擎日志（最近 300 行）</span>
        <span className="spacer" />
        <button className="btn" onClick={load}>刷新</button>
        <button className="btn" onClick={() => {
          void navigator.clipboard?.writeText(lines.join('\n'))
            .catch(() => { /* 剪贴板不可用：静默 */ });
        }}>复制全部</button>
        <button className="btn" onClick={onClose}>关闭 (Esc)</button>
      </header>
      {metrics && (
        <div className="faint" style={{ padding: '4px 10px' }}>
          壳指标：重启 {metrics.restarts} 次 · RPC {metrics.rpcCalls} 次
          （失败 {metrics.rpcErrors}）· 最近 {metrics.rpcCalls ? fmtMetricMs(metrics.rpcLastMs) : '—'}
          / 均值 {metrics.rpcCalls ? fmtMetricMs(metrics.rpcAvgMs) : '—'} ·
          页面加载 {fmtMetricMs(pageLoadMs)} · 壳日志 {metrics.shellLog}
        </div>
      )}
      <div className="body" ref={bodyRef}>
        {error && <div className="err">{error}</div>}
        {lines.map((l, i) => (
          <div className={l.includes('[err]') ? 'err' : l.includes('[warn]') ? 'warn' : undefined}
               key={`${i}-${l.slice(0, 24)}`}>{l}</div>
        ))}
        {lines.length === 0 && !error && <div>（暂无日志）</div>}
      </div>
    </div>
  );
}
