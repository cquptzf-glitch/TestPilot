/** 展示层格式化：状态文案/颜色、通过率、时间、ID 复制（设计 14.4/14.5）。 */

import type { RunStats } from './types';

/** 状态文案（固定枚举，不做自由文本）。 */
const LABELS: Record<string, string> = {
  pass: '通过',
  fail: '失败',
  blocked: '阻塞',
  skip: '跳过',
  skipped: '跳过',
  error: '错误',
  running: '执行中',
  completed: '已完成',
  aborted: '已中止',
  draft: '草稿',
  active: '已生效',
  rejected: '已拒绝',
  deprecated: '已废弃',
  synced: '已同步',
  stale: '待重解析',
  conflict: '有冲突',
  local: '仅本地',
  sync_pending: '待回写',
  pending: '待审',
};

export function statusLabel(status?: string | null): string {
  if (!status) return '—';
  return LABELS[status] ?? status;
}

/** CSS class 与状态同名，便于 `badge ${status}` 直接使用。 */
export function statusClass(status?: string | null): string {
  return status ?? 'unknown';
}

export interface PassRate {
  pass: number;
  fail: number;
  blocked: number;
  skip: number;
  total: number;
  rate: number;
}

export function passRate(stats: RunStats | undefined | null): PassRate {
  const s = stats ?? {};
  const pass = s.pass ?? 0;
  const fail = (s.fail ?? 0) + (s.error ?? 0);
  const blocked = s.blocked ?? 0;
  const skip = s.skip ?? 0;
  const total = s.total ?? pass + fail + blocked + skip;
  return { pass, fail, blocked, skip, total, rate: total > 0 ? pass / total : 0 };
}

/** 本地时区展示；悬浮可看原值（IS0 串）。 */
export function fmtTime(value?: string | null): string {
  if (!value) return '—';
  const d = new Date(value.replace(' ', 'T'));
  if (Number.isNaN(d.getTime())) return value;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export function fmtMs(ms?: number | null): string {
  if (ms === null || ms === undefined) return '—';
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60_000).toFixed(1)} 分`;
}

export function fmtDuration(start?: string | null, end?: string | null): string {
  if (!start || !end) return '—';
  const a = new Date(start.replace(' ', 'T')).getTime();
  const b = new Date(end.replace(' ', 'T')).getTime();
  if (Number.isNaN(a) || Number.isNaN(b)) return '—';
  return fmtMs(Math.max(0, b - a));
}

/** 相对时间（"5 分钟前"）：列表里的时间列用相对值扫一眼就新旧立判；绝对值走 title。 */
export function fmtRelative(value?: string | null): string {
  if (!value) return '—';
  const d = new Date(value.replace(' ', 'T')).getTime();
  if (Number.isNaN(d)) return value;
  const s = Math.round((Date.now() - d) / 1000);
  if (s < 0) return '刚刚';
  if (s < 45) return '刚刚';
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} 分钟前`;
  if (s < 86400) return `${Math.round(s / 3600)} 小时前`;
  return `${Math.round(s / 86400)} 天前`;
}

export function shortSig(sig?: string | null, n = 10): string {
  if (!sig) return '—';
  return sig.length > n ? `${sig.slice(0, n)}…` : sig;
}

export function copyText(text: string): void {
  // 剪贴板权限被拒时不能留 unhandled rejection（深链复制按钮的真实失败路径）
  navigator.clipboard?.writeText(text).catch(() => { /* 剪贴板不可用：静默 */ });
}

export function matchFilters(value: string, ...needles: (string | undefined)[]): boolean {
  const v = value.toLowerCase();
  return needles.every((n) => !n || v.includes(n.toLowerCase()));
}
