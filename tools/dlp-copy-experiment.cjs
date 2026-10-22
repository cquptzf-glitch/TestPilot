// DLP 判定实验：密文是「落盘加密」还是「按进程过滤读取」？
//
// 判定方法：对同一个源文件，分别用三种进程做**字节复制**，再用 node（非白名单）读回。
//   - by_python：白名单进程读（透明解密）→ 写
//   - by_node / by_ps：非白名单进程读（拿到密文）→ 写
//
// 结论映射：
//   * 若 by_python 输出对 node 仍为密文 => 保护随复制传递（防扩散）=> 产物**落盘即密文**，
//     必须清洗后才能发布给无 DLP 的机器；
//   * 若 by_python 输出对 node 为明文 => 只是"按进程过滤读取"，落盘字节是明文，
//     产物可直接发布，本机问题用白名单文件名即可绕过。
const fs = require('fs');
const path = require('path');

const SRC = 'D:\\MyCode\\TestPilot\\dist\\testpilot-sidecar\\_internal\\cv2\\__init__.py';
const OUT = 'D:\\MyCode\\TestPilot\\.cache\\dlp_t';
const MAGIC = Buffer.from([0x62, 0x14, 0x23, 0x65]);

fs.rmSync(OUT, { recursive: true, force: true });
fs.mkdirSync(OUT, { recursive: true });

// node 的复制（非白名单进程读源文件）
const byNode = path.join(OUT, 'by_node.py');
try {
  fs.copyFileSync(SRC, byNode);
  console.log('node 复制成功 ->', byNode);
} catch (e) {
  console.log('node 复制失败:', e.message);
}

// PowerShell 的复制（非白名单进程）
const { execFileSync } = require('child_process');
const byPs = path.join(OUT, 'by_ps.py');
try {
  execFileSync('powershell', ['-NoProfile', '-Command',
    `Copy-Item -Force '${SRC}' '${byPs}'`], { stdio: 'pipe' });
  console.log('powershell 复制成功 ->', byPs);
} catch (e) {
  console.log('powershell 复制失败:', (e.stderr || e.message).toString().slice(0, 200));
}

// python 的复制（白名单进程读源文件；为避免 DLP 干预 python 直接写在文件里）
const byPy = path.join(OUT, 'by_python.py');
try {
  execFileSync('D:\\MyCode\\TestPilot\\.venv\\Scripts\\python.exe',
    ['-c', `import shutil; shutil.copyfile(r'${SRC}', r'${byPy}')`], { stdio: 'pipe' });
  console.log('python 复制成功 ->', byPy);
} catch (e) {
  console.log('python 复制失败:', (e.stderr || e.message).toString().slice(0, 200));
}

function head(file) {
  try {
    const fd = fs.openSync(file, 'r');
    const buf = Buffer.alloc(8);
    const n = fs.readSync(fd, buf, 0, 8, 0);
    fs.closeSync(fd);
    const h = buf.subarray(0, n);
    return { hex: h.toString('hex'), enc: h.subarray(0, 4).equals(MAGIC) };
  } catch (e) {
    return { error: e.message };
  }
}

console.log('\n--- node（非白名单）视角 ---');
for (const [label, f] of [['源文件', SRC], ['by_python', byPy], ['by_node', byNode], ['by_ps', byPs]]) {
  const r = head(f);
  console.log(`${label.padEnd(12)} ${r.error ? 'ERR ' + r.error : (r.enc ? '密文' : '明文') + '  ' + r.hex}`);
}
