# -*- coding: utf-8 -*-
"""UIA 实机驱动验证（第二批）：下拉/断言变体/wait_gone/名称定位/失败策略。

TC-9101（预期全 pass）：
  ui.select（文本+序号，走新增的键盘选位回退）、assert equals、assert regex、
  ui.wait state=gone（隐藏面板）、ui.wait exists（恢复）、仅按 name 定位点击。
TC-9102（预期 case=fail，验证 on_failure=continue 策略语义）：
  坏目标（2s 超时）continue -> 后续步骤应**全部执行**。

判定：直接断言 DB 内 case_exec.status 与各 step 的落库结果。
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


def sh(args: list[str], home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["TESTPILOT_HOME"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([PY, "-m", "testpilot.app.cli"] + args,
                          cwd=str(ROOT), env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=300)


def kill_target() -> None:
    subprocess.run(["taskkill", "/IM", "demo_target.exe", "/F"], capture_output=True)


def loc(aid: str, ct: str) -> dict:
    return {"type": "uia", "automation_id": aid, "control_type": ct}


def tgt(key: str, aid: str, ct: str) -> dict:
    return {"key": key, "primary": loc(aid, ct)}


def build_9101() -> dict:
    return {
        "case_id": "TC-9101",
        "zentao_case_id": 9101,
        "title": "UIA 扩展验证：下拉/断言变体/wait_gone/名称定位",
        "version": 1,
        "steps": [
            {"id": "s1", "action": "app.launch",
             "args": {"exe": EXE, "title_re": TITLE},
             "source_text": "启动被测演示程序", "timeout_ms": 20000},
            {"id": "s2", "action": "ui.select", "value": "高级",
             "target": tgt("cmb.mode", "cmbMode", "ComboBox"),
             "source_text": "选择模式为高级"},
            {"id": "s3", "action": "assert.ui",
             "verify": {"type": "text", "expect": "模式:高级", "op": "contains",
                        "target": loc("lblMode", "Text"), "timeout_ms": 5000},
             "source_text": "校验模式标签更新"},
            {"id": "s4", "action": "ui.select", "value": "2",
             "target": tgt("cmb.mode", "cmbMode", "ComboBox"),
             "source_text": "按序号选择第 3 项"},
            {"id": "s5", "action": "assert.ui",
             "verify": {"type": "text", "expect": "模式:专家", "op": "contains",
                        "target": loc("lblMode", "Text"), "timeout_ms": 5000},
             "source_text": "校验模式标签更新"},
            {"id": "s6", "action": "assert.ui",
             "verify": {"type": "text", "expect": "版本:2.4.1-build99", "op": "equals",
                        "target": loc("lblVersion", "Text"), "timeout_ms": 5000},
             "source_text": "等值断言版本标签"},
            {"id": "s7", "action": "assert.ui",
             "verify": {"type": "text", "expect": r"^版本:\d\.\d\.\d-build\d+$",
                        "op": "regex",
                        "target": loc("lblVersion", "Text"), "timeout_ms": 5000},
             "source_text": "正则断言版本号格式"},
            {"id": "s8", "action": "ui.click",
             "target": tgt("btn.toggle", "btnToggle", "Button"),
             "source_text": "隐藏面板"},
            {"id": "s9", "action": "ui.wait", "args": {"state": "gone"},
             "target": tgt("lbl.panel", "lblPanel", "Text"), "timeout_ms": 5000,
             "source_text": "等待面板消失"},
            {"id": "s10", "action": "ui.click",
             "target": tgt("btn.toggle", "btnToggle", "Button"),
             "source_text": "显示面板"},
            {"id": "s11", "action": "ui.wait", "args": {"state": "exists"},
             "target": tgt("lbl.panel", "lblPanel", "Text"), "timeout_ms": 5000,
             "source_text": "等待面板恢复"},
            {"id": "s12", "action": "ui.click",
             "target": {"key": "btn.reset.byname",
                        "primary": {"type": "uia", "name": "重置",
                                    "control_type": "Button"}},
             "source_text": "仅按名称定位点击「重置」"},
            {"id": "s13", "action": "assert.ui",
             "verify": {"type": "text", "expect": "状态:READY", "op": "contains",
                        "target": loc("lblStatus", "Text"), "timeout_ms": 5000},
             "source_text": "校验重置生效"},
            {"id": "s14", "action": "app.close", "args": {"mode": "graceful"},
             "source_text": "关闭被测程序"},
        ],
    }


def build_9102() -> dict:
    return {
        "case_id": "TC-9102",
        "zentao_case_id": 9102,
        "title": "UIA 失败策略：on_failure=continue",
        "version": 1,
        "steps": [
            {"id": "s1", "action": "app.launch",
             "args": {"exe": EXE, "title_re": TITLE},
             "source_text": "启动被测演示程序", "timeout_ms": 20000},
            {"id": "s2", "action": "ui.click",
             "target": tgt("btn.ghost", "btnNoSuchControl", "Button"),
             "timeout_ms": 2000,
             "on_failure": "continue",
             "source_text": "点击不存在的控件（应失败但继续）"},
            {"id": "s3", "action": "ui.click",
             "target": tgt("btn.reset", "btnReset", "Button"),
             "source_text": "后续步骤应照常执行"},
            {"id": "s4", "action": "assert.ui",
             "verify": {"type": "text", "expect": "状态:READY", "op": "contains",
                        "target": loc("lblStatus", "Text"), "timeout_ms": 5000},
             "source_text": "校验继续执行"},
            {"id": "s5", "action": "app.close", "args": {"mode": "graceful"},
             "source_text": "关闭被测程序"},
        ],
    }


def run_case(home: Path, case: dict, task_name: str) -> tuple[dict, list[dict]]:
    case_file = home / f"{case['case_id']}.json"
    case_file.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
    for args in (["case", "import", "--file", str(case_file)],
                 ["case", "accept", case["case_id"]],
                 ["task", "create", "--name", task_name, "--cases", case["case_id"]]):
        proc = sh(args, home)
        if proc.returncode != 0:
            raise SystemExit(f"FAIL {' '.join(args)}: {proc.stderr[:400]}")
    proc = sh(["task", "list", "--pretty"], home)
    # 按 case_ids 包含关系定位刚建的任务（list 排序不可依赖）
    tasks = json.loads(proc.stdout)["tasks"]
    tid = next(t["id"] for t in tasks if case["case_id"] in t["case_ids"])
    proc = sh(["task", "run", str(tid), "--no-writeback"], home)
    if proc.returncode != 0:
        raise SystemExit(f"FAIL task run: {proc.stderr[:400]}")

    conn = sqlite3.connect(str(home / "data" / "testpilot.db"))
    conn.row_factory = sqlite3.Row
    case_row = dict(conn.execute(
        "SELECT status, failure_signature FROM case_exec WHERE case_id=?",
        (case["case_id"],)).fetchone())
    steps = [dict(r) for r in conn.execute(
        "SELECT step_no, action, verify_result, error_json FROM step_trace "
        "WHERE case_exec_id = (SELECT id FROM case_exec WHERE case_id=?) "
        "ORDER BY step_no", (case["case_id"],))]
    conn.close()
    return case_row, steps


def main() -> int:
    kill_target()
    time.sleep(0.5)
    csc = subprocess.run(
        [CSC, "-nologo", "-target:winexe", f"-out:{EXE}",
         str(ROOT / "tools" / "demo_target.cs")], capture_output=True)
    if csc.returncode != 0:
        print("FAIL 编译:", (csc.stdout + csc.stderr).decode("gbk", "replace")[:400])
        return 1

    home = Path(tempfile.mkdtemp(prefix="tp_uia2_"))
    print(f"[setup] {home}")
    ok = True

    # ---- TC-9101：预期全 pass ----
    case, steps = run_case(home, build_9101(), "扩展验证")
    if case["status"] != "pass":
        print(f"FAIL TC-9101 case={case['status']} sig={case['failure_signature']}")
        ok = False
    for s in steps:
        print(f"  9101 s{s['step_no']:<2} {s['action']:<9} "
              f"{s['verify_result'] or '-'} {s['error_json'] or ''}"[:150])
    if any(s["verify_result"] not in ("pass", None) for s in steps):
        print("FAIL TC-9101 有步骤未通过")
        ok = False

    # ---- TC-9102：预期 case=fail 且后续步骤全部执行 ----
    case, steps = run_case(home, build_9102(), "失败策略验证")
    if case["status"] != "fail":
        print(f"FAIL TC-9102 预期 fail（continue 策略语义），实际 {case['status']}")
        ok = False
    for s in steps:
        print(f"  9102 s{s['step_no']:<2} {s['action']:<9} "
              f"{s['verify_result'] or '-'} {s['error_json'] or ''}"[:150])
    if len(steps) != 5:
        print(f"FAIL TC-9102 步骤数 {len(steps)} != 5（continue 后应全部执行）")
        ok = False
    if steps[-1]["action"] != "app.close" or steps[2]["error_json"] is not None:
        print("FAIL TC-9102 后续步骤未正常执行")
        ok = False

    kill_target()
    print("\n结论:", "UIA 扩展驱动验证全部通过 ✓" if ok else "存在失败 ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
