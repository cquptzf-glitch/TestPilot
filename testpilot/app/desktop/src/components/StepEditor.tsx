import { useMemo, useState } from 'react';

type Obj = Record<string, unknown>;
const object = (v: unknown): Obj => v && typeof v === 'object' && !Array.isArray(v) ? v as Obj : {};
const ACTIONS: [string, string][] = [
  ['app.launch', '启动应用'], ['app.close', '关闭应用'], ['app.wait_ready', '等待应用就绪'],
  ['ui.click', '点击控件'], ['ui.input', '输入文本'], ['ui.select', '选择选项'],
  ['ui.toggle', '勾选或取消勾选'], ['ui.keypress', '键盘操作'],
  ['ui.right_click', '右键 / 上下文菜单'], ['ui.scroll', '滚动控件'],
  ['ui.drag', '拖拽控件'], ['ui.wait', '等待控件'], ['vision.click', '视觉定位并点击'],
  ['assert.ui', '检查界面文本'], ['assert.state', '检查设备状态'], ['assert.vision', '检查画面'],
  ['device.scene', '加载设备场景'], ['device.call', '发送设备指令'], ['sleep', '等待一段时间'],
];

export function StepEditor({ text, onChange }: { text: string; onChange: (value: string) => void }) {
  const [selected, setSelected] = useState(0);
  const draft = useMemo(() => { try { return object(JSON.parse(text)); } catch { return null; } }, [text]);
  const steps = Array.isArray(draft?.steps) ? draft.steps as Obj[] : [];
  const index = Math.min(selected, Math.max(0, steps.length - 1));
  const step = object(steps[index]);
  const action = String(step.action ?? '');
  const target = object(step.target);
  const primary = object(target.primary);
  const verify = object(step.verify);
  const args = object(step.args);
  if (!draft || !steps.length) return null;
  const update = (patch: Obj) => {
    const next = steps.map((s, i) => i === index ? { ...s, ...patch, uncertain: false } : s);
    onChange(JSON.stringify({ ...draft, steps: next }, null, 2));
  };
  const arg = (key: string, value: unknown) => update({ args: { ...args, [key]: value } });
  const locator = (key: string, value: unknown) => update({ target: {
    ...target, key: target.key ?? String(step.id ?? 'element'),
    primary: { type: 'uia', ...primary, [key]: value },
  } });
  return <details className="step-editor">
    <summary>逐步修订 <span>直接核对操作、目标和预期结果，无需编辑 JSON</span></summary>
    <div className="step-editor-grid">
      <label>当前步骤<select aria-label="当前步骤" value={index} onChange={(e) => setSelected(Number(e.target.value))}>
        {steps.map((s, i) => <option key={i} value={i}>第 {i + 1} 步 · {ACTIONS.find(([key]) => key === s.action)?.[1] ?? String(s.action ?? '待修正')}</option>)}
      </select></label>
      <label>操作类型<select aria-label="操作类型" value={action} onChange={(e) => {
        const value = e.target.value;
        const kind = { 'assert.ui': 'text', 'assert.state': 'state', 'assert.vision': 'vision' }[value];
        update({ action: value, ...(kind ? { verify: { ...verify, type: kind } } : {}) });
      }}><option value="">请选择操作</option>{ACTIONS.map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
      {(['ui.click', 'ui.input', 'ui.select', 'ui.toggle', 'ui.keypress',
          'ui.right_click', 'ui.scroll', 'ui.drag', 'ui.wait',
          'assert.ui', 'vision.click'].includes(action)) && <>
        <label>目标控件名称<input aria-label="目标控件名称" value={String(primary.name ?? '')} onChange={(e) => locator('name', e.target.value)} placeholder="例如：开始检测" /></label>
        <label>控件自动化 ID（可选）<input aria-label="控件自动化 ID" value={String(primary.automation_id ?? '')} onChange={(e) => locator('automation_id', e.target.value)} /></label>
        <label>控件类型（可选）<input aria-label="控件类型" value={String(primary.control_type ?? '')} onChange={(e) => locator('control_type', e.target.value || null)} placeholder="如 Edit、Tab、Table、TreeItem" /></label>
        <label>表格行（从 0 开始，可选）<input type="number" min="0" aria-label="表格行" value={primary.row == null ? '' : Number(primary.row)} onChange={(e) => locator('row', e.target.value === '' ? null : Number(e.target.value))} /></label>
        <label>表格列（从 0 开始，可选）<input type="number" min="0" aria-label="表格列" value={primary.column == null ? '' : Number(primary.column)} onChange={(e) => locator('column', e.target.value === '' ? null : Number(e.target.value))} /></label>
      </>}
      {action === 'ui.toggle' && <label>勾选状态<select aria-label="勾选状态" value={String(step.value ?? '')} onChange={(e) => update({ value: e.target.value })}><option value="">请选择</option><option value="true">勾选</option><option value="false">取消勾选</option></select></label>}
      {action === 'ui.keypress' && <label>键序列<input aria-label="键序列" value={String(step.value ?? '')} onChange={(e) => update({ value: e.target.value })} placeholder="{F2} 或 {ENTER}" /></label>}
      {action === 'ui.scroll' && <>
        <label>滚动方向<select aria-label="滚动方向" value={String(args.direction ?? 'down')}
          onChange={(e) => arg('direction', e.target.value)}>
          <option value="up">向上</option><option value="down">向下</option>
          <option value="left">向左</option><option value="right">向右</option>
        </select></label>
        <label>滚动格数<input type="number" min="1" max="20" aria-label="滚动格数"
          value={Number(args.amount ?? 3)}
          onChange={(e) => arg('amount', Number(e.target.value))} /></label>
      </>}
      {action === 'ui.drag' && <>
        <label>拖拽 X 偏移<input type="number" aria-label="拖拽 X 偏移"
          value={Number(args.dx ?? 0)} onChange={(e) => arg('dx', Number(e.target.value))} /></label>
        <label>拖拽 Y 偏移<input type="number" aria-label="拖拽 Y 偏移"
          value={Number(args.dy ?? 0)} onChange={(e) => arg('dy', Number(e.target.value))} /></label>
        <label>拖拽耗时 ms<input type="number" min="0" max="10000" aria-label="拖拽耗时"
          value={Number(args.duration_ms ?? 300)}
          onChange={(e) => arg('duration_ms', Number(e.target.value))} /></label>
      </>}
      {['ui.input', 'ui.select'].includes(action) && <label>输入或选择内容<input aria-label="输入或选择内容" value={String(step.value ?? '')} onChange={(e) => update({ value: e.target.value })} /></label>}
      {action === 'device.scene' && <label>设备场景<input aria-label="设备场景" value={String(args.scene_id ?? '')} onChange={(e) => arg('scene_id', e.target.value)} /></label>}
      {action === 'device.call' && <label>设备指令<input aria-label="设备指令" value={String(args.cmd ?? '')} onChange={(e) => arg('cmd', e.target.value)} /></label>}
      {action === 'app.launch' && <><label>应用路径<input aria-label="应用路径" value={String(args.exe ?? '')} onChange={(e) => arg('exe', e.target.value)} /></label><label>窗口标题匹配<input aria-label="窗口标题匹配" value={String(args.title_re ?? '')} onChange={(e) => arg('title_re', e.target.value)} placeholder="留空路径可附加已打开窗口" /></label></>}
      {(verify.type === 'state' || action === 'assert.state') && <label>状态路径<input aria-label="状态路径" value={String(args.path ?? '')} onChange={(e) => arg('path', e.target.value)} /></label>}
      <label>验证方式<select aria-label="验证方式" value={String(verify.type ?? 'none')} onChange={(e) => update({ verify: { ...verify, type: e.target.value } })}>
        <option value="none">无需验证</option><option value="text">控件文本</option><option value="state">设备状态</option><option value="vision">画面内容</option><option value="screenshot_diff">画面变化</option>
      </select></label>
      {typeof verify.type === 'string' && verify.type !== 'none' && <label>预期结果<input aria-label="预期结果" value={String(args.expect ?? verify.expect ?? '')} onChange={(e) => {
        const patch: Obj = { verify: { ...verify, expect: e.target.value } };
        if ('expect' in args) patch.args = { ...args, expect: e.target.value };
        update(patch);
      }} placeholder="例如：检测完成" /></label>}
    </div>
    {step.source_text != null && <p className="step-source">用例原文：{String(step.source_text)}</p>}
  </details>;
}
