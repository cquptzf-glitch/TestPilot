// TestPilot UIA 实机验证的被测样例程序（WinForms，经典 Win32 控件）。
//
// 设计要点：WinForms 会把 Control.Name 透出为 UIA AutomationId —— 控件有**稳定
// 语义键**，与引擎 uia 定位（automation_id）完全匹配；Edit 是经典 Win32 Edit，
// GetWindowText 即内容（引擎 read_text 直接可读）。按钮点击后改自身/状态条文案，
// 供「点击 + 文本断言」闭环验证。
//
// 编译（.NET Framework 4 自带 csc，无需装任何东西）：
//   C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe /nologo /winexe:False ^
//     /out:.cache\demo_target.exe tools\demo_target.cs
using System;
using System.Drawing;
using System.Windows.Forms;

internal static class DemoTarget
{
    private static Label lblStatus;
    private static Label lblEcho;
    private static Label lblMode;
    private static Label lblVersion;
    private static Label lblPanel;
    private static Label lblDate;
    private static Label lblCheck;
    private static TextBox txtInput;
    private static Button btnSubmit;
    private static Button btnToggle;
    private static ComboBox cmbMode;
    private static DateTimePicker dtpDate;
    private static DataGridView gridData;
    private static CheckBox chkAgree;
    private static ListBox lstItems;

    [STAThread]
    private static void Main()
    {
        var form = new Form
        {
            Text = "TestPilot 被测演示程序",
            Size = new Size(560, 520),
            StartPosition = FormStartPosition.Manual,
            DesktopLocation = new Point(80, 80),
            TopMost = true,   // 夹具加固：演示窗不被并排窗口（浏览器/IDE）遮挡
        };

        lblStatus = new Label { Name = "lblStatus", Text = "状态:READY", Left = 20, Top = 16, Width = 220 };
        lblEcho = new Label { Name = "lblEcho", Text = "回显:-", Left = 20, Top = 150, Width = 400 };
        lblMode = new Label { Name = "lblMode", Text = "模式:标准", Left = 250, Top = 16, Width = 180 };
        // 供 equals/regex 断言的稳定内容（regex: ^版本:\d\.\d\.\d-build\d+$）
        lblVersion = new Label { Name = "lblVersion", Text = "版本:2.4.1-build99", Left = 250, Top = 150, Width = 200 };
        // 供 ui.wait state=gone：btnToggle 切换其 Visible
        lblPanel = new Label { Name = "lblPanel", Text = "面板内容", Left = 20, Top = 190, Width = 200 };
        // 时间选择回显（供断言）；lblCheck 复选框回显
        lblDate = new Label { Name = "lblDate", Text = "日期:2026-09-12", Left = 20, Top = 440, Width = 240 };
        lblCheck = new Label { Name = "lblCheck", Text = "勾选:否", Left = 280, Top = 440, Width = 160 };

        var lblInput = new Label { Name = "lblInput", Text = "输入:", Left = 20, Top = 58, Width = 44 };
        txtInput = new TextBox { Name = "txtInput", Text = "初始文本", Left = 70, Top = 55, Width = 220 };

        btnSubmit = new Button { Name = "btnSubmit", Text = "提交", Left = 70, Top = 96, Width = 94 };
        btnSubmit.Click += OnSubmit;

        var btnReset = new Button { Name = "btnReset", Text = "重置", Left = 180, Top = 96, Width = 94 };
        btnReset.Click += OnReset;

        btnToggle = new Button { Name = "btnToggle", Text = "隐藏面板", Left = 290, Top = 96, Width = 94 };
        btnToggle.Click += OnToggle;

        cmbMode = new ComboBox { Name = "cmbMode", Left = 310, Top = 53, Width = 120,
                                 DropDownStyle = ComboBoxStyle.DropDownList };
        cmbMode.Items.AddRange(new object[] { "标准", "高级", "专家" });
        cmbMode.SelectedIndex = 0;
        cmbMode.SelectedIndexChanged += (s, e) =>
            lblMode.Text = "模式:" + (cmbMode.SelectedItem as string ?? "?");

        // 时间选择：Change 事件回显（供 text 断言）
        dtpDate = new DateTimePicker { Name = "dtpDate", Left = 70, Top = 400, Width = 220,
                                       Format = DateTimePickerFormat.Short, Value = new DateTime(2026, 9, 12) };
        dtpDate.ValueChanged += (s, e) => lblDate.Text = "日期:" + dtpDate.Value.ToString("yyyy-MM-dd");

        // 表格：3 行 2 列；单元格点击回显到状态条（供"点到具体单元格"验证）
        gridData = new DataGridView
        {
            Name = "gridData", Left = 20, Top = 220, Width = 400, Height = 150,
            AllowUserToAddRows = false, ReadOnly = true, RowHeadersVisible = false,
        };
        gridData.Columns.Add("colName", "名称");
        gridData.Columns.Add("colValue", "值");
        gridData.Rows.Add("alpha", "1");
        gridData.Rows.Add("beta", "2");
        gridData.Rows.Add("gamma", "3");
        gridData.CellClick += (s, e) =>
            lblStatus.Text = string.Format("状态:cell({0},{1})", e.RowIndex, e.ColumnIndex);

        chkAgree = new CheckBox { Name = "chkAgree", Text = "同意条款", Left = 280, Top = 398, Width = 120 };
        chkAgree.CheckedChanged += (s, e) => lblCheck.Text = "勾选:" + (chkAgree.Checked ? "是" : "否");

        lstItems = new ListBox { Name = "lstItems", Left = 420, Top = 220, Width = 110, Height = 150 };
        lstItems.Items.AddRange(new object[] { "苹果", "香蕉", "樱桃" });
        lstItems.SelectedIndex = 0;

        form.Controls.AddRange(new Control[]
        {
            lblStatus, lblEcho, lblMode, lblVersion, lblPanel, lblDate, lblCheck,
            lblInput, txtInput, btnSubmit, btnReset, btnToggle, cmbMode,
            dtpDate, gridData, chkAgree, lstItems,
        });

        Console.WriteLine("demo-target ready");
        Application.Run(form);
    }

    private static void OnSubmit(object sender, EventArgs e)
    {
        lblStatus.Text = "状态:DONE";
        lblEcho.Text = "回显:" + txtInput.Text;
        btnSubmit.Text = "已提交";
    }

    private static void OnReset(object sender, EventArgs e)
    {
        lblStatus.Text = "状态:READY";
        lblEcho.Text = "回显:-";
        txtInput.Text = "初始文本";
        btnSubmit.Text = "提交";
        cmbMode.SelectedIndex = 0;
    }

    private static void OnToggle(object sender, EventArgs e)
    {
        lblPanel.Visible = !lblPanel.Visible;
        btnToggle.Text = lblPanel.Visible ? "隐藏面板" : "显示面板";
    }
}
