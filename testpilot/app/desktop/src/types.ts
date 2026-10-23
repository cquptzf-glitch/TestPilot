/** 引擎数据模型（与 testpilot/rpc.py 的返回体一一对应）。
 *
 *  只需描述**前端真正消费**的字段；引擎侧新增字段不会破坏这里（多余字段被忽略）。
 */

export type CaseStatus = 'draft' | 'active' | 'rejected' | 'deprecated';
export type SyncStatus = 'synced' | 'stale' | 'conflict';
export type ExecStatus = 'pass' | 'fail' | 'blocked' | 'skip' | 'error' | 'running';

export interface CaseRow {
  case_id: string;
  zentao_case_id: number | null;
  title: string;
  version: number;
  status: CaseStatus;
  sync_status: SyncStatus;
  steps: unknown[];
  updated_at?: string;
  zentao_updated_at?: string | null;
  /** 上次从禅道同步的时间（引擎写入；「增量同步」按钮据此提示上次同步时间） */
  last_synced_at?: string | null;
  /** 禅道用例分组（模块）：名称解析失败时只回 id（第十五轮） */
  module_id?: number | null;
  module_name?: string | null;
}

export interface CaseDetail extends CaseRow {
  revision?: string;
  raw?: Record<string, unknown> | null;
  preconditions?: string[];
  notes?: string;
  source_text?: string;
  uncertain_steps?: string[];
  step_count?: number;
  accepted_steps?: unknown[] | null;
}

export interface SuiteRow {
  id: number;
  name: string;
  case_ids: string[];
  preconditions: string[];
  driver_config: Record<string, unknown>;
  description: string;
  updated_at?: string;
  not_runnable?: { case_id: string; reason: string }[];
}

export interface TaskRow {
  id: number;
  zentao_task_id: number | null;
  name: string;
  suite_id: number | null;
  case_ids: string[];
  preconditions: string[];
  driver_config: Record<string, unknown>;
  sync_status: 'local' | 'synced' | 'sync_pending';
  remote_state?: {
    creation_attempted?: boolean;
    creation_uncertain?: boolean;
  };
  created_at: string;
  runs?: RunRow[];
}

export interface RunStats {
  pass?: number;
  fail?: number;
  blocked?: number;
  skip?: number;
  error?: number;
  total?: number;
  kb_hit_cases?: number;
  status?: string;
  writeback?: string;
  llm_usage?: { calls?: number; tokens?: number; ms?: number; errors?: number };
  preload_errors?: unknown[];
  report?: string;
  /** 驱动降级统计（引擎 stats.drivers，来自 step_trace 尝试链） */
  drivers?: DriverStats;
  /** env_compat 本次汇总结果（签名/累计 run 数） */
  env_compat?: { signature?: string; runs?: number; cases?: number };
  /** 失败复跑（flake 过滤）：发生复跑的用例数 / 其中复跑后通过的用例数 */
  retry_cases?: number;
  retry_passed?: number;
}

/** 驱动与降级统计（引擎 `engine/stats.py` 的产出结构，键名勿改）。 */
export interface DriverStats {
  steps?: number;
  degraded_steps?: number;
  failed_steps?: number;
  /** 降级率 = 降级步骤数 / 有驱动尝试的步骤数；null 表示无数据 */
  downgrade_rate?: number | null;
  by_driver?: Record<string, {
    attempts?: number; success?: number; fail?: number; ms?: number;
    success_rate?: number | null; avg_ms?: number;
  }>;
  /** 降级路径（如 uia→vision）：count 为出现次数，resolved 为最终成功次数 */
  transitions?: { from: string; to: string; count: number; resolved?: number }[];
}

export interface RunRow {
  id: number;
  task_id: number;
  started_at: string;
  finished_at: string | null;
  status: string;
  stats: RunStats;
}

