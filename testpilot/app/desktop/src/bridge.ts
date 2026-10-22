/** 壳 ↔ 引擎桥（唯一对外通道，设计 2.5）。
 *
 *  分两层：
 *  1. **RPC 方法表**（`RPC_METHODS`）—— 必须与 `testpilot/rpc.py` 的 `methods()` 完全一致，
 *     由 `tests/test_desktop_contract.py` 跨语言守门（改一边不改另一边直接测试失败）；
 *  2. **壳命令**（engine_status / engine_log / sync_roots / read_*）—— 由 Rust 壳提供，
 *     前端不直连文件系统与进程。
 */
import { invoke } from '@tauri-apps/api/core';
import { listen, type UnlistenFn } from '@tauri-apps/api/event';

import type { EngineHealth, EngineStatus, ShellMetrics } from './types';

/** 与 testpilot/rpc.py 的 RpcService.methods() 对齐（新增方法必须同时改两处）。 */
export const RPC_METHODS = [
  'job.start',
  'job.status',
  'job.cancel',
  'health',
  'shutdown',
  'case.pull',
  'case.sync',
  'case.list',
  'case.detail',
  'case.export',
  'case.import',
  'case.parse',
  'suite.list',
  'suite.detail',
  'suite.create',
  'suite.update',
  'suite.delete',
  'task.create',
  'task.run',
  'task.abort',
  'task.list',
  'task.detail',
  'task.syncPending',
  'task.reconcile',
  'run.list',
  'run.detail',
  'run.caseDetail',
  'run.stepTrace',
  'run.report',
  'run.compare',
  'run.shotDiff',
  'kb.pending',
  'kb.confirm',
  'kb.reject',
  'kb.setDisposition',
  'kb.caseCtxList',
  'kb.caseCtxSave',
  'kb.caseCtxDelete',
  'kb.envCompat',
  'dsl.review',
  'dsl.save',
  'dsl.accept',
  'dsl.reject',
  'automation.tools',
  'automation.invoke',
  'automation.history',
  'automation.draft',
  'automation.removeStep',
  'automation.updateStep',
  'automation.rerunStep',
  'automation.moveStep',
  'automation.restore',
  'automation.reset',
  'config.describe',
  'config.apply',
  'config.reset',
  'config.overrideText',
] as const;

export type RpcMethod = (typeof RPC_METHODS)[number];

/** 引擎领域事件名（与 testpilot/core/events.py 的事件名一致）。 */
export const RPC_EVENTS = ['run.progress', 'case.finished', 'step.finished'] as const;
export type RpcEvent = (typeof RPC_EVENTS)[number];

/** 壳侧事件（Rust shell 自产，非引擎领域事件）。 */
export const SHELL_EVENTS = { engineStatus: 'engine://engine.status' } as const;

/** 是否在 Tauri 运行时内 —— **调用时**动态判定，不在模块加载时缓存：
 *  dev 模式（npm run dev）的壳仿真（devMock）在 main.tsx 里晚于本模块安装，
 *  缓存值会让仿真永远判定失败。 */
function inTauriRuntime(): boolean {
  return typeof window !== 'undefined'
    && ('__TAURI_INTERNALS__' in window || '__TAURI__' in window);
}

function assertTauri(): void {
  if (!inTauriRuntime()) {
    throw new Error('当前不在 Tauri 壳内运行（npm run dev 只能看静态骨架；'
      + '请用 npm run shell:dev 或打包后的应用启动引擎）');
  }
}

/** 发起一次引擎 RPC。方法名受 `RpcMethod` 约束，避免拼错静默 404。 */
export async function call<T = unknown>(
  method: RpcMethod,
  params: Record<string, unknown> = {},
): Promise<T> {
  assertTauri();
  return invoke<T>('rpc_call', { method, params });
}

/** 订阅引擎领域事件；返回取消订阅函数。 */
export function onEvent<T>(event: RpcEvent, handler: (payload: T) => void): Promise<UnlistenFn> {
  assertTauri();
  return listen<T>(`engine://${event}`, (e) => handler(e.payload));
}

/** 订阅壳的引擎状态变化（sidecar 启停/重启计数）。 */
export function onEngineStatus(handler: (s: EngineStatus) => void): Promise<UnlistenFn> {
  assertTauri();
  return listen<EngineStatus>(SHELL_EVENTS.engineStatus, (e) => handler(e.payload));
}

