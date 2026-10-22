# -*- coding: utf-8 -*-
"""UIA 实机驱动验证（第四批）：vision 驱动 L1 模板匹配 + 真实鼠标点击。

TC-9104（预期全 pass）：
- 阶段 A：启动被测程序，从**引擎同一套截图通道**（grab_screen）裁剪「提交」按钮
  存为模板 —— 保证模板与运行期截图同源同缩放；
- 阶段 B：CLI 全链路执行用例：
  s2 vision.click(template) -> 状态变 DONE；s4 UIA 重置；s5 **同一模板再次命中**
  （验证 L1 复用稳定）；s6 断言；s7 关闭。

判定：DB 内 case=pass。
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
CASE_ID = "TC-9104"
TEMPLATE = "submit_btn.png"


def sh(args: list[str], home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["TESTPILOT_HOME"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([PY, "-m", "testpilot.app.cli"] + args,
                          cwd=str(ROOT), env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=300)


def make_template(home: Path) -> None:
    """启动被测程序 -> grab_screen 整窗 -> 裁「提交」按钮区域 -> 存模板。"""
    subprocess.run(["taskkill", "/IM", "demo_target.exe", "/F"], capture_output=True)
    time.sleep(0.5)
    subprocess.Popen([EXE])
    time.sleep(2.5)

    from pywinauto import Desktop
    from testpilot.drivers.vision_driver import grab_screen
    from testpilot.infra.imaging import decode_png, imwrite
    from testpilot.infra.opencv import load_cv2

    win = Desktop(backend="uia").window(title_re=TITLE)
    win.wait("visible ready", timeout=10)
    wrect = win.rectangle()
    win_rect = (wrect.left, wrect.top, wrect.right - wrect.left,
                wrect.bottom - wrect.top)
    shot = grab_screen(win_rect)
    img = decode_png(shot.png)
    cv2 = load_cv2()
    h, w = img.shape[:2]

    btn = win.child_window(auto_id="btnSubmit", control_type="Button").rectangle()
    # 按钮矩形相对窗口，裁剪时各收 2px 边（避免吃进邻接像素）
    x0 = max(0, btn.left - wrect.left + 2)
    y0 = max(0, btn.top - wrect.top + 2)
    x1 = min(w, btn.right - wrect.left - 2)
    y1 = min(h, btn.bottom - wrect.top - 2)
    crop = img[y0:y1, x0:x1]
    assert crop.size > 0, "裁剪为空"
    tdir = home / "data" / "templates"
    tdir.mkdir(parents=True, exist_ok=True)
    if not imwrite(tdir / TEMPLATE, crop):
        raise SystemExit("FAIL 模板写盘失败")
    print(f"[template] {tdir / TEMPLATE}（{crop.shape[1]}x{crop.shape[0]}）")
    subprocess.run(["taskkill", "/IM", "demo_target.exe", "/F"], capture_output=True)
    time.sleep(0.5)


def build_case() -> dict:
    return {
        "case_id": CASE_ID,
        "zentao_case_id": 9104,
        "title": "vision 驱动：L1 模板定位 + 真实鼠标点击",
        "version": 1,
        "steps": [
            {"id": "s1", "action": "app.launch",
             "args": {"exe": EXE, "title_re": TITLE},
             "source_text": "启动被测演示程序", "timeout_ms": 20000},
            {"id": "s2", "action": "vision.click", "driver": "vision_only",
             "target": {"key": "btn.submit.tpl",
                        "primary": {"type": "template", "template": TEMPLATE}},
             "source_text": "视觉模板点击「提交」按钮"},
            {"id": "s3", "action": "assert.ui",
             "verify": {"type": "text", "expect": "状态:DONE", "op": "contains",
                        "target": {"type": "uia", "automation_id": "lblStatus",
                                   "control_type": "Text"},
                        "timeout_ms": 5000},
             "source_text": "校验提交生效"},
            {"id": "s4", "action": "ui.click",
             "target": {"key": "btn.reset",
                        "primary": {"type": "uia", "automation_id": "btnReset",
                                    "control_type": "Button"}},
             "source_text": "UIA 重置（混搭验证）"},
            {"id": "s5", "action": "vision.click", "driver": "vision_only",
             "target": {"key": "btn.submit.tpl",
                        "primary": {"type": "template", "template": TEMPLATE}},
             "source_text": "同一模板再次命中（L1 复用）"},
            {"id": "s6", "action": "assert.ui",
             "verify": {"type": "text", "expect": "状态:DONE", "op": "contains",
                        "target": {"type": "uia", "automation_id": "lblStatus",
                                   "control_type": "Text"},
                        "timeout_ms": 5000},
             "source_text": "复验提交生效"},
            {"id": "s7", "action": "app.close", "args": {"mode": "graceful"},
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

    home = Path(tempfile.mkdtemp(prefix="tp_uia4_"))
    print(f"[setup] {home}")
    make_template(home)

    case_file = home / "case.json"
    case_file.write_text(json.dumps(build_case(), ensure_ascii=False), encoding="utf-8")
    for args in (["case", "import", "--file", str(case_file)],
                 ["case", "accept", CASE_ID],
                 ["task", "create", "--name", "vision 模板验证", "--cases", CASE_ID]):
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
        "SELECT step_no, action, driver, verify_result, error_json FROM step_trace "
        "ORDER BY step_no").fetchall()
    conn.close()

    ok = case["status"] == "pass"
    for s in steps:
        mark = "PASS" if (s["verify_result"] == "pass"
                          or s["action"] in ("app.launch", "app.close")) else "FAIL?"
        print(f"  s{s['step_no']:<2} {s['action']:<12} drv={s['driver'] or '-':<7} "
              f"{mark} {s['error_json'] or ''}"[:160])
    if not ok:
        print(f"FAIL TC-9104 case={case['status']}")
    subprocess.run(["taskkill", "/IM", "demo_target.exe", "/F"], capture_output=True)
    print("\n结论:", "vision 模板驱动验证全部通过 ✓" if ok else "存在失败 ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
