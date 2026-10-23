/** 开发态数据 Mock（仅在 `!inTauri && import.meta.env.DEV` 时加载）。
 *
 *  为什么：`npm run dev` 此前只能看到一排"不在 Tauri 壳内"报错，前端样式与
 *  交互无法脱离引擎调。这里模拟壳命令与 RPC 方法表（同名同形），页面在浏览器里
 *  以**样例数据**完整渲染，且关键写操作（建任务/建方案/人审/执行）会**真实变更
 *  内存态** —— 让浏览器自动化能把"点击 -> 状态变化"整条链路验证起来。
 *  生产构建被 import.meta.env.DEV 常量折叠，整个文件不会进产物。
 */
import type { ConfigDescribe, EngineHealth, EngineStatus, RunDetail, RunRow,
              ShellMetrics } from './types';

/* eslint-disable @typescript-eslint/no-explicit-any */

const HEALTH: EngineHealth = {
  ok: true,
  db: { ok: true, detail: 'D:/demo/TestPilot/data/testpilot.db' },
  drivers: {
    uia: { ok: true, detail: 'pywinauto 0.6.9 backend=uia' },
    vision: { ok: true, detail: 'opencv 4.10；VLM 未装配（仅 L1 模板可用）' },
  },
  zentao: { configured: true },
  agent: { ok: true, detail: 'pi 0.52.12 · RPC-only tools' },
  svn: { ok: true, detail: '1.14.5' },
  emulator: { connected: true },
  dpi: {
    aware: true, scale: 1.5, space: 'physical', resolution: '2560x1440',
    monitors: [{ index: 1, rect: [0, 0, 2560, 1440], primary: true }],
  },
  warnings: ['环境告警示例：VLM 未装配，视觉语义定位不可用'],
  paths: {
    db: 'D:/demo/TestPilot/data/testpilot.db',
    screenshot_root: 'D:/demo/TestPilot/data/shots',
    report_root: 'D:/demo/TestPilot/data/reports',
  },
};

const STATUS: EngineStatus = {
  state: 'running', pid: 4321, restarts: 0, rpcConnected: true, detail: '',
};

const METRICS: ShellMetrics = {
  state: 'running', restarts: 0, rpcConnected: true,
  rpcCalls: 132, rpcErrors: 1, rpcLastMs: 6, rpcAvgMs: 11,
  shellLog: 'D:/demo/TestPilot/logs/shell.log',
};

// ---------------------------------------------------------------- 可变存储

interface MockCase {
  case_id: string; zentao_case_id: number | null; title: string; version: number;
  status: string; sync_status: string; steps: unknown[]; updated_at: string;
  last_synced_at: string; module_id: number; module_name: string;
  source_text?: string; preconditions?: string[];
}

let mockZentaoSyncedAt = '2026-09-12 09:00:00';
let suiteSeq = 3;
let taskSeq = 4;
let runSeq = 43;
let activeMockRun: { row: RunRow; aborted: boolean } | null = null;

interface MockTask {
  id: number; zentao_task_id: number | null; name: string;
  suite_id: number | null; case_ids: string[]; preconditions: string[];
  driver_config: Record<string, unknown>; sync_status: string;
  created_at: string; runs: RunRow[];
}

const CASES: MockCase[] = [
  { case_id: 'TC-1024', zentao_case_id: 1024, title: '新建检测任务并启动',
    version: 3, status: 'active', sync_status: 'stale', steps: [{}, {}, {}],
    updated_at: '2026-09-11 20:12:00', last_synced_at: '2026-09-11 20:12:00',
    module_id: 5, module_name: '登录模块',
    source_text: '1. 新建任务\n2. 启动检测', preconditions: ['被测程序已登录'] },
  { case_id: 'TC-1025', zentao_case_id: 1025, title: '导出检测报告（PDF）',
    version: 2, status: 'active', sync_status: 'synced', steps: [{}, {}],
    updated_at: '2026-09-10 09:00:00', last_synced_at: '2026-09-10 09:00:00',
    module_id: 6, module_name: '订单模块',
    source_text: '1. 打开报告页\n2. 点击导出' },
  { case_id: 'TC-1031', zentao_case_id: 1031, title: '模拟器场景切换',
    version: 0, status: 'draft', sync_status: 'conflict', steps: [{}],
    updated_at: '2026-09-09 18:40:00', last_synced_at: '2026-09-09 18:40:00',
    module_id: 5, module_name: '登录模块' },
  { case_id: 'TC-1032', zentao_case_id: 1032, title: '打印报告归档',
    version: 0, status: 'draft', sync_status: 'synced', steps: [],
    updated_at: '2026-09-08 11:00:00', last_synced_at: '2026-09-08 11:00:00',
    module_id: 6, module_name: '订单模块' },
];

for (const c of CASES) {
  c.steps = c.steps.map((_, index) => ({
    id: `s${index + 1}`, action: index === 0 ? 'ui.click' : 'assert.ui',
    source_text: index === 0 ? '点击开始检测按钮' : '检查状态栏显示检测完成',
    target: { key: index === 0 ? 'btn.start_detect' : 'lbl.status',
              primary: { type: 'uia', name: index === 0 ? '开始检测' : '状态栏' } },
    verify: index === 0 ? { type: 'none' } : { type: 'text', expect: '检测完成' },
  }));
}

interface MockSuite {
  id: number; name: string; case_ids: string[]; preconditions: string[];
  driver_config: Record<string, unknown>; description: string;
  updated_at: string; not_runnable: { case_id: string; reason: string }[];
}

const SUITES: MockSuite[] = [
  { id: 1, name: '核心回归 24 例', case_ids: ['TC-1024', 'TC-1025', 'TC-1031'],
    preconditions: ['模拟器场景: normal_a'], driver_config: { mode: 'auto' },
    description: '', updated_at: '2026-09-10 10:00:00', not_runnable: [] },
  { id: 2, name: '夜间冒烟', case_ids: ['TC-1001', 'TC-1002'],
    preconditions: [], driver_config: { mode: 'vision_first' },
    description: '', updated_at: '2026-09-08 10:00:00', not_runnable: [] },
];

const TASKS: MockTask[] = [
  { id: 3, zentao_task_id: 88, name: '每日回归', suite_id: 1,
    case_ids: ['TC-1024', 'TC-1025', 'TC-1031'], preconditions: [],
    driver_config: { mode: 'auto' }, sync_status: 'synced',
    created_at: '2026-09-01 10:00:00', runs: [] },
  { id: 2, zentao_task_id: null, name: '冒烟-夜间', suite_id: 2,
    case_ids: ['TC-1001', 'TC-1002'], preconditions: [],
    driver_config: { mode: 'vision_first' }, sync_status: 'sync_pending',
    created_at: '2026-08-30 15:30:00', runs: [] },
];

function mkRun(id: number, task_id: number, started: string, pass: number, fail: number): RunRow {
  const total = pass + fail;
  return { id, task_id, started_at: started, finished_at: started,
           status: 'completed',
           stats: { total, pass, fail, blocked: 0, skip: 0, writeback: 'synced',
                    llm_usage: { calls: 3, tokens: 1200, errors: 0 } } };
}

const RUNS: RunRow[] = [
  mkRun(42, 3, '2026-09-12 09:12:44', 21, 3),
  mkRun(41, 3, '2026-09-11 21:00:12', 19, 5),
  mkRun(40, 2, '2026-09-11 02:00:00', 12, 0),
];
TASKS[0].runs = [RUNS[0], RUNS[1]];
TASKS[1].runs = [RUNS[2]];

const KB_PENDING = [
  { id: 7, type: 'failure', signature: 'uia:click:btn_start_detect|locate_timeout',
    confidence: 0.8, updated_at: '2026-09-12 09:31:00',
    payload: { category: 'locate_timeout', disposition: 'retry_ready',
               suggestion: '预热要等 3s 后再找按钮', error_type: 'locate_timeout',
               action: 'ui.click', target_key: 'btn.start_detect', page_key: 'main' } },
  { id: 8, type: 'failure', signature: 'assert:ui:text:lbl_status|verify_failed',
    confidence: 0.62, updated_at: '2026-09-12 09:30:00',
    payload: { category: 'verify_failed', disposition: 'none',
               suggestion: '状态文案断言失败，确认需求口径', error_type: 'verify_failed',
               action: 'assert.ui', target_key: 'lbl.status', page_key: 'main' } },
];

