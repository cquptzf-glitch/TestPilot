/** 通用 Hook：hash 路由、异步加载、引擎状态与事件订阅。 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  bootstrapRoots, call, caseJobActive, engineStatus as fetchEngineStatus, onEngineStatus,
  onEvent, type RpcEvent,
} from './bridge';
import type { EngineHealth, EngineStatus } from './types';

/* ---------------------------------------------------------------- 路由 */

export interface Route {
  page: string;
  params: URLSearchParams;
}

function parse(): Route {
  const raw = window.location.hash.replace(/^#\/?/, '');
  const [path, query = ''] = raw.split('?');
  return { page: path || 'home', params: new URLSearchParams(query) };
}

export function navigate(page: string, params: Record<string, string | number | undefined> = {}): void {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && `${v}` !== '') q.set(k, `${v}`);
  }
  const query = q.toString();
  window.location.hash = `#/${page}${query ? `?${query}` : ''}`;
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(parse);
  useEffect(() => {
    const onChange = () => setRoute(parse());
    window.addEventListener('hashchange', onChange);
    return () => window.removeEventListener('hashchange', onChange);
  }, []);
  return route;
}

/* ---------------------------------------------------------------- 异步加载 */

const ENGINE_READY = 'testpilot:engine-ready';

export interface AsyncState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
}

/** 统一的"加载 -> 渲染 / 报错 -> 重试"三态，避免每页重复写 loading 分支。 */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const ready = () => setTick((t) => t + 1);
    window.addEventListener(ENGINE_READY, ready);
    window.addEventListener('testpilot:cases-changed', ready);
    return () => {
      window.removeEventListener(ENGINE_READY, ready);
      window.removeEventListener('testpilot:cases-changed', ready);
    };
  }, []);

  // 只在最后一个异步结果上落状态（快速切页时避免旧结果覆盖新结果）
  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    fn()
      .then((d) => { if (alive) setData(d); })
      .catch((e: unknown) => { if (alive) setError(msg(e)); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  return { data, loading, error, reload: useCallback(() => setTick((t) => t + 1), []) };
}

export function useCaseJobBusy(): boolean {
  const [busy, setBusy] = useState(caseJobActive);
  useEffect(() => {
    const update = () => setBusy(caseJobActive());
    window.addEventListener('testpilot:case-job-state', update);
    return () => window.removeEventListener('testpilot:case-job-state', update);
  }, []);
  return busy;
}

export function msg(e: unknown): string {
  if (e instanceof Error) return e.message;
  if (typeof e === 'string') return e;
  return JSON.stringify(e);
}

/* ---------------------------------------------------------------- 引擎状态 */

export interface EngineContextValue {
  status: EngineStatus | null;
  health: EngineHealth | null;
  healthError: string | null;
  roots: string[];
  refresh: () => void;
}

/** 引擎状态 + 健康检查：导航右端徽章与全局告警都读它。 */
export function useEngine(): EngineContextValue {
  const [status, setStatus] = useState<EngineStatus | null>(null);
  const [health, setHealth] = useState<EngineHealth | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [roots, setRoots] = useState<string[]>([]);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let off: (() => void) | undefined;
    // 事件 payload 缺 rpcConnected 字段时保留旧值：壳的状态迁移事件只带
    // state/pid/restarts，整体替换会让徽章最长 5s 误显示"引擎等待 RPC"
    onEngineStatus((next) => {
      setStatus((prev) => ({
        ...(prev ?? next),
        ...next,
        rpcConnected: next.rpcConnected ?? prev?.rpcConnected ?? false,
      }));
    }).then((fn) => { off = fn; }).catch(() => { /* 非 Tauri 环境 */ });
    const pull = () => fetchEngineStatus().then(setStatus).catch(() => setStatus(null));
    pull();                        // 立即取一次：状态徽章不该等第一个 5s 轮询才亮起来
    const timer = window.setInterval(pull, 5000);
    return () => { off?.(); window.clearInterval(timer); };
  }, []);

  useEffect(() => {
    if (status?.rpcConnected) window.dispatchEvent(new Event(ENGINE_READY));
  }, [status?.rpcConnected]);

  // 周期自愈：应用先开、引擎后起（或中途重启）时，health 红点不能一直挂着等人手点
  useEffect(() => {
    const timer = window.setInterval(() => setTick((t) => t + 1), 15000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    let cancelled = false;
    call<EngineHealth>('health')
      .then(async (h) => {
        if (cancelled) return;
        setHealth(h);
        setHealthError(null);
        const registered = await bootstrapRoots(h);
        if (!cancelled && registered.length > 0) setRoots(registered);
      })
      .catch((e: unknown) => { if (!cancelled) setHealthError(msg(e)); });
    return () => { cancelled = true; };
  }, [tick, status?.rpcConnected]);

  return useMemo(
    () => ({ status, health, healthError, roots, refresh: () => setTick((t) => t + 1) }),
    [status, health, healthError, roots],
  );
}

/* ---------------------------------------------------------------- 事件订阅 */

/** 订阅一批引擎事件；回调变化时自动重订阅。 */
export function useEngineEvents(
  events: RpcEvent[],
  handler: (event: RpcEvent, payload: unknown) => void,
): void {
  const ref = useRef(handler);
  ref.current = handler;
  const key = events.join(',');
  useEffect(() => {
    const offs: (() => void)[] = [];
    let cancelled = false;
    for (const ev of key.split(',') as RpcEvent[]) {
      onEvent(ev, (payload) => ref.current(ev, payload))
        .then((fn) => { if (cancelled) fn(); else offs.push(fn); })
        .catch(() => { /* 非 Tauri 环境忽略 */ });
    }
    return () => { cancelled = true; offs.forEach((f) => f()); };
  }, [key]);
}