export function engineStatus(): Promise<EngineStatus> {
  return invoke<EngineStatus>('engine_status');
}

export function engineRestart(): Promise<{ ok: boolean; killedPid?: number; detail?: string }> {
  return invoke('engine_restart');
}

export function shellMetrics(): Promise<ShellMetrics> {
  return invoke<ShellMetrics>('shell_metrics');
}

export function engineLog(lines = 200): Promise<string[]> {
  return invoke<string[]>('engine_log', { lines });
}

/** 注册资源白名单根目录（取到 health.paths 后调用一次）。 */
export function syncRoots(paths: string[]): Promise<{ roots: string[] }> {
  return invoke('sync_roots', { paths });
}

/** 读取白名单内的文本（Markdown 报告）。 */
export function readTextFile(path: string): Promise<string> {
  return invoke<string>('read_text_file', { path });
}

/** 读取白名单内的图片，返回 data URL（可直接给 <img src>）。 */
export function readAssetDataUrl(path: string): Promise<string> {
  return invoke<string>('read_asset_data_url', { path });
}

/** 启动时把引擎暴露的资源根目录登记进壳白名单；失败不阻断（只是读不了资源）。 */
export async function bootstrapRoots(health: EngineHealth): Promise<string[]> {
  const paths = health.paths ?? {};
  const roots = [paths.screenshot_root, paths.report_root, paths.db]
    .filter((p): p is string => typeof p === 'string' && p.length > 0);
  if (roots.length === 0) return [];
  try {
    const out = await syncRoots(roots);
    return out.roots ?? [];
  } catch {
    return [];
  }
}

/** 截图/报告的绝对路径拼接（引擎只存相对 screenshot_root 的路径）。 */
export function assetPath(root: string | undefined, rel: string): string {
  if (!root) return rel;
  const sep = root.includes('\\') ? '\\' : '/';
  return `${root.replace(/[\\/]+$/, '')}${sep}${rel.replace(/^[\\/]+/, '')}`;
}

/** 截图相对路径（与 storage/shotstore.py 的三级目录规则一致：run/case/step_phase.png）。
 *
 *  `step_trace.screenshot_path` 只存"主截图"（after 优先），但对比回放需要 before/after
 *  两张 —— 磁盘命名规则是唯一权威且稳定，因此按 (run, case_exec, step, phase) 反推。
 *  不存在的那张由 `readAssetDataUrl` 报错兜住，不会显示破图。
 */
export function shotRel(
  runId: number, caseExecId: number, stepNo: number, phase: 'before' | 'after' | 'error',
): string {
  return `${runId}/${caseExecId}/${stepNo}_${phase}.png`;
}

/** Persisted engine-side progress remains queryable after page navigation. */
export interface CaseJob {
  id: string; method: string;
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';
  progress: { phase?: string; case_id?: string | number; total?: number; completed?: number; result?: ParseBatchResult };
  error?: string; result?: unknown;
}

export interface ParseBatchResult {
  parsed: number; total: number;
  items: { case_id: string; step_count: number; uncertain_steps?: string[] }[];
  failed: { case_id: string; error: string }[];
}

let jobActive = false;
const announced = new Set<string>();
export function caseJobActive(): boolean { return jobActive; }
export function notifyCaseJob(job: CaseJob): void {
  const active = job.status === 'queued' || job.status === 'running';
  if (jobActive !== active) {
    jobActive = active;
    window.dispatchEvent(new Event('testpilot:case-job-state'));
  }
  if (!active && !announced.has(job.id)) {
    announced.add(job.id);
    if (announced.size > 100) announced.delete(announced.values().next().value!);
    window.dispatchEvent(new Event('testpilot:cases-changed'));
  }
}

export async function caseJob<T = unknown>(
  method: 'case.pull' | 'case.sync' | 'case.parse',
  params: Record<string, unknown>,
  changed?: (job: CaseJob) => void,
): Promise<T> {
  let job = await call<CaseJob>('job.start', { method, params });
  notifyCaseJob(job);
  changed?.(job);
  while (job.status === 'queued' || job.status === 'running') {
    await new Promise((resolve) => setTimeout(resolve, 500));
    job = await call<CaseJob>('job.status', { jobId: job.id });
    notifyCaseJob(job);
    changed?.(job);
  }
  if (job.status !== 'completed') throw new Error(job.error || '用例后台任务未完成');
  return job.result as T;
}