const KB_CTX: { id: number; case_id: string; signature: string;
  updated_at: string; case_title: string | null; zentao_case_id: number | null;
  payload: { scene?: string; pitfalls?: string[]; notes?: string } }[] = [
  { id: 1, case_id: 'TC-1024', signature: 'ctx:TC-1024', updated_at: '2026-09-11 19:00:00',
    case_title: '新建检测任务并启动', zentao_case_id: 1024,
    payload: { scene: 'normal_a', pitfalls: ['预热要等 3 秒'], notes: '按钮 accessibleName 已适配' } },
];

const CONFIG: ConfigDescribe = {
  path: 'D:/demo/TestPilot/data/config.override.yaml',
  exists: true,
  groups: [
    { group: '驱动路由', description: '双驱动路由与重试', fields: [
      { path: 'driver.mode', label: '驱动模式', hint: 'auto = 按注册顺序', type: 'enum',
        enum: ['auto', 'uia_first', 'vision_first', 'uia_only', 'vision_only'],
        min: null, max: null, step: null, value: 'uia_first', default: 'auto',
        overridden: true, secret_like: false },
      { path: 'driver.retry_on_failure', label: '定位失败自动重试', hint: '仅对定位超时生效',
        type: 'boolean', enum: null, min: null, max: null, step: null,
        value: true, default: true, overridden: false, secret_like: false },
      { path: 'driver.vision_min_confidence', label: 'VLM 最低置信度', hint: '低于该值不点击',
        type: 'number', enum: null, min: 0, max: 1, step: 0.05,
        value: 0.7, default: 0.7, overridden: false, secret_like: false },
    ] },
    { group: '禅道', description: '禅道服务器与凭据', fields: [
      { path: 'zentao.base_url', label: '服务器地址', hint: '', type: 'string', enum: null,
        min: null, max: null, step: null, value: 'https://zentao.example.com',
        default: 'https://zentao.example.com', overridden: false, secret_like: false },
      { path: 'zentao.account', label: '账号', hint: '', type: 'secret', enum: null,
        min: null, max: null, step: null, value: '', default: '',
        overridden: false, secret_like: true, has_value: true },
      { path: 'zentao.password', label: '密码', hint: '明文存本地覆盖层（本机数据根）',
        type: 'secret', enum: null, min: null, max: null, step: null, value: '',
        default: '', overridden: false, secret_like: true, has_value: false },
    ] },
    { group: '设备模拟器', description: '带外控制通道', fields: [
      { path: 'emulator.endpoint', label: 'WebSocket 地址', hint: '', type: 'string',
        enum: null, min: null, max: null, step: null, value: 'ws://127.0.0.1:9300',
        default: 'ws://127.0.0.1:9300', overridden: false, secret_like: false },
    ] },
    { group: '对话模型', description: '用例解析与失败归因', fields: [
      { path: 'llm.model', label: '模型名', hint: '', type: 'string', enum: null,
        min: null, max: null, step: null, value: 'glm-4.6', default: 'glm-4.6',
        overridden: false, secret_like: false },
      { path: 'llm.max_tokens', label: '单次最大 token', hint: '', type: 'integer',
        enum: null, min: 128, max: 32768, step: null, value: 4096, default: 4096,
        overridden: false, secret_like: false },
    ] },
  ],
};