export interface CaseExecRow {
  id: number;
  run_id: number;
  case_id: string;
  zentao_case_id: number | null;
  dsl_version: number;
  status: ExecStatus;
  duration_ms: number;
  failure_signature: string | null;
  kb_hit_id: number | null;
  root_cause: RootCause | null;
  /** 该失败签名的历史画像（仅失败用例有；按需计算不落库） */
  history?: SignatureHistory | null;
  started_at: string;
  /** 执行次数（>1 表示发生过失败复跑） */
  attempts?: number;
  /** 每次尝试的摘要（复跑才有；最终通过时首次失败的证据在这里） */
  retry?: RetryAttempt[] | null;
}

/** 一次尝试的摘要（引擎 `case_exec.retry_json` 的元素）。 */
export interface RetryAttempt {
  attempt: number;
  status: string;
  duration_ms?: number;
  failure_signature?: string | null;
  kb_hit_id?: number | null;
  root_cause?: RootCause | null;
}

/** 壳侧独立运行指标（Rust 命令 `shell_metrics`，设计 10 章）。 */
export interface ShellMetrics {
  state: string;
  restarts: number;
  rpcConnected: boolean;
  rpcCalls: number;
  rpcErrors: number;
  rpcLastMs: number;
  rpcAvgMs: number;
  /** 壳侧 JSONL 日志落盘路径（重启/状态迁移，与引擎日志分离） */
  shellLog: string;
}

/** 失败签名历史画像（引擎 `engine/stats.signature_history`，设计 9.4 快速定位）。 */
export interface SignatureHistory {
  signature: string;
  count: number;
  categories: { category: string; count: number }[];
  last: {
    run_id?: number | null;
    task_id?: number | null;
    case_exec_id?: number | null;
    case_id?: string | null;
    zentao_case_id?: number | null;
    step_no?: number | null;
    at?: string | null;
  };
  suggestion?: string;
}

export interface RootCause {
  category?: string;
  source?: string;
  confidence?: number;
  evidence?: string;
  suggestion?: string;
  needs_review?: boolean;
  /** 相似历史对照（引擎 `FailureKnowledge.as_ref()` 的结构，勿改键名）。
   *  `hits` = 该签名在知识库中的命中次数；`entry_id` 用于跳转知识库审阅。 */
  similar?: {
    signature: string;
    entry_id?: number;
    category?: string;
    suggestion?: string;
    confidence?: number;
    hits?: number;
  }[];
}

export interface StepTrace {
  status?: string;
  id: number;
  case_exec_id: number;
  step_no: number;
  /** 第几次执行（复跑用例的轨迹按此区分；查看端默认只看最后一次） */
  attempt?: number;
  step_id: string;
  action: string;
  driver: string | null;
  locate_ms: number | null;
  act_ms: number | null;
  verify_result: string | null;
  verify_detail: string | null;
  screenshot_path: string | null;
  /** before 截图（与 screenshot_path=after 分列；加列之前的旧数据为 null） */
  screenshot_before_path?: string | null;
  source_text: string | null;
  step_kb_hit: number | null;
  target: Record<string, unknown> | null;
  device_state: unknown;
  /** 设备指令回执（device.* 步骤专用；加列之前的老数据为 null） */
  receipt?: {
    step_id?: string;
    cmd?: string;
    ok?: boolean | null;
    ms?: number | null;
    error?: string;
  } | null;
  /** 驱动尝试链：降级步骤（uia→vis）按序保留每次尝试的成败与耗时 */
  driver_chain?: {
    driver: string; ok: boolean | null; ms?: number | null; error?: string | null;
  }[] | null;
  error: { type?: string; message?: string; detail?: string; expect?: string } | null;
  at: string;
}

export interface RunDetail extends RunRow {
  env: EnvSnapshot;
  cases: CaseExecRow[];
  /** 用例稳定性（跨 run 派生视图，引擎按需计算，键=case_id） */
  stability?: Record<string, StabilityEntry>;
}

