// 提取前端所有在用的 CSS 类名（含模板字符串与三元表达式里的字面量）。
// 用途：重写 styles.css 前确认清单，避免漏掉某个类导致样式丢失。
// 内联在 PowerShell 里跑会被引号/编码破坏，所以落成文件（见 env-windows-powershell 记忆）。
const fs = require('fs');
const path = require('path');

const ROOT = process.argv[2] || '.';
const files = [];
(function walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(p);
    else if (entry.name.endsWith('.tsx')) files.push(p);
  }
})(ROOT);

const names = new Set();
const attrRe = /className=(?:\{([^}]*)\}|"([^"]*)")/gs;

for (const file of files) {
  const src = fs.readFileSync(file, 'utf8');
  let m;
  while ((m = attrRe.exec(src))) {
    const raw = m[1] || m[2] || '';
    // 取出该表达式里的所有字符串字面量，再拆词
    const lits = raw.match(/'[^']*'|`[^`]*`|"[^"]*"/g) || [raw];
    for (const lit of lits) {
      const text = lit.replace(/[`'"]/g, '');
      for (const token of text.split(/\s+/)) {
        if (!token) continue;
        if (/[${}\\]/.test(token)) continue;        // 变量拼接的跳过
        if (token.includes('?')) continue;
        names.add(token);
      }
    }
  }
}

const sorted = [...names].sort();
console.log(`共 ${sorted.length} 个类名：`);
for (const n of sorted) console.log('  ' + n);
