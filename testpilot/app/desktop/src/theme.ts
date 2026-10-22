/** 主题（浅色/暗色）—— 令牌在 styles.css 里按 `[data-theme]` 切换。
 *
 *  选择持久化在 localStorage（键 `tp.theme`）；未选择时跟随系统
 *  `prefers-color-scheme`。index.html 里有同逻辑的**内联脚本**在首帧前设置
 *  `data-theme`，避免暗色用户看到白屏闪烁 —— 这里只在运行时切换。
 */

export type Theme = 'light' | 'dark';

const KEY = 'tp.theme';

function systemTheme(): Theme {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches
    ? 'dark' : 'light';
}

/** 当前生效主题（以 DOM 属性为准 —— index.html 的内联脚本已在首帧前写入）。 */
export function currentTheme(): Theme {
  return document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light';
}

/** 切换并持久化；返回切换后的主题。 */
export function setTheme(theme: Theme): Theme {
  document.documentElement.dataset.theme = theme;
  try { window.localStorage.setItem(KEY, theme); } catch { /* 隐私模式等 */ }
  return theme;
}

export function toggleTheme(): Theme {
  return setTheme(currentTheme() === 'dark' ? 'light' : 'dark');
}

/** 兜底：内联脚本因任何原因没生效时，React 启动后补一次。 */
export function ensureTheme(): Theme {
  const t = document.documentElement.dataset.theme;
  if (t !== 'light' && t !== 'dark') {
    let saved: string | null = null;
    try { saved = window.localStorage.getItem(KEY); } catch { /* 同上 */ }
    return setTheme(saved === 'dark' || saved === 'light' ? saved : systemTheme());
  }
  return t;
}
