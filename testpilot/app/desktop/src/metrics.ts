/** 前端可观测指标（设计 10 章「壳侧运行指标」的前端部分）。
 *
 *  只保留"能真实测到"的两类：
 *  - **页面加载**：模块被 import 的时刻即首屏 JS 执行到这里的耗时（SPA 近似值）；
 *  - **展示格式化**：毫秒/秒统一样式，避免各组件各写一套。
 *
 *  RPC 时延与重启次数由 Rust 壳统计（`shell_metrics` 命令），前端不重复测量。
 */

/** 首屏加载耗时（ms）：模块加载时采样一次，之后不再变化。 */
export const pageLoadMs: number =
  typeof performance !== 'undefined' ? Math.round(performance.now()) : 0;

/** 毫秒展示：>= 1s 用秒（1 位小数），否则整数毫秒；空值给 "—"。 */
export function fmtMetricMs(ms: number | null | undefined): string {
  if (ms == null || Number.isNaN(ms)) return '—';
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
}
