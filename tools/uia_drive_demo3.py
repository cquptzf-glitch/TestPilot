# -*- coding: utf-8 -*-
"""UIA 实机驱动验证（第三批）：表格 / 时间选择 / 列表 / 复选框。

TC-9103（预期全 pass）：
- CheckBox 勾选 -> text 断言回显；
- ListBox 按 name 点击 ListItem；
- DataGridView 单元格（DataItem "列头 行N" 命名）精确点击 -> 断言行列回显；
- DateTimePicker（ui.input value=type_keys 标记）键盘改日期 -> 断言回显。

判定：CLI 全链路执行 + DB 断言 case=pass。
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
EXE = str(ROOT / ".cache" / "demo_target.exe")
CSC = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
TITLE = "TestPilot 被测演示程序"
CASE_ID = "TC-9103"


def sh(args: list[str], home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["TESTPILOT_HOME"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([PY, "-m", "testpilot.app.cli"] + args,
                          cwd=str(ROOT), env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=300)


def build_case() -> dict:
    loc = lambda aid, ct: {"type": "uia", "automation_id": aid, "control_type": ct}
    tgt = lambda key, aid, ct: {"key": key, "primary": loc(aid, ct)}
    nloc = lambda key, name, ct: {"key": key,
                                  "primary": {"type": "uia", "name": name,
                                              "control_type": ct}}
    return {
        "case_id": CASE_ID,
        "zentao_case_id": 9103,
        "title": "UIA 复杂控件：表格/时间选择/列表/复选框",
        "version": 1,
        "steps": [
            {"id": "s1", "action": "app.launch",
             "args": {"exe": EXE, "title_re": TITLE},
             "source_text": "启动被测演示程序", "timeout_ms": 20000},

            # ---- 复选框：点击 + 回显断言 ----
            {"id": "s2", "action": "ui.click",
             "target": tgt("chk.agree", "chkAgree", "CheckBox"),
             "source_text": "勾选同意条款"},
            {"id": "s3", "action": "assert.ui",
             "verify": {"type": "text", "expect": "勾选:是", "op": "contains",
                        "target": loc("lblCheck", "Text"), "timeout_ms": 5000},
             "source_text": "校验勾选回显"},

            # ---- 列表：按项文本点击 ----
            {"id": "s4", "action": "ui.click",
             "target": nloc("lst.banana", "香蕉", "ListItem"),
             "source_text": "在列表中选择「香蕉」"},

            # ---- 表格：DataItem（列头 行N）精确点击 ----
            {"id": "s5", "action": "ui.click",
             "target": nloc("grid.cell.10", "名称 行 1", "DataItem"),
             "source_text": "点击表格第 1 行「名称」列单元格"},
            {"id": "s6", "action": "assert.ui",
             "verify": {"type": "text", "expect": "cell(1,0)", "op": "contains",
                        "target": loc("lblStatus", "Text"), "timeout_ms": 5000},
             "source_text": "校验点中的是 (1,0) 单元格"},

            # ---- 时间选择：click 聚焦 + 键盘标记改日期（+1 天）----
            {"id": "s7", "action": "ui.click",
             "target": tgt("dtp.date", "dtpDate", "Pane"),
             "source_text": "聚焦时间选择器"},
            {"id": "s8", "action": "ui.input",
             "value": "{LEFT}{LEFT}{LEFT}{UP}",
             "target": tgt("dtp.date", "dtpDate", "Pane"),
             "source_text": "键盘将日期 +1 天"},
            {"id": "s9", "action": "assert.ui",
             "verify": {"type": "text", "expect": "日期:2026-09-13", "op": "contains",
                        "target": loc("lblDate", "Text"), "timeout_ms": 5000},
             "source_text": "校验日期变为 09-13"},

            {"id": "s10", "action": "app.close", "args": {"mode": "graceful"},
             "source_text": "关闭被测程序"},
        ],
    }


def main() -> int:
    subprocess.run(["taskkill", "/IM", "demo_target.exe", "/F"], capture_output=True)
    time.sleep(0.5)
    csc = subprocess.run(
        [CSC, "-nologo", "-target:winexe", f"-out:{EXE}",
         str(ROOT / "tools" / "demo_target.cs")], capture_output=True)
    if csc.returncode != 0:
        print("FAIL 编译:", (csc.stdout + csc.stderr).decode("gbk", "replace")[:400])
        return 1

    home = Path(tempfile.mkdtemp(prefix="tp_uia3_"))
    print(f"[setup] {home}")
    case_file = home / "case.json"
    case_file.write_text(json.dumps(build_case(), ensure_ascii=False), encoding="utf-8")

    for args in (["case", "import", "--file", str(case_file)],
                 ["case", "accept", CASE_ID],
                 ["task", "create", "--name", "复杂控件验证", "--cases", CASE_ID]):
        proc = sh(args, home)
        if proc.returncode != 0:
            print(f"FAIL {' '.join(args)}: {proc.stderr[:400]}")
            return 1

    proc = sh(["task", "run", "1", "--no-writeback"], home)
    if proc.returncode != 0:
        print("FAIL task run:", proc.stderr[:300])
        return 1

    conn = sqlite3.connect(str(home / "data" / "testpilot.db"))
    conn.row_factory = sqlite3.Row
    case = conn.execute("SELECT status FROM case_exec WHERE case_id=?",
                        (CASE_ID,)).fetchone()
    steps = conn.execute(
        "SELECT step_no, action, verify_result, error_json FROM step_trace "
        "ORDER BY step_no").fetchall()
    conn.close()

    ok = case["status"] == "pass"
    for s in steps:
        mark = "PASS" if (s["verify_result"] == "pass"
                          or s["action"] in ("app.launch", "app.close",
                                             "ui.click", "ui.input")) else "FAIL?"
        print(f"  s{s['step_no']:<2} {s['action']:<9} {mark} {s['error_json'] or ''}"[:150])
    if not ok:
        print(f"FAIL TC-9103 case={case['status']}")
    subprocess.run(["taskkill", "/IM", "demo_target.exe", "/F"], capture_output=True)
    print("\n结论:", "复杂控件驱动验证全部通过 ✓" if ok else "存在失败 ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