function now(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
         `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function runDetail(id: number): RunDetail {
  const base = RUNS.find((r) => r.id === id) ?? RUNS[0];
  return {
    ...base,
    env: {
      machine: 'DEMO-PC', platform: 'Windows-11 23H2', python: '3.11.9',
      driver_mode: 'auto', task_name: '每日回归', suite_id: 1,
      case_ids: ['TC-1024', 'TC-1025'],
      dsl_versions: { 'TC-1024': 3, 'TC-1025': 2 },
      test_app_version: '2.4.1', emulator_version: '1.0',
      screen: '2560x1440', dpi: HEALTH.dpi,
    },
    cases: [
      { id: 901, run_id: base.id, case_id: 'TC-1024', zentao_case_id: 1024,
        dsl_version: 3, status: 'fail', duration_ms: 41203,
        failure_signature: 'uia:click:btn_start_detect|locate_timeout',
        kb_hit_id: 7, attempts: 2,
        retry: [{ attempt: 1, status: 'fail', duration_ms: 30110,
                  failure_signature: 'uia:click:btn_start_detect|locate_timeout' },
                { attempt: 2, status: 'fail', duration_ms: 11093,
                  failure_signature: 'uia:click:btn_start_detect|locate_timeout' }],
        root_cause: { category: 'locate_timeout', source: 'rule', confidence: 0.8,
          evidence: '按钮 30s 未出现；上一次出现是在 14:02', needs_review: false,
          suggestion: '检查被测程序启动速度或放大 timeout_ms',
          similar: [{ signature: 'uia:click:btn_start_detect|locate_timeout',
                      entry_id: 7, category: 'locate_timeout', hits: 4,
                      confidence: 0.9, suggestion: '预热要等 3s 后再找按钮' }] },
        started_at: base.started_at },
      { id: 902, run_id: base.id, case_id: 'TC-1025', zentao_case_id: 1025,
        dsl_version: 2, status: 'pass', duration_ms: 18322,
        failure_signature: null, kb_hit_id: null, started_at: base.started_at },
      { id: 903, run_id: base.id, case_id: 'TC-1031', zentao_case_id: 1031,
        dsl_version: 1, status: 'blocked', duration_ms: 2102,
        failure_signature: 'device:load_scene:normal_a|err',
        kb_hit_id: 4, started_at: base.started_at,
        root_cause: { category: 'device_error', source: 'kb_exact', confidence: 0.95,
          evidence: '命中已知失败 entry#4', needs_review: false,
          suggestion: '确认模拟器场景 normal_a 已导入', similar: [] } },
    ],
    stability: {
      'TC-1024': { runs: 10, pass: 6, fail: 4, blocked: 0, skip: 0, pass_rate: 0.6,
        recent: ['fail', 'fail', 'pass', 'pass', 'fail', 'pass', 'pass', 'fail', 'pass', 'pass'],
        flips: 5, retried: 1, streak: { status: 'fail', len: 2 },
        last_status: 'fail', last_run_id: base.id, level: 'flaky' },
      'TC-1025': { runs: 10, pass: 10, fail: 0, blocked: 0, skip: 0, pass_rate: 1,
        recent: ['pass', 'pass', 'pass', 'pass', 'pass', 'pass', 'pass', 'pass', 'pass', 'pass'],
        flips: 0, streak: { status: 'pass', len: 10 },
        last_status: 'pass', last_run_id: base.id, level: 'stable' },
    },
  } as RunDetail;
}

interface MockJob {
  id: string; method: string; status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';
  progress: Record<string, unknown>; result?: unknown; error?: string;
  cancelled?: boolean;
}
const mockJobs: MockJob[] = [];
async function mockParseBatch(ids: string[], appTarget?: unknown) {
  const result = { total: ids.length, parsed: 0, items: [] as unknown[], failed: [] as { case_id: string; error: string }[] };
  for (const id of Array.from(new Set(ids))) {
    try { result.items.push(await rpc('case.parse', { caseId: id, appTarget })); result.parsed++; }
    catch (e) { result.failed.push({ case_id: id, error: String(e) }); }
  }
  return result;
}
async function runMockJob(job: MockJob, params: Record<string, unknown>): Promise<void> {
  job.status = 'running';
  try {
    if (job.method === 'case.parse') {
      const ids = params.caseIds as string[] | undefined
        ?? (params.allStale ? CASES.filter(c => c.status !== 'rejected' && (!c.steps.length || c.sync_status !== 'synced')).map(c => c.case_id) : [String(params.caseId)]);
      const result = { total: ids.length, parsed: 0, items: [] as unknown[], failed: [] as { case_id: string; error: string }[] };
      job.result = result;
      for (const id of Array.from(new Set(ids))) {
        if (job.cancelled) break;
        job.progress = { case_id: id, total: ids.length, completed: result.parsed + result.failed.length };
        await new Promise(resolve => setTimeout(resolve, 500));
        if (job.cancelled) break;
        try { result.items.push(await rpc('case.parse', { caseId: id, appTarget: params.appTarget })); result.parsed++; }
        catch (e) { result.failed.push({ case_id: id, error: String(e) }); }
        job.progress.completed = result.parsed + result.failed.length;
      }
    } else if (job.method === 'change.svn.generate') {
      job.progress = { phase: 'snapshot', changed: 3, working: 0 };
      await new Promise(resolve => setTimeout(resolve, 350));
      job.progress = { phase: 'agent_tool', tool: 'tp_svn_diff', tool_calls: 1, events: 4 };
      await new Promise(resolve => setTimeout(resolve, 350));
      job.progress = { phase: 'agent_tool', tool: 'tp_grep', tool_calls: 2, events: 9 };
      await new Promise(resolve => setTimeout(resolve, 350));
      const ids = ['SVN-WC-START-01', 'SVN-WC-RESULT-02'];
      const titles = ['开始检测状态机变更回归', '结果计算边界与异常传播'];
      ids.forEach((id, index) => {
        if (CASES.some(c => c.case_id === id)) return;
        CASES.push({
          case_id: id, zentao_case_id: null, title: titles[index],
          version: 0, status: 'draft', sync_status: 'synced',
          steps: params.parse ? structuredClone(CASES[0].steps) : [],
          updated_at: now(), last_synced_at: now(), module_id: 0, module_name: '',
          source_text: index === 0
            ? '1. 配置样本并启动检测 [期望: 状态进入检测中]\n2. 等待完成 [期望: 状态为已完成]'
            : '1. 输入边界样本 [期望: 结果计算正确]\n2. 模拟异常 [期望: 明确提示且可恢复]',
        });
      });
      const parse = params.parse ? {
        total: ids.length, parsed: ids.length,
        items: ids.map(id => ({ case_id: id, step_count: 3, uncertain_steps: [] })),
        failed: [],
      } : {};
      job.result = {
        source: 'svn_agent',
        summary: '检测状态机与结果计算逻辑发生修改，需要覆盖直接行为、异常路径与级联影响。',
        impacted_modules: ['检测流程', '结果计算'],
        risks: ['状态切换时序变化', '异常传播影响结果展示'],
        snapshot: await rpc('change.svn.preview', params),
        cases: ids.map((id, index) => ({
          case_id: id, title: titles[index], priority: index === 0 ? 'P0' : 'P1',
          category: index === 0 ? 'regression' : 'boundary',
          source_files: index === 0 ? ['src/RunController.cpp'] : ['src/ResultCalculator.cpp'],
          related_case_ids: index === 0 ? ['TC-1024'] : [],
        })),
        created: ids.length, parse,
        agent: { runtime: 'pi', tool_calls: 4, elapsed_ms: 1260 },
      };
    } else {
      job.result = await rpc(job.method, params);
    }
    job.status = job.cancelled ? 'cancelled' : 'completed';
    if (job.cancelled) job.error = '任务已取消；已完成草稿保留';
  } catch (e) { job.status = 'failed'; job.error = String(e); }
}


const AUTO_PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABh6FO1AAAAABJRU5ErkJggg==';
const AUTO_WINDOWS = [
  { title: '仪器控制软件 - Demo', handle: 1001, pid: 5522, rect: [120, 80, 1280, 800] },
  { title: 'TestPilot Device Simulator', handle: 1002, pid: 6688, rect: [1420, 120, 900, 720] },
];
let autoSnapshot = 0;
let autoCaseId = 'TC-EXPLORE';
let autoTitle = '自动化探索路径';
let autoAttached = false;
let autoDeviceScene = 'normal_sample_a';
let autoSteps: any[] = [];

function autoDeviceValue(path: string): unknown {
  if (path === '/state/run') return 'completed';
  if (path === '/state/scene') return autoDeviceScene;
  if (path === '/state/summary') return { run: 'completed', scene: autoDeviceScene };
  return null;
}
let autoElements: any[] = [
  { element_index: 1, depth: 0, parent_index: null, name: '仪器控制软件', automation_id: 'mainWindow',
    control_type: 'Window', class_name: 'QWindow', framework_id: 'Qt', value: '',
    rect: [0, 0, 1280, 800], visible: true, enabled: true, actions: ['click'],
    stable_key: 'window.mainWindow', selector_score: 0.98, selector_quality: 'high',
    selector_reason: 'AutomationId 唯一',
    selector: { type: 'uia', automation_id: 'mainWindow', control_type: 'Window' } },
  { element_index: 2, depth: 1, parent_index: 1, name: '样本号', automation_id: 'sampleId',
    control_type: 'Edit', class_name: 'QLineEdit', framework_id: 'Qt', value: 'S001',
    rect: [80, 120, 240, 34], visible: true, enabled: true,
    actions: ['click', 'input', 'set_value'], stable_key: 'edit.sampleId',
    selector_score: 0.98, selector_quality: 'high', selector_reason: 'AutomationId 唯一',
    selector: { type: 'uia', automation_id: 'sampleId', control_type: 'Edit' } },
  { element_index: 3, depth: 1, parent_index: 1, name: '开始检测', automation_id: 'startButton',
    control_type: 'Button', class_name: 'QPushButton', framework_id: 'Qt', value: '',
    rect: [80, 180, 130, 38], visible: true, enabled: true, actions: ['click'],
    stable_key: 'button.startButton', selector_score: 0.98, selector_quality: 'high',
    selector_reason: 'AutomationId 唯一',
    selector: { type: 'uia', automation_id: 'startButton', control_type: 'Button' } },
  { element_index: 4, depth: 1, parent_index: 1, name: '自动保存', automation_id: 'autoSave',
    control_type: 'CheckBox', class_name: 'QCheckBox', framework_id: 'Qt', value: 'false',
    rect: [80, 240, 120, 30], visible: true, enabled: true, actions: ['click', 'toggle'],
    stable_key: 'checkbox.autoSave', selector_score: 0.98, selector_quality: 'high',
    selector_reason: 'AutomationId 唯一',
    selector: { type: 'uia', automation_id: 'autoSave', control_type: 'CheckBox' } },
];

function autoTarget(index: number): any {
  return autoElements.find((e) => e.element_index === index);
}

interface AutoSessionState {
  snapshot: number;
  caseId: string;
  title: string;
  attached: boolean;
  deviceScene: string;
  steps: any[];
  elements: any[];
}

const AUTO_ELEMENT_SEED = structuredClone(autoElements);
const autoSessionStates = new Map<string, AutoSessionState>();
let activeAutoSessionId = 'default';

function validateAutoSessionId(raw: unknown): string {
  const value = String(raw ?? 'default').trim();
  if (!value || value.length > 64 || !/^[A-Za-z0-9._-]+$/.test(value))
    throw new Error('sessionId 只允许 1..64 位字母、数字、点、下划线和连字符');
  return value;
}

function saveActiveAutoSession(): void {
  if (!activeAutoSessionId) return;
  autoSessionStates.set(activeAutoSessionId, {
    snapshot: autoSnapshot,
    caseId: autoCaseId,
    title: autoTitle,
    attached: autoAttached,
    deviceScene: autoDeviceScene,
    steps: structuredClone(autoSteps),
    elements: structuredClone(autoElements),
  });
}

function loadAutoSession(sessionId: string): void {
  const saved = autoSessionStates.get(sessionId);
  if (saved) {
    autoSnapshot = saved.snapshot;
    autoCaseId = saved.caseId;
    autoTitle = saved.title;
    autoAttached = saved.attached;
    autoDeviceScene = saved.deviceScene;
    autoSteps = structuredClone(saved.steps);
    autoElements = structuredClone(saved.elements);
    return;
  }
  autoSnapshot = 0;
  autoCaseId = 'TC-EXPLORE';
  autoTitle = '自动化探索路径';
  autoAttached = false;
  autoDeviceScene = 'normal_sample_a';
  autoSteps = [];
  autoElements = structuredClone(AUTO_ELEMENT_SEED);
}

function activateAutoSession(raw: unknown): string {
  const sessionId = validateAutoSessionId(raw);
  if (sessionId === activeAutoSessionId) return sessionId;
  saveActiveAutoSession();
  activeAutoSessionId = sessionId;
  loadAutoSession(sessionId);
  return sessionId;
}

function closeAutoSession(raw: unknown): boolean {
  const sessionId = validateAutoSessionId(raw);
  saveActiveAutoSession();
  const existed = autoSessionStates.delete(sessionId)
    || (activeAutoSessionId === sessionId);
  if (activeAutoSessionId === sessionId) {
    activeAutoSessionId = '';
    loadAutoSession('default');
  }
  return existed;
}

function listAutoSessions(): Record<string, unknown>[] {
  saveActiveAutoSession();
  return Array.from(autoSessionStates.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([sessionId, state]) => ({
      session_id: sessionId,
      case_id: state.caseId,
      title: state.title,
      history_count: state.steps.length,
      snapshot_id: state.snapshot,
    }));
}


function recordAuto(tool: string, args: Record<string, unknown>, ok: boolean,
                    result?: Record<string, unknown>, error?: string): void {
  autoSteps.push({
    index: autoSteps.length + 1, tool, args: structuredClone(args), ok,
    ms: Math.max(1, Math.round(4 + Math.random() * 28)),
    driver: String(args.driver ?? (tool.startsWith('ui.') || tool === 'assert.ui' ? 'uia' : '')) || null,
    result, error,
  });
}

function autoObservation(elements = autoElements): Record<string, unknown> {
  autoSnapshot += 1;
  return {
    snapshot_id: autoSnapshot, include_ax: true, include_screenshot: true,
    window_rect: [120, 80, 1280, 800], window_title: '仪器控制软件 - Demo',
    window_handle: 1001, screenshot_rect: [120, 80, 1280, 800],
    screenshot_bytes: 70, png_base64: AUTO_PNG,
    elements: structuredClone(elements), truncated: false, max_nodes: 500, max_depth: 16,
  };
}

function autoDraft(): Record<string, unknown> {
  const steps: any[] = [];
  for (const item of autoSteps) {
    if (!item.ok) continue;
    const target = item.result?.target as Record<string, unknown> | undefined;
    const selector = target?.selector as Record<string, unknown> | undefined;
    if (item.tool === 'app.attach') {
      steps.push({ id: 's' + (steps.length + 1), action: 'app.launch',
        args: { title_re: item.args.title_re }, source_text: '[探索] 附加被测窗口' });
    } else if (item.tool === 'ui.click' && selector) {
      const visual = item.result?.driver === 'vision' || item.args.driver === 'vision';
      steps.push({ id: 's' + (steps.length + 1), action: visual ? 'vision.click' : 'ui.click',
        driver: visual ? 'vision_only' : undefined,
        target: { key: target?.stable_key ?? 'explore.target', primary: selector },
        verify: item.args.verify_diff ? {
          type: 'screenshot_diff',
          expect: String(item.args.diff_threshold ?? 0.005),
          timeout_ms: 5000,
        } : undefined,
        source_text: '[探索] 点击 ' + (target?.name ?? target?.desc ?? 'target') });
    } else if (item.tool === 'ui.right_click' && selector) {
      const visual = item.result?.driver === 'vision' || item.args.driver === 'vision';
      steps.push({ id: 's' + (steps.length + 1), action: 'ui.right_click',
        driver: visual ? 'vision_only' : undefined,
        target: { key: target?.stable_key ?? 'explore.target', primary: selector },
        verify: item.args.verify_diff ? {
          type: 'screenshot_diff',
          expect: String(item.args.diff_threshold ?? 0.005),
          timeout_ms: 5000,
        } : undefined,
        source_text: '[探索] 右键 ' + (target?.name ?? target?.desc ?? 'target') });
    } else if ((item.tool === 'ui.input' || item.tool === 'ui.set_value') && selector) {
      const value = item.tool === 'ui.input' ? item.args.text : item.args.value;
      const visual = item.result?.driver === 'vision' || item.args.driver === 'vision';
      steps.push({ id: 's' + (steps.length + 1), action: 'ui.input',
        driver: visual ? 'vision_only' : undefined,
        target: { key: target?.stable_key ?? 'explore.target', primary: selector },
        value: String(value ?? ''), args: visual ? {} : { clear: true },
        verify: item.args.verify_diff ? {
          type: 'screenshot_diff',
          expect: String(item.args.diff_threshold ?? 0.005),
          timeout_ms: 5000,
        } : undefined,
        source_text: '[探索] 输入 ' + String(value ?? '') });
    } else if (item.tool === 'ui.toggle' && selector) {
      steps.push({ id: 's' + (steps.length + 1), action: 'ui.toggle',
        target: { key: target?.stable_key ?? 'explore.target', primary: selector },
        value: item.args.checked ? 'true' : 'false',
        verify: item.args.verify_diff ? {
          type: 'screenshot_diff',
          expect: String(item.args.diff_threshold ?? 0.005),
          timeout_ms: 5000,
        } : undefined,
        source_text: '[探索] 设置开关' });
    } else if ((item.tool === 'ui.scroll' || item.tool === 'ui.drag') && selector) {
      const visual = item.result?.driver === 'vision' || item.args.driver === 'vision';
      const args = item.tool === 'ui.scroll'
        ? { direction: item.args.direction, amount: item.args.amount ?? 3 }
        : { dx: item.args.dx ?? 0, dy: item.args.dy ?? 0,
            duration_ms: item.args.duration_ms ?? 300 };
      steps.push({ id: 's' + (steps.length + 1), action: item.tool,
        driver: visual ? 'vision_only' : undefined,
        target: { key: target?.stable_key ?? 'explore.target', primary: selector },
        args,
        verify: item.args.verify_diff ? {
          type: 'screenshot_diff',
          expect: String(item.args.diff_threshold ?? 0.005),
          timeout_ms: 5000,
        } : undefined,
        source_text: item.tool === 'ui.scroll' ? '[探索] 滚动元素' : '[探索] 拖拽元素' });
    } else if (item.tool === 'assert.ui' && selector) {
      steps.push({ id: 's' + (steps.length + 1), action: 'assert.ui',
        target: { key: target?.stable_key ?? 'explore.target', primary: selector },
        verify: { type: 'text', expect: String(item.args.expect ?? ''),
                  op: String(item.args.op ?? 'equals'), timeout_ms: 5000 },
        source_text: '[探索] 文本断言' });
    } else if (item.tool === 'assert.vision') {
      steps.push({ id: 's' + (steps.length + 1), action: 'assert.vision',
        verify: { type: 'vision', expect: String(item.args.expect ?? ''),
                  timeout_ms: Number(item.args.timeout_ms ?? 10000) },
        source_text: '[探索] 视觉断言' });
    } else if (item.tool === 'device.scene') {
      steps.push({ id: 's' + (steps.length + 1), action: 'device.scene',
        args: { scene_id: item.args.scene_id }, source_text: '[探索] 加载设备场景' });
    } else if (item.tool === 'device.call') {
      steps.push({ id: 's' + (steps.length + 1), action: 'device.call',
        args: { cmd: item.args.cmd, args: item.args.args ?? {} },
        source_text: '[探索] 执行设备指令' });
    } else if (item.tool === 'assert.state') {
      steps.push({ id: 's' + (steps.length + 1), action: 'assert.state',
        args: { path: item.args.path, expect: item.args.expect },
        verify: { type: 'state', expect: String(item.args.expect ?? ''),
                  op: String(item.args.op ?? 'equals'), timeout_ms: 5000 },
        source_text: '[探索] 设备状态断言' });
    }
  }
  return { case_id: autoCaseId, title: autoTitle, preconditions: [], steps,
           notes: '开发态 Mock：由自动化探索会话生成，需人审确认。' };
}

async function rpc(method: string, params: Record<string, unknown>): Promise<unknown> {
  switch (method) {
    case 'automation.sessions':
      return { sessions: listAutoSessions() };
    case 'automation.closeSession': {
      const sessionId = validateAutoSessionId(params.sessionId);
      return { closed: closeAutoSession(sessionId), session_id: sessionId };
    }
    case 'automation.tools':
      activateAutoSession(params.sessionId);
      return { session_id: validateAutoSessionId(params.sessionId), snapshot_id: autoSnapshot, drivers: ['uia', 'vision'], emulator: true, tools: [
        { name: 'app.list' }, { name: 'app.attach' }, { name: 'app.launch' },
        { name: 'ui.observe' }, { name: 'ui.query' }, { name: 'ui.click' },
        { name: 'ui.read' }, { name: 'ui.input' }, { name: 'ui.set_value' },
        { name: 'ui.toggle' }, { name: 'ui.keypress' }, { name: 'ui.select' },
        { name: 'ui.right_click' }, { name: 'ui.scroll' }, { name: 'ui.drag' },
        { name: 'assert.ui' }, { name: 'assert.vision' },
        { name: 'device.scene' }, { name: 'device.call' }, { name: 'device.state' },
        { name: 'assert.state' }, { name: 'screen.save_template' },
      ] };
    case 'automation.invoke': {
      activateAutoSession(params.sessionId);
      const tool = String(params.tool ?? '');
      const args = (params.args ?? {}) as Record<string, unknown>;
      let result: Record<string, unknown>;
      if (tool === 'app.list') {
        result = { windows: structuredClone(AUTO_WINDOWS), count: AUTO_WINDOWS.length };
      } else if (tool === 'app.attach' || tool === 'app.launch') {
        autoAttached = true;
        autoSnapshot += 1;
        result = { window_rect: [120, 80, 1280, 800],
                   mode: tool === 'app.attach' ? 'attach' : 'launch',
                   title_re: args.title_re };
      } else if (tool === 'ui.observe') {
        if (!autoAttached) throw new Error('尚未附加被测窗口');
        result = autoObservation();
      } else if (tool === 'ui.query') {
        if (!autoAttached) throw new Error('尚未附加被测窗口');
        autoSnapshot += 1;
        const q = String(args.text ?? '').toLowerCase();
        const ct = String(args.control_type ?? '').toLowerCase();
        const matches = autoElements.filter((e) =>
          (!q || String(e.name + ' ' + e.value + ' ' + e.automation_id).toLowerCase().includes(q)) &&
          (!ct || String(e.control_type).toLowerCase() === ct));
        result = { snapshot_id: autoSnapshot, matches: structuredClone(matches),
                   truncated: false, visited_nodes: autoElements.length, limit: 100 };
      } else if ((tool === 'ui.locate' || tool === 'ui.click' || tool === 'ui.right_click'
                    || tool === 'ui.input' || tool === 'ui.scroll' || tool === 'ui.drag')
                 && args.driver === 'vision') {
        const desc = String(args.desc ?? '');
        const template = String(args.template ?? '');
        if (!desc && !template) throw new Error('视觉定位需要 desc 或 template');
        const rect = [650, 180, 180, 52];
        const region = Array.isArray(args.region) ? args.region.map(Number) : undefined;
        const selector = template
          ? { type: 'template', template, region }
          : { type: 'visual', desc, region };
        const target = {
          desc, template, stable_key: 'vision.' + (desc || template || 'target'),
          selector,
        };
        result = { rect, driver: 'vision', confidence: 0.92, target, ms: 26 };
        if (tool === 'ui.input') result.read_back = String(args.text ?? '');
        if (tool === 'ui.scroll') {
          result.direction = args.direction;
          result.amount = args.amount;
        }
        if (tool === 'ui.drag') {
          result.dx = args.dx;
          result.dy = args.dy;
          result.duration_ms = args.duration_ms;
        }
        if (args.verify_diff) {
          result.diff_verified = true;
          result.diff_threshold = Number(args.diff_threshold ?? 0.005);
          result.diff_detail = '开发态模拟：变化像素比 0.12';
        }
      } else if (tool === 'screen.save_template') {
        const region = Array.isArray(args.region) ? args.region.map(Number) : [];
        if (region.length !== 4 || region[2] <= 0 || region[3] <= 0)
          throw new Error('region 必须是 [x,y,w,h]');
        const base = String(args.name ?? 'visual_template')
          .replace(/[^\w\u4e00-\u9fff.-]+/g, '_')
          .replace(/^\.+|\.+$/g, '') || 'visual_template';
        const template = base.toLowerCase().endsWith('.png') ? base : base + '.png';
        result = { template, path: 'data/templates/' + template, region,
                   width: region[2], height: region[3], bytes: 1024 };
      } else if (tool === 'assert.vision') {
        const expect = String(args.expect ?? '');
        if (!expect) throw new Error('视觉断言缺少期望描述');
        result = { expect, status: 'pass', detail: '开发态模拟：视觉语义匹配',
                   confidence: 0.93, screenshot_bytes: 70,
                   screenshot_rect: [120, 80, 1280, 800] };
      } else if (tool === 'device.scene') {
        autoDeviceScene = String(args.scene_id ?? '');
        result = { scene_id: autoDeviceScene, ok: true };
      } else if (tool === 'device.state') {
        const path = String(args.path ?? '/');
        result = { path, state: autoDeviceValue(path) };
      } else if (tool === 'device.call') {
        const cmdArgs = (args.args ?? {}) as Record<string, unknown>;
        result = { ok: true, data: { cmd: args.cmd, args: cmdArgs,
          state: args.cmd === 'query_state' ? autoDeviceValue(String(cmdArgs.path ?? '/')) : null },
          elapsed_ms: 7 };
      } else if (tool === 'assert.state') {
        const path = String(args.path ?? '/');
        const actual = autoDeviceValue(path);
        const expect = String(args.expect ?? '');
        const op = String(args.op ?? 'equals');
        const matched = op === 'regex'
          ? new RegExp(expect).test(String(actual))
          : op === 'contains'
            ? String(actual).includes(expect)
            : String(actual) === expect;
        if (!matched) throw new Error('状态断言失败');
        result = { path, expect, actual, op, matched: true };
      } else {
        const el = autoTarget(Number(args.element_index));
        if (!el) throw new Error('element_index 已失效，请重新观察');
        if (args.snapshot_id != null && Number(args.snapshot_id) !== autoSnapshot)
          throw new Error('snapshot_id 已过期，请重新观察');
        const target = { name: el.name, automation_id: el.automation_id,
          control_type: el.control_type, stable_key: el.stable_key, selector: el.selector };
        if (tool === 'ui.input' || tool === 'ui.set_value') {
          el.value = String(tool === 'ui.input' ? args.text ?? '' : args.value ?? '');
        } else if (tool === 'ui.toggle') {
          el.value = args.checked ? 'true' : 'false';
        } else if (tool === 'assert.ui') {
          const expect = String(args.expect ?? '');
          const op = String(args.op ?? 'equals');
          const matched = op === 'contains' ? String(el.value).includes(expect)
            : op === 'regex' ? new RegExp(expect).test(String(el.value))
            : String(el.value) === expect;
          if (!matched) throw new Error('UI 文本断言失败');
        }
        result = { rect: el.rect, driver: String(args.driver ?? 'uia'), target,
                   element_index: el.element_index, snapshot_id: autoSnapshot,
                   direction: tool === 'ui.scroll' ? args.direction : undefined,
                   amount: tool === 'ui.scroll' ? args.amount : undefined,
                   dx: tool === 'ui.drag' ? args.dx : undefined,
                   dy: tool === 'ui.drag' ? args.dy : undefined,
                   duration_ms: tool === 'ui.drag' ? args.duration_ms : undefined,
                   value: el.value, read_back: el.value,
                   expect: tool === 'assert.ui' ? args.expect : undefined,
                   matched: tool === 'assert.ui' ? true : undefined,
                   verified: tool === 'ui.set_value' ? true : undefined,
                   actual: tool === 'ui.set_value' || tool === 'assert.ui' ? el.value : undefined,
                   diff_verified: args.verify_diff ? true : undefined,
                   diff_threshold: args.verify_diff ? Number(args.diff_threshold ?? 0.005) : undefined,
                   diff_detail: args.verify_diff ? '开发态模拟：变化像素比 0.12' : undefined };
      }
      recordAuto(tool, args, true, result);
      return { tool, result, history_count: autoSteps.length };
    }
    case 'automation.history':
      activateAutoSession(params.sessionId);
      return { session: { case_id: autoCaseId, title: autoTitle, notes: '',
                          preconditions: [], steps: structuredClone(autoSteps) },
               snapshot_id: autoSnapshot };
    case 'automation.draft': {
      activateAutoSession(params.sessionId);
      if (params.caseId) autoCaseId = String(params.caseId);
      if (params.title) autoTitle = String(params.title);
      const item = autoDraft();
      return { case: item, text: JSON.stringify(item, null, 2),
               history_count: autoSteps.length };
    }
    case 'automation.removeStep': {
      activateAutoSession(params.sessionId);
      const index = Number(params.index);
      const pos = autoSteps.findIndex((step) => step.index === index);
      if (pos < 0) throw new Error('探索步骤不存在');
      const [removed] = autoSteps.splice(pos, 1);
      autoSteps.forEach((step, i) => { step.index = i + 1; });
      return { removed, history_count: autoSteps.length };
    }
    case 'automation.updateStep': {
      activateAutoSession(params.sessionId);
      const index = Number(params.index);
      const step = autoSteps.find((item) => item.index === index);
      if (!step) throw new Error('探索步骤不存在');
      const args = params.args;
      if (!args || typeof args !== 'object' || Array.isArray(args))
        throw new Error('args 必须是对象');
      step.args = structuredClone(args as Record<string, unknown>);
      return { step: structuredClone(step), history_count: autoSteps.length };
    }
    case 'automation.rerunStep': {
      const sessionId = activateAutoSession(params.sessionId);
      const index = Number(params.index);
      const original = autoSteps.find((item) => item.index === index);
      if (!original) throw new Error('探索步骤不存在');
      const supplied = params.args;
      if (supplied != null && (typeof supplied !== 'object' || Array.isArray(supplied)))
        throw new Error('args 必须是对象');
      const effective = structuredClone(
        (supplied ?? original.args) as Record<string, unknown>,
      );
      const target = original.result?.target as Record<string, unknown> | undefined;
      const selector = target?.selector as Record<string, unknown> | undefined;
      if (effective.element_index != null && selector) {
        delete effective.element_index;
        delete effective.snapshot_id;
        Object.assign(effective, selector);
        if (!effective.key && target?.stable_key) effective.key = target.stable_key;
      }
      const rerun = await rpc('automation.invoke', {
        sessionId,
        tool: original.tool,
        args: effective,
      }) as { result: Record<string, unknown>; history_count: number };
      if (autoSteps.length) autoSteps[autoSteps.length - 1].rerun_of = index;
      return {
        rerun_of: index,
        result: rerun.result,
        history_count: rerun.history_count,
      };
    }
    case 'automation.moveStep': {
      activateAutoSession(params.sessionId);
      const index = Number(params.index);
      const toIndex = Math.max(1, Math.min(Number(params.toIndex), autoSteps.length));
      const pos = autoSteps.findIndex((item) => item.index === index);
      if (pos < 0) throw new Error('探索步骤不存在');
      const [step] = autoSteps.splice(pos, 1);
      autoSteps.splice(toIndex - 1, 0, step);
      autoSteps.forEach((item, i) => { item.index = i + 1; });
      return { steps: structuredClone(autoSteps), history_count: autoSteps.length };
    }
    case 'automation.restore': {
      activateAutoSession(params.sessionId);
      const session = params.session as Record<string, unknown> | undefined;
      if (!session || typeof session !== 'object' || Array.isArray(session))
        throw new Error('session 必须是对象');
      const steps = session.steps;
      if (!Array.isArray(steps)) throw new Error('session.steps 必须是数组');
      autoCaseId = String(session.case_id ?? 'TC-EXPLORE');
      autoTitle = String(session.title ?? '自动化探索路径');
      autoSteps = structuredClone(steps) as any[];
      autoSteps.forEach((item, i) => { item.index = i + 1; });
      return {
        session: { ...structuredClone(session), case_id: autoCaseId,
                   title: autoTitle, steps: structuredClone(autoSteps) },
        history_count: autoSteps.length,
        snapshot_id: autoSnapshot,
      };
    }
    case 'automation.reset':
      activateAutoSession(params.sessionId);
      autoSteps = []; autoSnapshot += 1; autoAttached = false;
      autoCaseId = String(params.caseId ?? 'TC-EXPLORE');
      autoTitle = String(params.title ?? '自动化探索路径');
      return { ok: true, snapshot_id: autoSnapshot };
    case 'job.start': {
      if (mockJobs.some(j => j.status === 'queued' || j.status === 'running')) throw new Error('已有用例后台任务');
      const method = String(params.method);
      if (!['case.pull', 'case.sync', 'case.parse', 'change.svn.generate'].includes(method))
        throw new Error('不支持的后台任务');
      const job: MockJob = { id: `preview-${Date.now()}`, method, status: 'queued', progress: {} };
      mockJobs.unshift(job); mockJobs.splice(100);
      setTimeout(() => void runMockJob(job, (params.params ?? {}) as Record<string, unknown>), 0);
      return structuredClone(job);
    }
    case 'job.status':
      if (params.jobId) {
        const job = mockJobs.find(j => j.id === params.jobId);
        if (!job) throw new Error('后台任务不存在');
        return structuredClone(job);
      }
      return structuredClone(mockJobs);
    case 'job.cancel': {
      const job = mockJobs.find(j => j.id === params.jobId);
      if (!job) throw new Error('后台任务不存在');
      job.cancelled = true;
      return structuredClone(job);
    }
    case 'health': return HEALTH;
    case 'run.list': return { runs: RUNS };
    case 'run.detail': return runDetail(Number(params.runId) || 42);
    case 'run.compare': return {
      base: { id: 41, task_id: 3, status: 'completed', started_at: RUNS[1].started_at,
        finished_at: RUNS[1].finished_at, total: 24, counts: {}, pass_rate: 19 / 24,
        test_app_version: '2.4.0' },
      target: { id: 42, task_id: 3, status: 'completed', started_at: RUNS[0].started_at,
        finished_at: RUNS[0].finished_at, total: 24, counts: {}, pass_rate: 21 / 24,
        test_app_version: '2.4.1' },
      pass_rate_delta: 2 / 24,
      new_failures: [{ case_id: 'TC-1042', base_status: 'pass', target_status: 'fail',
                       signature: 'assert:ui:text:lbl_status', kb_hit_id: null }],
      still_failing: [{ case_id: 'TC-1024', base_status: 'fail', target_status: 'fail',
                        base_signature: 'uia:click:btn_start_detect|locate_timeout',
                        target_signature: 'uia:click:btn_start_detect|locate_timeout',
                        same_cause: true }],
      fixed: [{ case_id: 'TC-1038', base_status: 'fail', target_status: 'pass',
                base_signature: 'vision:locate:save_btn|low_conf' }],
      added: [], removed: [],
      counts: { new_failures: 1, fixed: 1, still_failing: 1, added: 0, removed: 0,
                passed_both: 21 },
    };
    case 'run.stepTrace': return { steps: [
      { id: 1, case_exec_id: 901, step_no: 1, attempt: 1, step_id: 'launch',
        action: 'app.launch', driver: 'uia', locate_ms: 210, act_ms: 1850,
        verify_result: 'pass', verify_detail: null, screenshot_path: null,
        source_text: '启动被测程序', step_kb_hit: null, target: null,
        device_state: null, receipt: null, driver_chain: null, error: null,
        at: '2026-09-12 09:12:51' },
      { id: 2, case_exec_id: 901, step_no: 2, attempt: 1, step_id: 'click_start',
        action: 'ui.click', driver: 'vision', locate_ms: 41200, act_ms: 130,
        verify_result: null, verify_detail: null, screenshot_path: null,
        source_text: '点击「开始检测」按钮', step_kb_hit: 7,
        target: { key: 'btn.start_detect' },
        device_state: { scene: 'normal_a', status: 'idle' },
        receipt: null,
        driver_chain: [
          { driver: 'uia', ok: false, ms: 30210, error: 'locate_timeout' },
          { driver: 'vision', ok: true, ms: 41330 }],
        error: null, at: '2026-09-12 09:13:22' },
      { id: 3, case_exec_id: 901, step_no: 3, attempt: 1, step_id: 'assert_status',
        action: 'assert.ui', driver: 'uia', locate_ms: 95, act_ms: 12,
        verify_result: 'fail', verify_detail: '期望“检测中”，实际“待机”',
        screenshot_path: null, source_text: '校验状态栏显示“检测中”',
        step_kb_hit: null, target: { key: 'lbl.status' }, device_state: null,
        receipt: null, driver_chain: null,
        error: { type: 'verify_failed', expect: '检测中',
                 detail: '期望“检测中”，实际“待机”' },
        at: '2026-09-12 09:13:40' },
    ] };
    case 'task.list': return { tasks: TASKS };
    case 'task.create': {
      const name = String(params.name ?? '未命名任务');
      const caseIds = (params.caseIds as string[] | undefined) ?? [];
      const suiteId = params.suiteId != null ? Number(params.suiteId) : null;
      if (suiteId != null) {
        const s = SUITES.find((x) => x.id === suiteId);
        if (s) caseIds.splice(0, caseIds.length, ...s.case_ids);
      }
      const task = { id: taskSeq, zentao_task_id: null, name, suite_id: suiteId,
        case_ids: caseIds, preconditions: [] as string[],
        driver_config: (params.driverConfig as Record<string, unknown>) ?? { mode: 'auto' },
        sync_status: 'local', created_at: now(), runs: [] };
      TASKS.unshift(task);
      return { task_id: taskSeq++, sync_status: 'local', not_runnable: [] };
    }
    case 'task.run': {
      const rid = runSeq++;
      const run = mkRun(rid, Number(params.taskId) || 0, now(), 8, 2);
      run.status = 'running'; run.finished_at = null;
      const active = { row: run, aborted: false };
      activeMockRun = active;
      RUNS.unshift(run);
      const t = TASKS.find((x) => x.id === Number(params.taskId));
      t?.runs?.unshift(run);
      emitDomain('run.progress', { run_id: rid, phase: 'start' });
      await new Promise((resolve) => setTimeout(resolve, 2500));
      run.status = active.aborted ? 'aborted' : 'completed';
      run.finished_at = now();
      run.stats.status = run.status;
      if (active.aborted) run.stats = { ...run.stats, pass: 0, fail: 0, total: 0, skip: 1 };
      activeMockRun = null;
      emitDomain('run.progress', { run_id: rid, phase: 'finished', stats: run.stats });
      return { ...run.stats, run_id: rid, status: run.status };
    }
    case 'suite.list': return { suites: SUITES };
    case 'suite.create': {
      const name = String(params.name ?? '未命名方案');
      const caseIds = (params.caseIds as string[] | undefined) ?? [];
      const s = { id: suiteSeq, name, case_ids: caseIds, preconditions: [] as string[],
                  driver_config: { mode: 'auto' }, description: '',
                  updated_at: now(), not_runnable: [] };
      SUITES.unshift(s);
      return s;
    }
    case 'suite.update': {
      const s = SUITES.find((x) => x.id === Number(params.suiteId));
      if (s) {
        if (params.name != null) s.name = String(params.name);
        if (params.caseIds != null) s.case_ids = params.caseIds as string[];
        if (params.preconditions != null) s.preconditions = params.preconditions as string[];
        if (params.driverConfig != null) {
          s.driver_config = params.driverConfig as Record<string, unknown>;
        }
        s.updated_at = now();
      }
      return s ?? {};
    }
    case 'case.list': return { cases: CASES, sync_state: {
      source: 'zentao', scope: '1', watermark: '2026-09-12T08:30:00',
      synced_at: mockZentaoSyncedAt, detail: { scanned: 0 },
    } };
    case 'case.detail': {
      const c = CASES.find((x) => x.case_id === params.caseId) ?? CASES[0];
      return { ...c, uncertain_steps: [], step_count: c.steps.length,
               revision: JSON.stringify([c.version, c.status, c.steps]), accepted_steps: null };
    }
    case 'case.parse': {
      if (params.caseIds || params.allStale) return mockParseBatch(params.caseIds as string[]
        ?? CASES.filter(c => c.status !== 'rejected' && (!c.steps.length || c.sync_status !== 'synced')).map(c => c.case_id), params.appTarget);
      const c = CASES.find((x) => x.case_id === params.caseId);
      if (!c) throw new Error('本地不存在该用例');
      if (c.status === 'rejected') throw new Error('用例已被拒绝，请先修订草稿');
      c.sync_status = 'synced'; c.status = 'draft';
      if (c.steps.length === 0) c.steps = structuredClone(CASES.find(x => x.steps.length > 1)!.steps);
      if (params.appTarget) {
        const target = params.appTarget as Record<string, string>;
        const launch = (c.steps as Record<string, unknown>[]).find(s => s.action === 'app.launch');
        if (launch) launch.args = target;
        else c.steps.unshift({ id: 'workflow_launch', action: 'app.launch', args: target, source_text: '启动或附加被测软件' });
      }
      return { case_id: params.caseId, step_count: c.steps.length, uncertain_steps: [] };
    }
    case 'dsl.accept': {
      const c = CASES.find((x) => x.case_id === params.caseId);
      if (c) {
        if (c.sync_status !== 'synced') throw new Error('请先重新分析远端变更');
        if (!c.steps.length) throw new Error('请先生成 DSL');
        if (typeof params.text === 'string') {
          const draft = JSON.parse(params.text);
          c.steps = draft.steps ?? [];
          c.title = draft.title ?? c.title;
        }
        c.status = 'active'; c.version = Math.max(1, c.version) + (c.version > 0 ? 1 : 0);
      }
      return { case_id: params.caseId, version: c?.version ?? 1, status: 'active',
               uncertain_steps: [] };
    }
    case 'dsl.reject': {
      const c = CASES.find((x) => x.case_id === params.caseId);
      if (c) c.status = 'rejected';
      return { case_id: params.caseId, status: 'rejected' };
    }
    case 'task.abort':
      if (activeMockRun) activeMockRun.aborted = true;
      return { ok: true, stopping: activeMockRun !== null };
    case 'case.pull': {
      // 模拟拉取 2 条新用例（含分组），供浏览器自动化验证增量入库
      const ids = (params.caseIds as number[] | undefined) ?? [];
      const created: MockCase[] = [];
      for (const zid of ids.slice(0, 5)) {
        const cid = `TC-${zid}`;
        if (CASES.some((c) => c.case_id === cid)) continue;
        const c: MockCase = {
          case_id: cid, zentao_case_id: zid, title: `拉取的用例 ${zid}`,
          version: 0, status: 'draft', sync_status: 'synced', steps: [],
          updated_at: now(), last_synced_at: now(),
          module_id: 5, module_name: '登录模块',
          source_text: '1. 点击开始检测按钮\n2. 检查状态栏显示检测完成' };
        CASES.push(c);
        created.push(c);
      }
      const parse = params.parse ? await mockParseBatch(ids.map(id => `TC-${id}`)) : undefined;
      return { new: created.length, skipped: ids.length - created.length,
               cases: created, parse };
    }
    case 'case.sync': {
      mockZentaoSyncedAt = now();
      const full = !!params.full;
      const n = full ? CASES.filter(c => c.zentao_case_id != null).length : 0;
      if (full) {
        for (const item of CASES.filter(c => c.zentao_case_id != null)) {
          item.last_synced_at = mockZentaoSyncedAt;
        }
      }
      const parse = params.parse ? await rpc('case.parse', { allStale: true }) : undefined;
      return {
        scanned: n, unchanged: n, new: 0, stale: 0, conflict: 0, parse,
        since: full ? null : '2026-09-12T08:30:00', full,
        sync_state: { source: 'zentao', scope: '1',
          watermark: '2026-09-12T08:30:00', synced_at: mockZentaoSyncedAt,
          detail: { scanned: n, full } },
      };
    }
    case 'change.svn.preview':
      return {
        working_copy: String(params.workingCopy || 'D:/work/Analyzer'),
        from_revision: params.fromRevision ?? null,
        to_revision: params.toRevision ?? null,
        info: {
          revision: '1528',
          url: 'https://svn.example.com/product/trunk',
          repository_root: 'https://svn.example.com/product',
          repository_uuid: 'demo-uuid',
        },
        changed: [
          { status: 'M', path: 'src/RunController.cpp' },
          { status: 'M', path: 'src/ResultCalculator.cpp' },
          { status: 'M', path: 'ui/MainWindow.cpp' },
        ],
        working_status: [],
      };
    case 'kb.caseCtxSave': {
      const cid = String(params.caseId);
      const found = KB_CTX.find((x) => x.case_id === cid);
      const payload = {
        scene: params.scene != null ? String(params.scene) : undefined,
        pitfalls: params.pitfalls != null
          ? String(params.pitfalls).split('\n').filter(Boolean) : undefined,
        notes: params.notes != null ? String(params.notes) : undefined,
      };
      if (found) Object.assign(found.payload, payload);
      else KB_CTX.push({ id: KB_CTX.length + 1, case_id: cid,
                         signature: `ctx:${cid}`, updated_at: now(),
                         case_title: CASES.find((c) => c.case_id === cid)?.title ?? null,
                         zentao_case_id: null,
                         payload: { ...payload, scene: payload.scene ?? '',
                                    pitfalls: payload.pitfalls ?? [],
                                    notes: payload.notes ?? '' } });
      return { case_id: cid };
    }
    case 'kb.caseCtxDelete': {
      const i = KB_CTX.findIndex((x) => x.case_id === String(params.caseId));
      if (i >= 0) KB_CTX.splice(i, 1);
      return { case_id: params.caseId };
    }
    case 'config.apply': {
      const values = (params.values ?? {}) as Record<string, unknown>;
      const out = { path: CONFIG.path, written: 0, cleared: 0,
                    changed: [] as unknown[], overridden: 0, restart_required: true };
      for (const g of CONFIG.groups) {
        for (const f of g.fields) {
          const v = values[f.path];
          if (v === undefined) continue;
          if (String(v) === String(f.value)) continue;
          f.value = v as string | number | boolean;
          f.overridden = true;
          out.written += 1;
          out.changed.push({ path: f.path, value: v, cleared: false });
        }
      }
      for (const g of CONFIG.groups) {
        for (const f of g.fields) if (f.overridden) out.overridden += 1;
      }
      return out;
    }
    case 'config.reset': {
      for (const g of CONFIG.groups) {
        for (const f of g.fields) { f.value = f.default; f.overridden = false; }
      }
      return { path: CONFIG.path, written: 0, cleared: 1, changed: [],
               overridden: 0, restart_required: true };
    }
    case 'case.export': {
      const c = CASES.find((x) => x.case_id === params.caseId);
      return { content: JSON.stringify({ case_id: params.caseId, title: c?.title,
        steps: c?.steps ?? [], preconditions: c?.preconditions ?? [] }, null, 2) };
    }
    case 'dsl.save': {
      const c = CASES.find((x) => x.case_id === params.caseId);
      const draft = JSON.parse(String(params.text));
      if (c) { c.steps = draft.steps; c.status = 'draft'; }
      return { case_id: params.caseId, step_count: draft.steps?.length ?? 0 };
    }
    case 'kb.pending': return { entries: KB_PENDING };
    case 'kb.confirm': case 'kb.reject': {
      const id = Number(params.entryId);
      const i = KB_PENDING.findIndex((e) => e.id === id);
      if (i >= 0) KB_PENDING.splice(i, 1);
      return { entryId: id, status: method === 'kb.confirm' ? 'active' : 'rejected' };
    }
    case 'kb.setDisposition': {
      const e = KB_PENDING.find((x) => x.id === Number(params.entryId));
      if (e) e.payload.disposition = String(params.disposition);
      return { entryId: params.entryId, disposition: params.disposition };
    }
    case 'kb.caseCtxList': return { entries: KB_CTX };
    case 'kb.envCompat': return { entries: [
      { id: 3, signature: 'env:core-24', updated_at: '2026-09-12 09:31:00',
        payload: { test_app_version: '2.4.1', suite_id: 1,
                   case_ids: ['TC-1024', 'TC-1025'],
                   matrix: { 'TC-1024': { pass: 6, fail: 4, total: 10, last_status: 'fail', last_run_id: 42 },
                             'TC-1025': { pass: 10, total: 10, last_status: 'pass', last_run_id: 42 } } } },
      { id: 2, signature: 'env:core-24', updated_at: '2026-09-11 21:17:00',
        payload: { test_app_version: '2.4.0', suite_id: 1,
                   case_ids: ['TC-1024', 'TC-1025'],
                   matrix: { 'TC-1024': { pass: 6, fail: 4, total: 10, last_status: 'fail', last_run_id: 41 },
                             'TC-1025': { pass: 9, fail: 1, total: 10, last_status: 'pass', last_run_id: 41 } } } },
    ] };
    case 'config.describe': return CONFIG;
    case 'config.overrideText': return { path: CONFIG.path, exists: true, text:
      '# 由设置页写入的差异项\ndriver:\n  mode: uia_first\n' };
    case 'run.report': return { path: 'D:/demo/TestPilot/data/reports/run-42.md' };
    default: return {};
  }
}

// ---- Tauri 运行时壳（invoke/listen 的最小仿真） ----
type Cb = (payload: unknown) => void;
let cbSeq = 0;
const callbacks = new Map<number, Cb>();
const listeners = new Map<number, { event: string; handler: number }>();
let listenerSeq = 0;
function emitDomain(event: string, payload: unknown): void {
  for (const [id, listener] of listeners) {
    if (listener.event === `engine://${event}`) {
      callbacks.get(listener.handler)?.({ event: listener.event, id, payload });
    }
  }
}

