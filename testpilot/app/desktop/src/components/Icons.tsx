/** 内联图标集（16px 线性风格，stroke=currentColor，零依赖）。
 *
 *  为什么不用图标库：壳体积敏感（vite 产物是安装包的一部分），常用图标
 *  不超过 20 个，内联 SVG 更可控也更贴主题（颜色自动跟随文字色）。
 */
import type { SVGProps } from 'react';

type P = SVGProps<SVGSVGElement> & { size?: number };

function Base({ size = 16, children, ...rest }: P) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none"
         stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"
         strokeLinejoin="round" aria-hidden {...rest}>
      {children}
    </svg>
  );
}

export const IconCases = (p: P) => (
  <Base {...p}><path d="M2 4.5A1.5 1.5 0 0 1 3.5 3h3l1.2 1.5h4.8A1.5 1.5 0 0 1 14 6v5.5a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 11.5z" /></Base>
);

export const IconSuites = (p: P) => (
  <Base {...p}><path d="M3 2.5h7l3 3v8a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-10a1 1 0 0 1 1-1z" /><path d="M10 2.5v3h3" /><path d="M5.5 8.5h5M5.5 11h5" /></Base>
);

export const IconRuns = (p: P) => (
  <Base {...p}><path d="M2.5 8a5.5 5.5 0 1 1 1.6 3.9" /><path d="M2.5 12.5v-3h3" /><path d="M8 5.5V8l2 1.5" /></Base>
);

export const IconKB = (p: P) => (
  <Base {...p}><path d="M8 2.5c-1.5 1-3.2 1.4-5 1.4 0 4.6 1.6 7.6 5 9.6 3.4-2 5-5 5-9.6-1.8 0-3.5-.4-5-1.4z" /><path d="M6 7.5l1.5 1.5L10.5 6" /></Base>
);

export const IconDsl = (p: P) => (
  <Base {...p}><path d="M5 4L2 8l3 4" /><path d="M11 4l3 4-3 4" /><path d="M9 3.5L7 12.5" /></Base>
);

export const IconTasks = (p: P) => (
  <Base {...p}><path d="M2.5 4A1.5 1.5 0 0 1 4 2.5h8A1.5 1.5 0 0 1 13.5 4v8A1.5 1.5 0 0 1 12 13.5H4A1.5 1.5 0 0 1 2.5 12z" /><path d="M5.5 6.5h5M5.5 9.5h3" /></Base>
);

export const IconSettings = (p: P) => (
  <Base {...p}><circle cx="8" cy="8" r="2" /><path d="M8 1.8v1.7M8 12.5v1.7M2.9 4.9l1.5.9M11.6 10.2l1.5.9M2.9 11.1l1.5-.9M11.6 5.8l1.5-.9" /></Base>
);

export const IconSearch = (p: P) => (
  <Base {...p}><circle cx="7" cy="7" r="4.5" /><path d="M10.5 10.5L14 14" /></Base>
);

export const IconSun = (p: P) => (
  <Base {...p}><circle cx="8" cy="8" r="3" /><path d="M8 1.5v1.6M8 12.9v1.6M1.5 8h1.6M12.9 8h1.6M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M3.4 12.6l1.1-1.1M11.5 4.5l1.1-1.1" /></Base>
);

export const IconMoon = (p: P) => (
  <Base {...p}><path d="M13.5 9.5A6 6 0 0 1 6.5 2.5a6 6 0 1 0 7 7z" /></Base>
);

export const IconPlay = (p: P) => (
  <Base {...p}><path d="M5 3.5v9l7-4.5z" /></Base>
);

export const IconStop = (p: P) => (
  <Base {...p}><rect x="4" y="4" width="8" height="8" rx="1" /></Base>
);

export const IconRefresh = (p: P) => (
  <Base {...p}><path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9" /><path d="M13.5 3.5v3h-3" /></Base>
);