/** 用例稳定性（引擎 `engine/stats.py::case_stability` 的产出结构，键名勿改）。 */
export interface StabilityEntry {
  /** 窗口内样本数（每个用例最近 DEFAULT_WINDOW 次执行） */
  runs: number;
  pass: number;
  fail: number;
  blocked: number;
  skip: number;
  other?: number;
  /** 通过率分母是全部样本：被阻塞/跳过的批次不计入通过 */
  pass_rate: number;
  /** 最近执行状态，新→旧（画条带用） */
  recent: string[];
  /** 相邻两次状态翻转次数 */
  flips: number;
  /** 窗口内"复跑后才通过"的次数（>0 时判 flaky：要跑两次才过就是不稳定的证据） */
  retried?: number;
  streak: { status: string; len: number };
  last_status: string;
  last_run_id: number;
  last_at?: string;
  /** 样本不足（<2 次）时为 unknown，不硬下结论 */
  level: 'stable' | 'flaky' | 'broken' | 'unknown';
}

/** 两次 run 对比（引擎 `engine/stats.py::compare_runs`，回归收尾用）。 */
export interface RunCompareSide {
  id: number;
  task_id: number | null;
  status: string | null;
  started_at: string | null;
  finished_at: string | null;
  total: number;
  counts: Record<string, number>;
  pass_rate: number | null;
  test_app_version?: string | null;
  downgrade_rate?: number | null;
}

export interface RunCompare {
  base: RunCompareSide;
  target: RunCompareSide;
  pass_rate_delta: number | null;
  new_failures: {
    case_id: string; base_status: string; target_status: string;
    signature: string | null; kb_hit_id: number | null;
  }[];
  fixed: {
    case_id: string; base_status: string; target_status: string;
    base_signature: string | null;
  }[];
  still_failing: {
    case_id: string; base_status: string; target_status: string;
    base_signature: string | null; target_signature: string | null;
    /** 失败签名是否一致；null = 至少一侧无签名，无法判定 */
    same_cause: boolean | null;
  }[];
  added: { case_id: string; target_status: string }[];
  removed: { case_id: string; base_status: string }[];
  counts: {
    new_failures: number; fixed: number; still_failing: number;
    added: number; removed: number; passed_both: number;
  };
}

export interface EnvSnapshot {
  machine?: string;
  platform?: string;
  python?: string;
  driver_mode?: string;
  /** 本次 run 的失败复跑策略（0=关闭）：不同策略的 run 不该直接比通过率 */
  case_retry?: number;
  task_name?: string;
  suite_id?: number | null;
  case_ids?: string[];
  dsl_versions?: Record<string, number>;
  test_app_version?: string;
  emulator_version?: string;
  screen?: string;
  dpi?: {
    aware?: boolean;
    scale?: number;
    space?: string;
    resolution?: string;
    monitors?: { index: number; rect: number[]; primary: boolean }[];
  };
}

export interface KbEntry {
  id: number;
  type: string;
  signature: string;
  confidence: number;
  updated_at: string;
  payload: {
    category?: string;
    disposition?: string;
    suggestion?: string;
    evidence?: string;
    error_type?: string;
    action?: string;
    target_key?: string;
    page_key?: string;
  };
}

/* ---------------------------------------------------------------- 知识库：用例上下文 / 环境矩阵 */

/** case_ctx（人审维护的用例上下文，设计 9.1）。 */
export interface CaseCtxEntry {
  id: number;
  case_id: string;
  signature: string;
  status?: string;
  updated_at?: string;
  case_title?: string | null;
  zentao_case_id?: number | null;
  payload: {
    scene?: string;
    pitfalls?: string[];
    notes?: string;
    zentao_case_id?: number;
    source?: string;
  };
}

/** env_compat（被测版本 × 用例集 的历史通过率矩阵）。 */
export interface EnvCompatEntry {
  id: number;
  signature: string;
  updated_at: string;
  payload: {
    test_app_version?: string;
    suite_id?: number | null;
    case_ids?: string[];
    driver_mode?: string | null;
    emulator_version?: string | null;
    updated_at?: string;
    runs?: number[];
    matrix?: Record<string, {
      pass?: number; fail?: number; other?: number; total?: number;
      last_status?: string; last_run_id?: number;
    }>;
  };
}

