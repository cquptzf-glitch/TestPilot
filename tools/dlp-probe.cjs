// 用「非白名单进程」视角探测各扩展名是否会被 DLP 返回密文。
//
// 背景：DLP 按进程可执行文件名放行（python.exe / Code.exe 放行，其它返回密文）。
// Tauri 壳进程名是 testpilot-shell.exe，同样不在白名单 —— 因此**壳自己读前端资源**
// 也可能拿到密文（症状：WebView2 白屏 / JS 解析失败）。
// node.exe 实测与壳同为非白名单进程，用它的读取结果代表壳的读取结果。
//
// 用法：node tools/dlp-probe.cjs
const fs = require('fs');
const path = require('path');

const ROOT = 'D:\\MyCode\\TestPilot';
const MAGIC = Buffer.from([0x62, 0x14, 0x23, 0x65]);

const targets = [];
const push = (label, p) => targets.push([label, path.join(ROOT, p)]);

// 壳要读的东西：前端静态资源（打包后落 resources）
push('desktop/index.html', 'testpilot/app/desktop/dist/index.html');
push('desktop/dist 目录', 'testpilot/app/desktop/dist');
push('tauri.conf.json', 'testpilot/app/desktop/src-tauri/tauri.conf.json');
push('capabilities', 'testpilot/app/desktop/src-tauri/capabilities');

// sidecar 资源
push('sidecar/testpilot-sidecar.exe', 'dist/testpilot-sidecar/testpilot-sidecar.exe');
push('sidecar/_internal/cv2/__init__.py', 'dist/testpilot-sidecar/_internal/cv2/__init__.py');
push('sidecar/_internal 抽查(抽样 200)', 'dist/testpilot-sidecar/_internal');

function walk(dir, limit = 400) {
  const out = [];
  const stack = [dir];
  while (stack.length && out.length < limit) {
    const cur = stack.pop();
    let entries;
    try { entries = fs.readdirSync(cur, { withFileTypes: true }); } catch { continue; }
    for (const e of entries) {
      const full = path.join(cur, e.name);
      if (e.isDirectory()) stack.push(full);
      else out.push(full);
      if (out.length >= limit) break;
    }
  }
  return out;
}

function head(file) {
  let fd;
  try {
    fd = fs.openSync(file, 'r');
    const buf = Buffer.alloc(8);
    const n = fs.readSync(fd, buf, 0, 8, 0);
    return buf.subarray(0, n);
  } catch (e) {
    return null;
  } finally {
    if (fd !== undefined) { try { fs.closeSync(fd); } catch {} }
  }
}

function check(label, p) {
  let st;
  try { st = fs.statSync(p); } catch { console.log(`${label}: 不存在`); return; }
  if (st.isDirectory()) {
    const files = walk(p);
    let enc = 0;
    const ext = {};
    for (const f of files) {
      const h = head(f);
      if (h && h.length >= 4 && h.subarray(0, 4).equals(MAGIC)) {
        enc++;
        const e = path.extname(f) || '(无扩展名)';
        ext[e] = (ext[e] || 0) + 1;
      }
    }
    console.log(`${label}: 抽样 ${files.length} 个文件，密文 ${enc} 个  分布=${JSON.stringify(ext)}`);
    return;
  }
  const h = head(p);
  const isEnc = !!h && h.length >= 4 && h.subarray(0, 4).equals(MAGIC);
  console.log(`${label}: ${isEnc ? '密文' : '明文'}  head=${h ? h.toString('hex') : 'N/A'}`);
}

for (const [label, p] of targets) check(label, p);