function install(): void {
  const w = window as unknown as {
    __TAURI_INTERNALS__: {
      invoke: (cmd: string, args?: Record<string, unknown>) => Promise<unknown>;
      transformCallback: (cb: Cb) => number;
      metadata?: unknown;
    };
    __TAURI_EVENT_PLUGIN_INTERNALS__?: { unregisterListener: (e: string, id: number) => void };
  };
  w.__TAURI_INTERNALS__ = {
    transformCallback: (cb) => {
      cbSeq += 1;
      callbacks.set(cbSeq, cb);
      return cbSeq;
    },
    invoke: async (cmd: string, args: Record<string, unknown> = {}) => {
      switch (cmd) {
        case 'rpc_call': return rpc(String(args.method), (args.params ?? {}) as Record<string, unknown>);
        case 'engine_status': return STATUS;
        case 'engine_log': return LOG_LINES;
        case 'shell_metrics': return METRICS;
        case 'engine_restart': return { ok: true };
        case 'sync_roots': return { roots: Object.values(HEALTH.paths ?? {}) };
        case 'read_text_file':
          return '# run 42 报告（样例）\n\n- 通过 21/24\n- 失败 2 · 阻塞 1\n';
        case 'read_asset_data_url':
          // 1x1 灰色 PNG：让截图区渲染出占位块而不是报错
          return 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABh6FO1AAAAABJRU5ErkJggg==';
        case 'plugin:event|listen': {
          const id = ++listenerSeq;
          listeners.set(id, { event: String(args.event), handler: Number(args.handler) });
          return id;
        }
        case 'plugin:event|unlisten': listeners.delete(Number(args.eventId)); return null;
        default: return null;
      }
    },
  };
  // @tauri-apps/api 的 event 模块反注册监听时走这个内部通道（缺了会抛
  // "reading 'unregisterListener'"）；mock 下没有真实监听需要清理
  w.__TAURI_EVENT_PLUGIN_INTERNALS__ = { unregisterListener: () => {} };
  console.info('[devMock] 已安装 Tauri 运行时仿真（仅 npm run dev 生效）');
}

const LOG_LINES = [
  '2026-09-12 09:12:44 [info] run#42 开始: task#3 cases=24',
  '2026-09-12 09:12:51 [info] 用例开始: TC-1024',
  '2026-09-12 09:13:21 [warn] 定位失败，尝试下一驱动: driver=uia target=btn.start_detect',
  '2026-09-12 09:13:31 [err] 用例失败: TC-1024 locate_timeout（30s）',
  '2026-09-12 09:31:02 [info] run#42 结束: pass=21/24',
];

install();
