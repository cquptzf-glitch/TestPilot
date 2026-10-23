/** Settings · 设置与自检（设计 14.3，v1.8 改版：Tab 式布局）。
 *
 *  页面结构（Tab 切换，同类设置合并进同一 Tab，每个配置 Tab 有**独立保存按钮**）：
 *  1. **执行与驱动** —— 被测程序 + 驱动路由 + UIA + 视觉（执行链路的参数都在这）；
 *  2. **模型服务** —— 对话模型（用例解析/归因）；
 *  3. **集成** —— 禅道（**含账号密码**）+ 设备模拟器；
 *  4. **知识库与日志**；
 *  5. **运行信息**（`health`：引擎进程/路径/DPI/驱动与外部依赖）；
 *  6. **高级**（覆盖层 YAML 原文 + 引擎启动开关）。
 *
 *  设计取舍：
 *  - 配置项由引擎的 `EDITABLE` 规格驱动渲染（type/enum/min/max/hint 全来自引擎），
 *    前端不硬编码字段表 —— 否则加一个配置要改两处，必然漂移；
 *  - 保存按 Tab 提交：只把**当前 Tab 里真的改了**的字段写进覆盖层，误触不扩散；
 *  - 改完**必须重启引擎**才生效，页面显式提示，不做"看起来生效了"的假象；
 *  - secret 字段（禅道账密）：密码框渲染、引擎永不回显真实值（value 恒空，
 *    只有 has_value 标记），明文存数据根覆盖层是本地凭据的既定取舍。
 */

import { useMemo, useState } from 'react';

import { call, engineRestart } from '../bridge';
import { Badge, Empty, Msg } from '../components/Bits';
import { Skeleton, pushToast } from '../components/Ui';
import { msg, useAsync, type EngineContextValue } from '../hooks';
import type { ConfigApplyResult, ConfigDescribe, ConfigField,
              ConfigGroup } from '../types';

/** 配置 Tab：groups 引用引擎 `describe.groups` 里的分组名（一一对应，改了就断言）。 */
const CONFIG_TABS = [
  { key: 'exec', title: '执行与驱动', groups: ['被测程序', '驱动路由', 'UIA 驱动', '视觉驱动'],
    desc: '被测程序、驱动路由与 UIA/视觉参数' },
  { key: 'llm', title: '模型服务', groups: ['对话模型'],
    desc: '对话模型（用例解析与失败归因）' },
  { key: 'integration', title: '集成', groups: ['禅道', 'Agent Runtime', 'SVN 变更分析', '设备模拟器'],
    desc: '禅道、Pi Agent、SVN 与设备模拟器' },
  { key: 'knowledge', title: '知识库与日志', groups: ['知识库', '日志'],
    desc: '知识库阈值与日志输出' },
] as const;

type TabKey = (typeof CONFIG_TABS)[number]['key'] | 'runtime' | 'advanced';

export function SettingsPage({ engine }: { engine: EngineContextValue }) {
  const [tab, setTab] = useState<TabKey>('exec');
  const h = engine.health;

  const configTab = CONFIG_TABS.find((t) => t.key === tab);

  return (
    <div className="main">
      <div className="page-head">
        <h2>设置</h2>
        <span className="hint">
          {configTab ? configTab.desc
            : tab === 'runtime' ? '引擎进程、路径、DPI 与外部依赖的实际状态'
            : '覆盖层 YAML 原文与引擎启动开关'}
        </span>
        <span className="spacer" />
        <button className="btn" onClick={engine.refresh}>重新自检</button>
      </div>

      <div className="segmented" role="tablist" style={{ marginBottom: 'var(--sp-4)' }}>
        {CONFIG_TABS.map((t) => (
          <button key={t.key} role="tab"
                  className={tab === t.key ? 'on' : ''}
                  onClick={() => setTab(t.key)}>{t.title}</button>
        ))}
        <button role="tab" className={tab === 'runtime' ? 'on' : ''}
                onClick={() => setTab('runtime')}>运行信息</button>
        <button role="tab" className={tab === 'advanced' ? 'on' : ''}
                onClick={() => setTab('advanced')}>高级</button>
      </div>

      {engine.healthError && <Msg kind="error">健康检查失败：{engine.healthError}</Msg>}
      {(h?.warnings?.length ?? 0) > 0 && (
        <Msg kind="warn">环境告警：{h?.warnings?.join('；')}</Msg>
      )}

      {configTab
        ? <ConfigPanel tabTitle={configTab.title}
                       groupNames={[...configTab.groups]} />
        : tab === 'runtime'
          ? <RuntimePanel engine={engine} />
          : <AdvancedPanel />}
    </div>
  );
}

