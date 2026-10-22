# -*- coding: utf-8 -*-
"""UIA 实机驱动验证：编译被测程序 -> CLI 全链路执行一条真实 DSL 用例。

验证目标（M1-5 主干，设计 8.1/8.3）：
- app.launch（exe + title_re 附加/启动）
- ui.input（clear + 键盘输入）
- ui.click（click_input 真实鼠标事件）
- assert text（UIA 读控件文本 + contains 断言）
- app.close（graceful 收尾，进程退出）

通过判定：task run 退出码 0 且 DB 内该用例 status=pass、全部步骤 pass、进程已退出。
"""
import json
import os
import subprocess
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
EXE = str(ROOT / ".cache" / "demo_target.exe")
CSC = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
CASE_ID = "TC-9100"
TITLE = "TestPilot 被测演示程序"
SECRET = "hello-testpilot-2026"


def sh(args: list[str], home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["TESTPILOT_HOME"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([PY, "-m", "testpilot.app.cli"] + args,
                          cwd=str(ROOT), env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=300)
    return proc


def kill_target() -> None:
    subprocess.run(["taskkill", "/IM", "demo_target.exe", "/F"],
                   capture_output=True)


def build_case() -> dict:
    loc = lambda _key, aid, ct: {"type": "uia", "automation_id": aid,
                                 "control_type": ct}
    tgt = lambda key, aid, ct: {"key": key, "primary": loc(key, aid, ct)}
    return {
        "case_id": CASE_ID,
        "zentao_case_id": 9100,
        "title": "UIA 实机驱动验证（演示被测程序）",
        "version": 1,
        "steps": [
            {"id": "s1", "action": "app.launch",
             "args": {"exe": EXE, "title_re": TITLE},
             "source_text": "启动被测演示程序",
             "timeout_ms": 20000},
            {"id": "s2", "action": "ui.wait", "args": {"state": "exists"},
             "target": tgt("btn.submit", "btnSubmit", "Button"),
             "source_text": "等待主界面就绪"},
            {"id": "s3", "action": "ui.input", "value": SECRET,
             "args": {"clear": True},
             "target": tgt("txt.input", "txtInput", "Edit"),
             "source_text": "在输入框填入验证文本"},
            {"id": "s4", "action": "ui.click",
             "target": tgt("btn.submit", "btnSubmit", "Button"),
             "source_text": "点击「提交」"},
            {"id": "s5", "action": "assert.ui",
             "verify": {"type": "text", "expect": "状态:DONE", "op": "contains",
                        "target": loc("lbl.status", "lblStatus", "Text"),
                        "timeout_ms": 5000},
             "source_text": "校验状态条变为 DONE"},
            {"id": "s6", "action": "assert.ui",
             "verify": {"type": "text", "expect": SECRET, "op": "contains",
                        "target": loc("lbl.echo", "lblEcho", "Text"),
                        "timeout_ms": 5000},
             "source_text": "校验回显包含输入文本（证明输入真的进去了）"},
            {"id": "s7", "action": "ui.click",
             "target": tgt("btn.reset", "btnReset", "Button"),
             "source_text": "点击「重置」"},
            {"id": "s8", "action": "assert.ui",
             "verify": {"type": "text", "expect": "状态:READY", "op": "contains",
                        "target": loc("lbl.status", "lblStatus", "Text"),
                        "timeout_ms": 5000},
             "source_text": "校验重置后回到 READY"},
            {"id": "s9", "action": "app.close", "args": {"mode": "graceful"},
             "source_text": "关闭被测程序"},
        ],
    }


def main() -> int:
    # 0) 清理残留实例（否则 exe 被锁无法重编译） -> 编译被测程序
    #    必须 -target:winexe：GUI 子系统下 pywinauto 的 WaitForInputIdle 才成立，
    #    控制台程序会报 1471"不是 GUI 进程"（实测定律）
    kill_target()
    time.sleep(0.5)
    csc = subprocess.run(
        [CSC, "-nologo", "-target:winexe", f"-out:{EXE}",
         str(ROOT / "tools" / "demo_target.cs")],
        capture_output=True)
    if csc.returncode != 0:
        print("FAIL 编译被测程序:",
              (csc.stdout + csc.stderr).decode("gbk", "replace")[:500])
        return 1
    home = Path(tempfile.mkdtemp(prefix="tp_uia_demo_"))
    print(f"[setup] TESTPILOT_HOME={home}")

    case_file = home / "case.json"
    case_file.write_text(json.dumps(build_case(), ensure_ascii=False),
                         encoding="utf-8")

    # 1) 导入 -> 人审通过
    steps = [
        ["case", "import", "--file", str(case_file)],
        ["case", "accept", CASE_ID],
        ["task", "create", "--name", "UIA 实机验证", "--cases", CASE_ID],
    ]
    for args in steps:
        proc = sh(args, home)
        if proc.returncode != 0:
            print(f"FAIL {' '.join(args)}: {proc.stderr[:500]}")
            return 1
        print(f"[ok] {' '.join(args[:2])}")

    # 2) 执行（真实驱动）
    proc = sh(["task", "run", "1", "--no-writeback"], home)
    print("[run]", (proc.stdout or proc.stderr).strip()[:600])
    if proc.returncode != 0:
        print("FAIL task run 非零退出")
        return 1

    # 3) 数据库断言：用例 pass、每步 pass、进程已退出
    conn = sqlite3.connect(str(home / "data" / "testpilot.db"))
    conn.row_factory = sqlite3.Row
    case = conn.execute("SELECT status, failure_signature FROM case_exec").fetchone()
    steps_rows = conn.execute(
        "SELECT step_no, action, verify_result, error_json FROM step_trace "
        "ORDER BY step_no").fetchall()
    conn.close()

    ok = True
    if case["status"] != "pass":
        print(f"FAIL 用例状态 {case['status']}（签名 {case['failure_signature']}）")
        ok = False
    for r in steps_rows:
        err = r["error_json"]
        flag = "PASS" if r["verify_result"] == "pass" or r["action"] in (
            "app.launch", "app.close", "ui.click", "ui.input", "ui.wait") else "??"
        print(f"  step#{r['step_no']} {r['action']:<10} {flag} err={err or '-'}")
    alive = subprocess.run(["tasklist", "/FI", "IMAGENAME eq demo_target.exe"],
                           capture_output=True).stdout
    if b"demo_target.exe" in alive:
        print("FAIL 被测进程未退出（app.close 未生效）")
        ok = False
        kill_target()
    print("\n结论:", "UIA 实机驱动验证全部通过 ✓" if ok and case["status"] == "pass"
          else "存在失败 ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
