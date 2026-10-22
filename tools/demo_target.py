# -*- coding: utf-8 -*-
"""TestPilot UIA 实机验证的被测样例程序（tkinter，经典 Win32 控件实现）。

窗口标题固定（供 app.launch title_re 附加），控件语义：
- 输入框 Entry：供 ui.input 验证（clear + 输入）
- 按钮「提交」：点击后把自身文案改为「已提交」——供 ui.click + text 断言
- 标签「状态:READY」：点击提交后变「状态:DONE」——供 text 断言（按 control_type+index 定位）
- 按钮「重置」：恢复初始态，便于重复跑

仅供引擎驱动验证使用，不参与产品功能。
"""
import tkinter as tk


class DemoApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("TestPilot 被测演示程序")
        self.root.geometry("460x220+80+80")

        self.label_status = tk.Label(self.root, text="状态:READY")
        self.label_status.place(x=20, y=16)

        tk.Label(self.root, text="输入:").place(x=20, y=56)
        self.entry = tk.Entry(self.root, width=34)
        self.entry.place(x=70, y=54)
        self.entry.insert(0, "初始文本")

        self.btn_submit = tk.Button(self.root, text="提交", width=12,
                                    command=self.on_submit)
        self.btn_submit.place(x=70, y=96)

        tk.Button(self.root, text="重置", width=12,
                  command=self.on_reset).place(x=180, y=96)

        self.label_echo = tk.Label(self.root, text="回显:-")
        self.label_echo.place(x=20, y=150)

    def on_submit(self) -> None:
        text = self.entry.get()
        self.label_status.config(text="状态:DONE")
        self.label_echo.config(text=f"回显:{text}")

    def on_reset(self) -> None:
        self.label_status.config(text="状态:READY")
        self.label_echo.config(text="回显:-")
        self.entry.delete(0, tk.END)
        self.entry.insert(0, "初始文本")

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    DemoApp().run()