export interface EngineHealth {
  ok: boolean;
  db: { ok: boolean; detail: string };
  drivers: Record<string, { ok: boolean; detail: string }>;
  zentao: { configured: boolean };
  agent?: { ok: boolean; detail: string };
  svn?: { ok: boolean; detail: string };
  /** 待回写（sync_pending）任务数：导航"禅道"告警徽章的数据源 */
  sync_pending?: number;
  emulator: { connected: boolean };
  dpi?: EnvSnapshot['dpi'];
  warnings?: string[];
  paths?: { db?: string; screenshot_root?: string; report_root?: string };
}

export interface EngineStatus {
  state: 'starting' | 'running' | 'restarting' | 'stopped';
  pid: number | null;
  restarts: number;
  /** 壳的状态迁移事件可能不带该字段（消费端要保留旧值） */
  rpcConnected?: boolean;
  detail: string;
}

/* ---------------------------------------------------------------- 设置页配置 */

/** 可编辑字段（与 testpilot/config/service.py 的 EDITABLE 一一对应）。 */
export interface ConfigField {
  path: string;
  label: string;
  hint: string;
  type: 'string' | 'integer' | 'number' | 'boolean' | 'enum' | 'secret';
  enum: string[] | null;
  min: number | null;
  max: number | null;
  step: number | null;
  /** 实际生效值（默认值 + 覆盖层合并后）；secret 类型恒为空串（不回显） */
  value: string | number | boolean | null;
  /** 出厂默认值（用于「恢复默认」与"是否已改"判定） */
  default: string | number | boolean | null;
  /** 是否被用户改过（写进覆盖层） */
  overridden: boolean;
  secret_like: boolean;
  /** 仅 secret 类型：是否已设置过值（真实值永不下发前端） */
  has_value?: boolean;
}

export interface ConfigGroup {
  group: string;
  description: string;
  fields: ConfigField[];
}

export interface ConfigDescribe {
  path: string;
  exists: boolean;
  groups: ConfigGroup[];
}

export interface ConfigApplyResult {
  path: string;
  written: number;
  cleared: number;
  changed: { path: string; value: unknown; cleared: boolean }[];
  overridden: number;
  /** 引擎启动时读取配置 -> 必须重启才生效（前端据此提示，不做"看起来生效了"） */
  restart_required: boolean;
}

/* ---------------------------------------------------------------- 截图差异 */

export interface ShotDiffResult {
  /** 差异图相对 screenshot_root 的路径；null 表示算不出来（见 detail） */
  rel: string | null;
  cached: boolean;
  missing?: 'before' | 'after';
  detail?: string;
  stats: {
    ratio?: number;
    boxes?: number;
    /** -1 约定：前后尺寸不一致，无法逐像素对齐 */
    changed?: number;
    same_size?: boolean;
    size_before?: string;
    size_after?: string;
  };
}

/** 引擎事件 payload（core/events.py 领域事件原样转发）。 */
export interface StepFinishedPayload {
  run_id: number;
  case_exec_id: number;
  step_no: number;
  step_id: string;
  status: string;
  driver: string | null;
}

export interface CaseFinishedPayload {
  run_id: number;
  case_exec_id: number;
  case_id: string;
  status: string;
  duration_ms: number;
  sig: string | null;
}

export interface RunProgressPayload {
  run_id: number;
  /** start / case_done / finished（finished = 本 run 已收尾，列表可立即刷新） */
  phase?: string;
  stats?: RunStats;
}


/* ---------------------------------------------------------------- 外部来源 / SVN 变更分析 */

export interface SourceSyncState {
  source: string;
  scope: string;
  watermark?: string | null;
  synced_at: string;
  detail?: Record<string, unknown>;
}

export interface SvnChangedPath {
  status?: string;
  item?: string;
  path: string;
}

export interface SvnChangePreview {
  working_copy: string;
  from_revision?: string | null;
  to_revision?: string | null;
  info?: {
    path?: string;
    revision?: string | null;
    url?: string;
    repository_root?: string;
    repository_uuid?: string;
  };
  changed: SvnChangedPath[];
  working_status?: SvnChangedPath[];
}

export interface SvnGeneratedCase {
  case_id: string;
  title: string;
  priority: string;
  category: string;
  source_files: string[];
  related_case_ids: string[];
  reused?: boolean;
  generation_run_id?: string;
  grounding?: {
    svn_diff_verified?: boolean;
    source_files?: { path: string; basis: string[] }[];
    related_case_ids?: { case_id: string; basis: string; case_token?: string }[];
  };
}