/* ------------------------------------------------------------------ 高级 */

/** 覆盖层 YAML 原文。
 *
 *  为什么需要：表单只能表达 **引擎声明为可编辑** 的字段（`EDITABLE`）。
 *  当用户改了配置却没生效时，第一时间要回答的是"磁盘上到底写了什么" ——
 *  没有这块就只能去数据根目录手翻文件，而那个路径在 UI 上并不会显示。
 *
 *  这里只读展示、不提供编辑：直接改 YAML 会绕过 `EDITABLE` 的类型校验，
 *  写进非法值后引擎启动时只能整体忽略该条目（静默失效）。
 */
function AdvancedPanel() {
  const text = useAsync(
    () => call<{ path: string; exists: boolean; text: string }>('config.overrideText'), []);

  const content = text.data?.text ?? '';
  const nonComment = content.split('\n')
    .filter((l) => l.trim() && !l.trimStart().startsWith('#')).length;

  return (
    <div className="col">
      <div className="panel">
        <h3>
          配置覆盖层原文
          <span className="hint" style={{ marginLeft: 8 }}>
            表单只能编辑引擎声明为可编辑的字段；这里看磁盘上实际写了什么
          </span>
        </h3>

        {text.error && <Msg kind="error">读取失败：{text.error}</Msg>}
        {!text.data && !text.error && <Skeleton rows={4} />}

        {text.data && (
          <>
            <div className="toolbar" style={{ marginBottom: 'var(--sp-3)' }}>
              <span className="muted">文件</span>
              <span className="mono truncate" style={{ maxWidth: 520 }}
                    title={text.data.path}>{text.data.path}</span>
              <Badge status={text.data.exists ? 'synced' : 'skip'}
                     text={text.data.exists ? `已存在（${nonComment} 项有效配置）` : '尚未创建'} />
              <span className="spacer" />
              <button className="btn sm" disabled={!text.data.exists}
                      onClick={() => void navigator.clipboard.writeText(content)
                        .then(() => pushToast('pass', '已复制到剪贴板'))
                        .catch(() => pushToast('error', '复制失败（剪贴板不可用）'))}>
                复制
              </button>
              <button className="btn sm" onClick={() => void text.reload()}>重新读取</button>
            </div>

            {!text.data.exists && (
              <Msg kind="info">
                还没有覆盖层文件 —— 当前全部取出厂默认。到「配置」页改任意一项并保存即会创建。
              </Msg>
            )}

            {text.data.exists && (
              <pre className="mono pre-wrap scroll-y"
                   style={{ margin: 0, maxHeight: 460, fontSize: 'var(--fs-sm)',
                            background: 'var(--c-bg-sunken)', padding: 'var(--sp-4)',
                            borderRadius: 'var(--r-md)' }}>
                {content}
              </pre>
            )}
          </>
        )}
      </div>

      <div className="panel">
        <h3>引擎启动开关</h3>
        <dl className="kv">
          <dt>TESTPILOT_NO_OVERRIDE</dt>
          <dd>
            设为 <span className="mono">1</span> 时引擎<strong>忽略覆盖层</strong>，
            完全按出厂默认启动。
            <div className="faint">
              用途：怀疑是配置改坏了时，用它排除覆盖层因素，而不用删文件。
            </div>
          </dd>
          <dt>TESTPILOT_HOME</dt>
          <dd>
            覆盖层与数据库所在的数据根（壳启动 sidecar 时注入）。
            <div className="faint">
              手动跑 CLI 时要设成同一个值，否则读的是另一份配置。
            </div>
          </dd>
        </dl>
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- 运行信息 */

function RuntimePanel({ engine }: { engine: EngineContextValue }) {
  const health = engine.health;
  const status = engine.status;

  return (
    <div className="row split">
      <div className="left" style={{ flex: '0 0 460px' }}>
        <div className="panel">
          <h3>引擎进程（sidecar）</h3>
          {!status ? <Empty text="未取到引擎状态" /> : (
            <dl className="kv">
              <dt>状态</dt>
              <dd>
                <Badge status={status.state === 'running' ? 'pass'
                  : status.state === 'stopped' ? 'fail' : 'blocked'} text={status.state} />
              </dd>
              <dt>RPC</dt>
              <dd>{status.rpcConnected ? '已连接（ws://127.0.0.1:9301）' : '未连接'}</dd>
              <dt>PID</dt><dd>{status.pid ?? '—'}</dd>
              <dt>自动重启</dt>
              <dd>{status.restarts} 次（上限 3，稳定运行 30s 后计数归零）</dd>
              <dt>说明</dt><dd>{status.detail || '—'}</dd>
            </dl>
          )}
        </div>

        <div className="panel">
          <h3>数据库与资源目录</h3>
          <dl className="kv">
            <dt>数据库</dt><dd>{health?.paths?.db ?? '—'}</dd>
            <dt>截图根目录</dt><dd>{health?.paths?.screenshot_root ?? '—'}</dd>
            <dt>报告根目录</dt><dd>{health?.paths?.report_root ?? '—'}</dd>
            <dt>已授权根目录</dt>
            <dd>{engine.roots.length > 0
              ? engine.roots.map((r) => <div key={r}>{r}</div>)
              : '—（壳资源白名单未注册，截图/报告无法预览）'}</dd>
          </dl>
        </div>

        <div className="panel">
          <h3>显示与坐标（DPI）</h3>
          <dl className="kv">
            <dt>缩放比</dt><dd>{(health?.dpi?.scale ?? 1).toFixed(2)}x</dd>
            <dt>DPI 感知</dt>
            <dd>{health?.dpi?.aware ? '已开启' : '未开启（引擎按缩放比补偿坐标）'}</dd>
            <dt>截图坐标空间</dt><dd>{health?.dpi?.space ?? '—'}</dd>
            <dt>分辨率</dt><dd>{health?.dpi?.resolution ?? '—'}</dd>
            <dt>显示器</dt>
            <dd>{(health?.dpi?.monitors ?? [])
              .map((m) => `#${m.index} ${m.rect.join(',')}${m.primary ? '(主)' : ''}`)
              .join('；') || '—'}</dd>
          </dl>
        </div>
      </div>

      <div className="right">
        <div className="panel">
          <h3>驱动与外部依赖</h3>
          {!health ? <Empty text="未取到健康信息" /> : (
            <table className="grid">
              <thead><tr><th>组件</th><th>状态</th><th>详情</th></tr></thead>
              <tbody>
                <tr>
                  <td>数据库</td>
                  <td><Badge status={health.db.ok ? 'pass' : 'fail'}
                             text={health.db.ok ? 'ok' : 'error'} /></td>
                  <td className="mono muted">{health.db.detail}</td>
                </tr>
                {Object.entries(health.drivers ?? {}).map(([name, d]) => (
                  <tr key={name}>
                    <td className="mono">{name}</td>
                    <td><Badge status={d.ok ? 'pass' : 'fail'} text={d.ok ? 'ok' : 'error'} /></td>
                    <td className="mono muted">{d.detail}</td>
                  </tr>
                ))}
                <tr>
                  <td>禅道</td>
                  <td><Badge status={health.zentao.configured ? 'synced' : 'skip'}
                             text={health.zentao.configured ? '已配置' : '未配置'} /></td>
                  <td className="muted">未配置时任务仅落本地（sync_status=local）</td>
                </tr>
                <tr>
                  <td>Pi Agent</td>
                  <td><Badge status={health.agent?.ok ? 'pass' : 'skip'}
                             text={health.agent?.ok ? '可用' : '未就绪'} /></td>
                  <td className="mono muted">{health.agent?.detail ?? '未检测'}</td>
                </tr>
                <tr>
                  <td>SVN CLI</td>
                  <td><Badge status={health.svn?.ok ? 'pass' : 'skip'}
                             text={health.svn?.ok ? '可用' : '未就绪'} /></td>
                  <td className="mono muted">{health.svn?.detail ?? '未检测'}</td>
                </tr>
                <tr>
                  <td>设备模拟器</td>
                  <td><Badge status={health.emulator.connected ? 'pass' : 'skip'}
                             text={health.emulator.connected ? '已连接' : '未连接'} /></td>
                  <td className="muted">未连接时 device 步骤与状态断言不可用</td>
                </tr>
              </tbody>
            </table>
          )}
        </div>

        <div className="panel">
          <h3>环境变量约定</h3>
          <dl className="kv">
            <dt>LLM_API_BASE</dt><dd>内网对话模型地址（用例解析、失败归因）</dd>
            <dt>LLM_API_KEY</dt><dd>对话模型密钥（<strong>不放配置文件</strong>）</dd>
            <dt>VLM_API_KEY</dt><dd>内网多模态模型密钥（视觉定位、视觉断言）</dd>
            <dt>ZENTAO_TOKEN</dt><dd>禅道 API Token（缺失时回落账号密码）</dd>
            <dt>ZENTAO_ACCOUNT</dt><dd>禅道账号（Token 不可用时）</dd>
            <dt>ZENTAO_PASSWORD</dt><dd>禅道密码</dd>
            <dt>TESTPILOT_HOME</dt><dd>可写数据根（壳启动 sidecar 时注入）</dd>
            <dt>TESTPILOT_NO_OVERRIDE</dt><dd>设为 1 时忽略配置覆盖层（排障用）</dd>
            <dt>TESTPILOT_SIDECAR</dt><dd>显式指定 sidecar 可执行文件路径</dd>
            <dt>TESTPILOT_RPC_HOST</dt><dd>覆盖 RPC 监听地址（默认 127.0.0.1）</dd>
            <dt>TESTPILOT_RPC_PORT</dt><dd>覆盖 RPC 端口（默认 9301）</dd>
          </dl>
          <div className="faint" style={{ marginTop: 'var(--sp-3)' }}>
            密钥一律走环境变量：配置文件可能被复制/备份，写进去等于泄露。
          </div>
        </div>
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- 配置面板 */

/** 单个配置 Tab：只渲染本 Tab 的分组，保存只提交本 Tab 的改动。 */
function ConfigPanel({ tabTitle, groupNames }: {
  tabTitle: string;
  groupNames: string[];
}) {
  const described = useAsync(() => call<ConfigDescribe>('config.describe'), []);
  const [draft, setDraft] = useState<Record<string, string | number | boolean>>({});
  const [busy, setBusy] = useState(false);

  // 当前 Tab 的分组（按引擎返回的分组名过滤；字段规格仍全部来自引擎）
  const groups = useMemo(() => (described.data?.groups ?? []).filter(
    (g) => groupNames.includes(g.group)), [described.data, groupNames]);
  const tabFields = useMemo(() => groups.flatMap((g) => g.fields), [groups]);

  const fieldValue = (f: ConfigField): string | number | boolean => {
    if (f.path in draft) return draft[f.path];
    return f.value ?? (f.type === 'boolean' ? false : '');
  };

  // 只提交"真的改了"的项：一次误触保存不该把整页字段都写进覆盖层。
  // secret 特殊：引擎恒回空值，按值比较永远不相等也无从表达"清空"——
  // 任何进入 draft 的输入都是显式动作（填新值=覆盖，清空保存=清除）。
  const dirty = useMemo(() => tabFields.filter((f) => {
    if (!(f.path in draft)) return false;
    if (f.type === 'secret') return true;
    return String(draft[f.path]) !== String(f.value ?? '');
  }).map((f) => f.path), [draft, tabFields]);

  const dropDraft = () => setDraft((d) => {
    const next = { ...d };
    for (const p of dirty) delete next[p];
    return next;
  });

  const save = async () => {
    if (dirty.length === 0) return;
    setBusy(true);
    try {
      const values = Object.fromEntries(dirty.map((p) => [p, draft[p]]));
      const out = await call<ConfigApplyResult>('config.apply', { values });
      const parts: string[] = [];
      if (out.written) parts.push(`写入 ${out.written} 项`);
      if (out.cleared) parts.push(`清除覆盖 ${out.cleared} 项`);
      pushToast('pass',
        `【${tabTitle}】已保存（${parts.join('，') || '无变化'}），需重启引擎生效`, 8000);
      dropDraft();
      described.reload();
    } catch (e) {
      pushToast('error', `保存失败：${msg(e)}`, 9000);
    } finally {
      setBusy(false);
    }
  };

  const restart = async () => {
    setBusy(true);
    try {
      await engineRestart();
      pushToast('info', '已请求重启引擎，稍候自动刷新健康状态', 6000);
      window.setTimeout(() => described.reload(), 3000);
    } catch (e) {
      pushToast('error', `重启失败：${msg(e)}`);
    } finally {
      setBusy(false);
    }
  };

  const resetAll = async () => {
    setBusy(true);
    try {
      const out = await call<ConfigApplyResult>('config.reset', {});
      pushToast('pass', `已恢复出厂默认（清除覆盖 ${out.cleared} 项），需重启引擎生效`, 8000);
      setDraft({});
      described.reload();
    } catch (e) {
      pushToast('error', `恢复默认失败：${msg(e)}`, 9000);
    } finally {
      setBusy(false);
    }
  };

  if (described.error) return <Msg kind="error">读取配置失败：{described.error}</Msg>;
  if (!described.data) return <Empty text="加载配置…" />;

  const overriddenTotal = described.data.groups
    .reduce((n, g) => n + g.fields.filter((f) => f.overridden).length, 0);

  return (
    <>
      {/* 全局条：覆盖层状态 + 恢复默认 + 重启引擎（各 Tab 共用） */}
      <div className="toolbar" style={{ marginBottom: 'var(--sp-3)' }}>
        <span className="muted">覆盖层</span>
        <span className="mono truncate" style={{ maxWidth: 380 }}
              title={described.data.path}>{described.data.path}</span>
        <Badge status={described.data.exists ? 'synced' : 'skip'}
               text={described.data.exists ? `全页已改 ${overriddenTotal} 项` : '全部出厂默认'} />
        <span className="spacer" />
        <button className="btn" disabled={busy || overriddenTotal === 0}
                title="删除覆盖层，所有配置回到出厂默认"
                onClick={() => void resetAll()}>恢复默认</button>
        <button className="btn" disabled={busy} onClick={() => void restart()}>重启引擎</button>
      </div>

      <Msg kind="info">
        保存后需<strong>重启引擎</strong>才生效（引擎在启动时读取配置）。
        覆盖层只记录与出厂默认不同的项，删除该文件即恢复默认。
      </Msg>

      <div className="col">
        {groups.map((g: ConfigGroup) => (
          <div className="panel" key={g.group}>
            <h3>
              {g.group}
              <span className="hint" style={{ marginLeft: 8 }}>{g.description}</span>
            </h3>
            <div className="form-grid">
              {g.fields.map((f) => (
                <FieldInput key={f.path} field={f} value={fieldValue(f)}
                            edited={dirty.includes(f.path)}
                            onChange={(v) => setDraft((d) => ({ ...d, [f.path]: v }))} />
              ))}
            </div>
          </div>
        ))}

        {/* 每个 Tab 独立的保存条：只提交本 Tab 改动 */}
        <div className="toolbar">
          {dirty.length > 0
            ? <span className="chip">本页待保存 {dirty.length} 项</span>
            : <span className="muted">本页无改动</span>}
          <span className="spacer" />
          <button className="btn" disabled={busy || dirty.length === 0}
                  onClick={dropDraft}>放弃本页改动</button>
          <button className="btn primary" disabled={busy || dirty.length === 0}
                  onClick={() => void save()}>
            {busy ? '保存中…' : `保存（${tabTitle}）`}
          </button>
        </div>
      </div>
    </>
  );
}

function FieldInput({ field, value, edited, onChange }: {
  field: ConfigField;
  value: string | number | boolean;
  edited: boolean;
  onChange: (value: string | number | boolean) => void;
}) {
  const label = (
    <>
      <span>{field.label}</span>
      <span className="path">{field.path}</span>
    </>
  );

  if (field.type === 'boolean') {
    return (
      <div className={`field ${edited ? 'edited' : ''}`}>
        <label>
          <input type="checkbox" checked={Boolean(value)}
                 onChange={(e) => onChange(e.target.checked)} />
          {label}
        </label>
        {field.hint && <span className="hint">{field.hint}</span>}
      </div>
    );
  }

  if (field.type === 'enum') {
    return (
      <div className={`field ${edited ? 'edited' : ''}`}>
        <label>{label}</label>
        <select value={String(value ?? '')} onChange={(e) => onChange(e.target.value)}>
          {(field.enum ?? []).map((opt) => <option key={opt} value={opt}>{opt}</option>)}
        </select>
        {field.hint && <span className="hint">{field.hint}</span>}
      </div>
    );
  }

  if (field.type === 'secret') {
    return (
      <div className={`field ${edited ? 'edited' : ''}`}>
        <label>{label}</label>
        <input
          type="password"
          autoComplete="new-password"
          // 引擎不回显真实值（value 恒空），has_value 只用来提示"已设置过"
          placeholder={field.has_value ? '已设置 —— 输入新值覆盖；清空并保存即清除' : '未设置'}
          value={String(value ?? '')}
          onChange={(e) => onChange(e.target.value)}
        />
        {field.hint && <span className="hint">{field.hint}</span>}
      </div>
    );
  }

  const numeric = field.type === 'integer' || field.type === 'number';
  return (
    <div className={`field ${edited ? 'edited' : ''}`}>
      <label>{label}</label>
      <input
        type={numeric ? 'number' : 'text'}
        value={String(value ?? '')}
        min={field.min ?? undefined}
        max={field.max ?? undefined}
        step={field.step ?? (field.type === 'integer' ? 1 : undefined)}
        onChange={(e) => onChange(e.target.value)}
      />
      {numeric && (field.min !== null || field.max !== null) && (
        <span className="hint">
          范围 {field.min ?? '−∞'} ~ {field.max ?? '+∞'}
          {field.default !== null && `；出厂默认 ${String(field.default)}`}
        </span>
      )}
      {field.hint && <span className="hint">{field.hint}</span>}
    </div>
  );
}
