/** 基础展示组件：状态徽章 / 驱动徽章 / 通过率条 / 计数卡 / 空态（设计 14.4）。 */

import type { ReactNode } from 'react';

import { copyText, fmtMs, statusClass, statusLabel } from '../format';

export function Badge({ status, text }: { status?: string | null; text?: string }) {
  return (
    <span className={`badge ${statusClass(status)}`} title={status ?? ''}>
      {text ?? statusLabel(status)}
    </span>
  );
}

/** 趋势折线（纯 SVG，零依赖）：最近 N 次 run 的通过率走势，点色=状态。
 *
 *  为什么折线而不是继续堆计数卡：计数卡只回答"现在多少"，回答不了
 *  "在变好还是变差"——回归质量本身就是时间序列。
 */
export function TrendChart({ points, height = 72 }: {
  points: { id: number; rate: number; status: string; title: string }[];
  height?: number;
}) {
  if (points.length < 2) {
    return <div className="empty" style={{ padding: '18px 12px' }}>至少需要两次 run 才能画趋势</div>;
  }
  const W = 100;                                    // viewBox 归一化宽度
  const H = 100;
  const pad = 6;
  const xs = (i: number) => pad + (i * (W - pad * 2)) / (points.length - 1);
  const ys = (rate: number) => H - pad - rate * (H - pad * 2);
  const path = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${xs(i)},${ys(p.rate)}`).join(' ');
  return (
    <svg className="trend" width="100%" height={height} viewBox={`0 0 ${W} ${H}`}
         preserveAspectRatio="none" role="img">
      <line className="gridline" x1={pad} y1={ys(1)} x2={W - pad} y2={ys(1)} />
      <line className="gridline" x1={pad} y1={ys(0.5)} x2={W - pad} y2={ys(0.5)} />
      <line className="gridline" x1={pad} y1={ys(0)} x2={W - pad} y2={ys(0)} />
      <path d={path} fill="none" stroke="var(--c-accent)" strokeWidth="1.2"
            vectorEffect="non-scaling-stroke" />
      {points.map((p, i) => (
        <circle key={p.id} cx={xs(i)} cy={ys(p.rate)} r="2.4"
                vectorEffect="non-scaling-stroke"
                fill={p.status === 'aborted' ? 'var(--s-warn)'
                  : p.status === 'running' ? 'var(--s-run)'
                    : p.rate >= 0.9 ? 'var(--s-pass)'
                      : p.rate >= 0.6 ? 'var(--s-warn)' : 'var(--s-fail)'}
                stroke="var(--c-panel)" strokeWidth="0.8">
          <title>{p.title}</title>
        </circle>
      ))}
    </svg>
  );
}

/** 热力块（纯 SVG/CSS）：分类×次数的色块矩阵——比条形列表更适合"一眼看出
 *  哪类失败占大头"。块面积按 sqrt(次数) 缩放，色深随占比加深。 */
export function HeatBlocks({ items, maxBlocks = 12, emptyText = '暂无数据' }: {
  items: { key: string; value: number; title?: string }[];
  maxBlocks?: number;
  emptyText?: string;
}) {
  const rows = items.filter((x) => x.value > 0).sort((a, b) => b.value - a.value)
    .slice(0, maxBlocks);
  if (rows.length === 0) return <div className="empty" style={{ padding: '18px 12px' }}>{emptyText}</div>;
  const sum = rows.reduce((acc, r) => acc + r.value, 0);
  const max = rows[0].value;
  return (
    <div className="heatblocks">
      {rows.map((r) => {
        const share = r.value / sum;
        const depth = 0.25 + 0.75 * (r.value / max);   // 色深系数
        return (
          <div key={r.key} className="heat" title={r.title ?? `${r.key}: ${r.value} 次`}
               style={{
                 flex: `${Math.max(1, Math.sqrt(r.value))} 1 0`,
                 background: `color-mix(in srgb, var(--s-fail) ${Math.round(depth * 100)}%, var(--c-panel))`,
               }}>
            <span className="heat-key truncate">{r.key}</span>
            <span className="heat-n mono">{r.value}</span>
            <span className="heat-pct mono">{Math.round(share * 100)}%</span>
          </div>
        );
      })}
    </div>
  );
}

export function DriverBadge({ driver, degradedFrom }: { driver?: string | null; degradedFrom?: string | null }) {
  if (!driver) return <span className="faint">—</span>;
  const label = degradedFrom && degradedFrom !== driver ? `${degradedFrom}→${short(driver)}` : driver;
  return <span className={`drv ${driver}`}>{label}</span>;
}

function short(driver: string): string {
  return driver === 'vision' ? 'vis' : driver === 'emulator' ? 'emu' : driver;
}

export function PassBar({ pass, fail, blocked, skip, total }: {
  pass: number; fail: number; blocked: number; skip: number; total: number;
}) {
  const sum = Math.max(total, 1);
  const seg = (n: number, cls: string) => (
    n > 0 ? <div className={`seg ${cls}`} style={{ width: `${(n / sum) * 100}%` }} /> : null
  );
  return (
    <div className="passbar">
      <div className="track">
        {seg(pass, 'pass')}{seg(fail, 'fail')}{seg(blocked, 'blocked')}{seg(skip, 'skip')}
      </div>
      <span className="txt">{pass}/{total}</span>
    </div>
  );
}

export function CountCard({ value, label, tone, sub, title }: {
  value: ReactNode;
  label: string;
  tone?: 'pass' | 'fail' | 'blocked' | 'skip' | 'accent';
  sub?: ReactNode;
  title?: string;
}) {
  return (
    <div className={`card ${tone ? `tone-${tone}` : ''}`} title={title}>
      <div className="n">{value}</div>
      <div className="l">{label}</div>
      {sub !== undefined && <div className="sub">{sub}</div>}
    </div>
  );
}

/** 环形通过率图（纯 SVG，零依赖）：比进度条更直观地把“通过率”作为主指标呈现。 */
export function Donut({ rate, pass, total, size = 96, caption = '通过率' }: {
  rate: number;
  pass: number;
  total: number;
  size?: number;
  caption?: string;
}) {
  const r = (size - 12) / 2;
  const circumference = 2 * Math.PI * r;
  const clamped = Math.max(0, Math.min(1, rate));
  const tone = total === 0 ? 'var(--s-skip)'
    : clamped >= 0.9 ? 'var(--s-pass)'
      : clamped >= 0.6 ? '#e0a33a' : 'var(--s-fail)';
  return (
    <svg className="donut" width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
      <circle className="track" cx={size / 2} cy={size / 2} r={r} strokeWidth={8} />
      <circle className="value" cx={size / 2} cy={size / 2} r={r} stroke={tone} strokeWidth={8}
              transform={`rotate(-90 ${size / 2} ${size / 2})`}
              strokeDasharray={`${circumference * clamped} ${circumference}`} />
      <text className="pct" x="50%" y="47%" textAnchor="middle"
            fontSize={size / 4.4}>{total === 0 ? '—' : `${Math.round(clamped * 100)}%`}</text>
      <text className="cap" x="50%" y="66%" textAnchor="middle">
        {caption}
      </text>
      <text className="cap" x="50%" y="80%" textAnchor="middle">{pass}/{total}</text>
    </svg>
  );
}

/** 横向条形分布图（根因分布、方案耗时等“分类×次数”场景）。 */
export function BarList({ items, total, tone = 'var(--c-accent)', maxBars = 8,
                          valueSuffix = '', emptyText = '暂无数据' }: {
  items: { key: string; value: number; title?: string }[];
  total?: number;
  tone?: string;
  maxBars?: number;
  valueSuffix?: string;
  emptyText?: string;
}) {
  if (items.length === 0) return <Empty text={emptyText} />;
  const rows = [...items].sort((a, b) => b.value - a.value).slice(0, maxBars);
  const sum = total ?? rows.reduce((acc, r) => acc + r.value, 0);
  const max = Math.max(...rows.map((r) => r.value), 1);
  return (
    <div className="stack" style={{ gap: 'var(--sp-2)' }}>
      {rows.map((r) => (
        <div key={r.key} title={r.title ?? `${r.key}: ${r.value}`}
             style={{ display: 'grid', gridTemplateColumns: 'minmax(72px, 26%) 1fr auto',
                      gap: 'var(--sp-3)', alignItems: 'center' }}>
          <span className="mono truncate" style={{ fontSize: 'var(--fs-sm)' }}>{r.key}</span>
          <span className="chart" style={{ display: 'block' }}>
            <svg width="100%" height="10" preserveAspectRatio="none" viewBox="0 0 100 10">
              <rect className="track" x="0" y="0" width="100" height="10" rx="3" />
              <rect className="bar" x="0" y="0" rx="3" height="10" fill={tone}
                    width={Math.max(2, (r.value / max) * 100)} />
            </svg>
          </span>
          <span className="mono muted" style={{ fontSize: 'var(--fs-sm)' }}>
            {r.value}{valueSuffix}{sum > 0 ? `  ${Math.round((r.value / sum) * 100)}%` : ''}
          </span>
        </div>
      ))}
    </div>
  );
}

/** 耗时拆解条（定位 vs 动作）：让“慢在哪一步”一眼可见。 */
export function SplitBar({ segments, width = 132 }: {
  segments: { label: string; value: number; color: string }[];
  width?: number;
}) {
  const total = segments.reduce((a, s) => a + Math.max(0, s.value), 0);
  if (total <= 0) return <span className="faint mono">—</span>;
  return (
    <span className="passbar" style={{ gap: 'var(--sp-2)' }}>
      <span className="track" style={{ width }}>
        {segments.map((s) => (
          s.value > 0
            ? <span key={s.label} className="seg" title={`${s.label} ${s.value}ms`}
                    style={{ width: `${(s.value / total) * 100}%`, background: s.color }} />
            : null
        ))}
      </span>
      <span className="txt">{fmtMs(total)}</span>
    </span>
  );
}

/** 空态：文字 + 可选引导动作（"去拉取用例"这类下一步，别让用户自己找入口）。 */
export function Empty({ text = '暂无数据', action }: { text?: string; action?: ReactNode }) {
  return (
    <div className="empty">
      <span className="empty-icon" aria-hidden>⌀</span>
      <span className="empty-text">{text}</span>
      {action && <span className="empty-action">{action}</span>}
    </div>
  );
}

export function Msg({ kind, children }: { kind: 'error' | 'warn' | 'info'; children: ReactNode }) {
  return <div className={`msg ${kind}`}>{children}</div>;
}

/** 所有 ID（run/case/step/签名）一键复制（设计 14.5）。 */
export function CopyId({ text, mono = true }: { text: string; mono?: boolean }) {
  return (
    <span className="hstack">
      <span className={mono ? 'mono' : undefined}>{text}</span>
      <button className="copy" title="复制" onClick={(e) => { e.stopPropagation(); copyText(text); }}>⧉</button>
    </span>
  );
}

export function Duration({ ms }: { ms?: number | null }) {
  return <span className="mono muted">{fmtMs(ms)}</span>;
}

export function Chips({ items, empty = '—' }: { items?: string[]; empty?: string }) {
  if (!items || items.length === 0) return <span className="faint">{empty}</span>;
  return <div className="chips">{items.map((it) => <span className="chip" key={it}>{it}</span>)}</div>;
}
