/** 通用交互组件：Toast、对话框、内联确认、骨架、折叠、按钮、灯箱（设计 14.4）。
 *
 *  为什么要统一：各页面原先直接用 `window.prompt` / `window.confirm` / 自建 notice 状态，
 *  结果是 ① 无法校验输入（空名字、非法 ID 也照提交）② 风格与壳不一致
 *  ③ 每页重复写一份"错误条 + 忙碌标志"。这里收敛为一套受控组件。
 */
import { useEffect, useRef, useState, type ReactNode } from 'react';

/* ---------------------------------------------------------------- Toast */

export type ToastKind = 'info' | 'pass' | 'warn' | 'error';

export interface ToastItem {
  id: number;
  kind: ToastKind;
  text: string;
}

let toastSeq = 0;
const toastListeners = new Set<(items: ToastItem[]) => void>();
let toastItems: ToastItem[] = [];

/** 推送一条 Toast（组件外也能调用，便于在 bridge/事件回调里提示）。 */
export function pushToast(kind: ToastKind, text: string, ttlMs = 5000): number {
  const id = ++toastSeq;
  toastItems = [...toastItems, { id, kind, text }];
  toastListeners.forEach((fn) => fn(toastItems));
  if (ttlMs > 0) window.setTimeout(() => dismissToast(id), ttlMs);
  return id;
}

export function dismissToast(id: number): void {
  toastItems = toastItems.filter((t) => t.id !== id);
  toastListeners.forEach((fn) => fn(toastItems));
}

/** Toast 容器（挂在 App 根部一次即可）。 */
export function Toaster() {
  const [items, setItems] = useState<ToastItem[]>(toastItems);
  useEffect(() => {
    toastListeners.add(setItems);
    return () => { toastListeners.delete(setItems); };
  }, []);
  if (items.length === 0) return null;
  return (
    <div className="toasts">
      {items.map((t) => (
        <div key={t.id} className={`toast ${t.kind}`}>
          <span>{t.text}</span>
          <button title="关闭" onClick={() => dismissToast(t.id)}>✕</button>
        </div>
      ))}
    </div>
  );
}

/* ---------------------------------------------------------------- 对话框 */

/** 受控对话框：`open` 为 false 时不渲染（避免空壳留在 DOM 里吸走点击）。 */
export function Modal({ open, title, children, footer, onClose, width }: {
  open: boolean;
  title: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  onClose: () => void;
  width?: number;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal" style={width ? { width } : undefined}
           onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true">
        <header>{title}</header>
        <div className="body">{children}</div>
        {footer && <footer>{footer}</footer>}
      </div>
    </div>
  );
}

/** 危险操作的内联确认条（设计 14.4：不用居中大弹窗）。 */
export function ConfirmBar({ text, confirmText = '确认删除', busy, onConfirm, onCancel }: {
  text: ReactNode;
  confirmText?: string;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="confirm-bar">
      <span>{text}</span>
      <span className="spacer" />
      <button className="btn sm danger" disabled={busy} onClick={onConfirm}>{confirmText}</button>
      <button className="btn sm" disabled={busy} onClick={onCancel}>取消</button>
    </div>
  );
}

/* ---------------------------------------------------------------- 交互小件 */

/** 受控输入对话框：替代 `window.prompt`（能校验、能阻止空提交、样式一致）。 */
export function PromptModal({ open, title, label, defaultValue = '', placeholder, validate,
                             hint, confirmText = '确定', busy, onCancel, onSubmit }: {
  open: boolean;
  title: ReactNode;
  label: string;
  defaultValue?: string;
  placeholder?: string;
  hint?: ReactNode;
  confirmText?: string;
  busy?: boolean;
  validate?: (value: string) => string | null;
  onCancel: () => void;
  onSubmit: (value: string) => void;
}) {
  const [value, setValue] = useState(defaultValue);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    setValue(defaultValue);
    setError(null);
    // 打开即聚焦并全选，省一次点击（批量建任务这类型操作会反复弹）
    const timer = window.setTimeout(() => {
      inputRef.current?.focus();
      inputRef.current?.select();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [open, defaultValue]);

  const submit = () => {
    const trimmed = value.trim();
    const bad = validate ? validate(trimmed) : (trimmed ? null : `${label}不能为空`);
    if (bad) { setError(bad); return; }
    onSubmit(trimmed);
  };

  return (
    <Modal
      open={open}
      title={title}
      onClose={onCancel}
      footer={
        <>
          <button className="btn" disabled={busy} onClick={onCancel}>取消</button>
          <button className="btn primary" disabled={busy} onClick={submit}>{confirmText}</button>
        </>
      }
    >
      <div className={`field ${error ? 'bad' : ''}`}>
        <label htmlFor="prompt-input">{label}</label>
        <input
          id="prompt-input"
          ref={inputRef}
          type="text"
          value={value}
          placeholder={placeholder}
          onChange={(e) => { setValue(e.target.value); setError(null); }}
          onKeyDown={(e) => { if (e.key === 'Enter') submit(); }}
        />
        {error
          ? <span className="hint" style={{ color: 'var(--s-fail)' }}>{error}</span>
          : hint ? <span className="hint">{hint}</span> : null}
      </div>
    </Modal>
  );
}

/* ---------------------------------------------------------------- 加载 / 空态 */

/** 骨架屏（比"加载中…"更少布局跳动）。 */
export function Skeleton({ rows = 4, height = 13 }: { rows?: number; height?: number }) {
  return (
    <div className="skeleton-rows">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="skeleton" style={{ height }} />
      ))}
    </div>
  );
}

/* ---------------------------------------------------------------- 折叠分组 */

export function Collapsible({ title, description, count, defaultOpen = false, children }: {
  title: ReactNode;
  description?: ReactNode;
  count?: ReactNode;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="panel">
      <div className={`group-head ${open ? 'open' : ''}`} onClick={() => setOpen((v) => !v)}
           role="button" tabIndex={0}
           onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') setOpen((v) => !v); }}>
        <span className="caret">▶</span>
        <h3>{title}</h3>
        {description && <span className="desc">{description}</span>}
        {count !== undefined && <span className="count">{count}</span>}
      </div>
      {open && <div style={{ marginTop: 'var(--sp-4)' }}>{children}</div>}
    </div>
  );
}

/* ---------------------------------------------------------------- 分段控件 */

export function Segmented<T extends string>({ value, options, onChange }: {
  value: T;
  options: { value: T; label: ReactNode; title?: string }[];
  onChange: (value: T) => void;
}) {
  return (
    <div className="segmented">
      {options.map((o) => (
        <button key={o.value} className={value === o.value ? 'on' : ''} title={o.title}
                onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

/* ---------------------------------------------------------------- 截图灯箱 */

/** 点图放大：查证截图细节时必用（列表里的缩略图看不清文字）。 */
export function Lightbox({ src, caption, onClose }: {
  src: string;
  caption?: ReactNode;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div className="lightbox" onClick={onClose}>
      <figure onClick={(e) => e.stopPropagation()}>
        <figcaption>
          {caption}
          <span className="spacer" />
          <button className="btn sm" onClick={onClose}>关闭 (Esc)</button>
        </figcaption>
        <img src={src} alt="截图" />
      </figure>
    </div>
  );
}