export interface AgentToolAuditEntry {
  seq: number;
  method: string;
  params?: Record<string, unknown>;
  ok: boolean;
  elapsed_ms: number;
  result?: Record<string, unknown>;
  error?: string;
}

export interface AgentToolEvidence {
  calls?: Record<string, number>;
  successes?: Record<string, number>;
  failures?: Record<string, number>;
  read_paths?: string[];
  read_paths_truncated?: boolean;
  read_hashes?: Record<string, string>;
  content_hashes?: Record<string, string>;
  content_hashes_truncated?: boolean;
  case_ids?: string[];
  case_ids_truncated?: boolean;
  case_tokens?: Record<string, string>;
  case_tokens_truncated?: boolean;
  diff_paths?: string[];
  diff_paths_truncated?: boolean;
  full_diff_complete?: boolean;
  full_diff_truncated?: boolean;
}

export interface SvnSnapshotGuard {
  signature: string;
  mode: 'working_copy' | 'revision';
  diff_sha256: string;
  diff_bytes: number;
  unversioned_count: number;
  unversioned_entries: number;
  unversioned_bytes: number;
}

export interface SvnChangeGenerateResult {
  source: 'svn_agent';
  generation_run_id?: string;
  summary: string;
  impacted_modules: string[];
  risks: string[];
  snapshot: SvnChangePreview;
  snapshot_guard?: SvnSnapshotGuard;
  cases: SvnGeneratedCase[];
  created: number;
  reused: number;
  duplicates_dropped?: number;
  parse?: ParseBatchLike;
  agent?: {
    runtime: string;
    tool_calls?: number;
    elapsed_ms?: number;
    audit?: AgentToolAuditEntry[];
    evidence?: AgentToolEvidence;
  };
}

export interface ParseBatchLike {
  parsed?: number;
  total?: number;
  items?: { case_id: string; step_count: number; uncertain_steps?: string[] }[];
  failed?: { case_id: string; error: string }[];
}

/* ---------------------------------------------------------------- 自动化探索 */

export interface AutomationSessionSummary {
  session_id: string;
  case_id: string;
  title: string;
  history_count: number;
  snapshot_id: number;
}

export interface AutomationWindow {
  title: string;
  handle: number;
  pid: number | null;
  rect: number[] | null;
}

export interface AutomationElement {
  element_index: number;
  depth: number;
  parent_index: number | null;
  name: string;
  automation_id: string;
  control_type: string;
  class_name: string;
  framework_id: string;
  value: string;
  rect: number[];
  visible: boolean | null;
  enabled: boolean | null;
  actions: string[];
  stable_key?: string;
  selector?: Record<string, unknown> | null;
  selector_score?: number;
  selector_quality?: 'high' | 'medium' | 'low';
  selector_reason?: string;
}

export interface AutomationObservation {
  snapshot_id: number;
  include_ax?: boolean;
  include_screenshot?: boolean;
  window_rect?: number[] | null;
  window_title?: string;
  window_handle?: number;
  screenshot_rect?: number[];
  screenshot_bytes?: number;
  screenshot_transport?: 'inline' | 'asset';
  asset_path?: string;
  png_base64?: string;
  elements?: AutomationElement[];
  matches?: AutomationElement[];
  truncated?: boolean;
  visited_nodes?: number;
}

export interface AutomationHistoryStep {
  index: number;
  tool: string;
  args: Record<string, unknown>;
  ok: boolean;
  ms: number;
  driver?: string | null;
  result?: Record<string, unknown> | null;
  error?: string | null;
  rerun_of?: number | null;
}

export interface AutomationHistory {
  case_id: string;
  title: string;
  notes?: string;
  preconditions?: string[];
  steps: AutomationHistoryStep[];
}

export interface AutomationDraft {
  case: {
    case_id: string;
    title: string;
    notes?: string;
    preconditions?: string[];
    steps: unknown[];
  };
  text: string;
  history_count: number;
}
