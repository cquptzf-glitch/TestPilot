import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Tauri 壳把前端产物放在 ../dist（见 src-tauri/tauri.conf.json 的 frontendDist）
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    port: 5173,
    strictPort: true,
    // src-tauri 由 cargo 监听，避免 Vite 反复重启
    watch: { ignored: ['**/src-tauri/**'] },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'chrome110',
    sourcemap: false,
  },
});
