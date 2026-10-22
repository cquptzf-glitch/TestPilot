import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import App from './App';
import './styles.css';

// 仅前端调试（npm run dev）时安装壳仿真：页面用样例数据完整渲染；
// 生产构建被 import.meta.env.DEV 折叠，devMock 不会进产物。
if (!('__TAURI_INTERNALS__' in window) && import.meta.env.DEV) {
  await import('./devMock');
}

const el = document.getElementById('root');
if (!el) throw new Error('缺少 #root 挂载点');

createRoot(el).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
