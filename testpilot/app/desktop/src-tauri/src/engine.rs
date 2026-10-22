//! Python 引擎 sidecar 监督（设计 2.5：壳负责启动/健康检查/崩溃重启）。
//!
//! 打包形态：PyInstaller **onedir** 产出 `testpilot-sidecar/`（含 `_internal/` 依赖），
//! 作为 Tauri **resources** 打进安装包，壳按资源目录解析可执行文件后直接拉起。
//!
//! 为什么不用 `externalBin`：Tauri 的 externalBin 只搬运**单个**可执行文件，
//! onedir 的依赖目录不会被带进安装包 —— 打出来的包会"能启动但一拉引擎就找不到 DLL"。
//! 因此这里用 resources + `shell().command(path)`，并在四个位置依次探测：
//! 环境变量 → 打包资源目录 → 开发目录 → 嵌入式引擎（单文件绿色版，见下文）。
//!
//! 约定：
//! - 崩溃自动重启采用**指数退避**（1s / 2s / 4s），上限 3 次；超过则停在
//!   `stopped` 并让前端徽章显示告警态（不静默吞掉故障）；
//! - 侧车 stdout/stderr 收进环形缓冲（最近 [`LOG_CAPACITY`] 行），前端底部抽屉
//!   点开即看，不必去磁盘找日志。
use std::collections::VecDeque;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::Serialize;
use serde_json::{json, Value};
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

use crate::config;
use crate::rpc::EVENT_PREFIX;
use crate::rpc::EngineRpc;

/// 侧车可执行文件名（无扩展名；Windows 下自动补 `.exe`）。
pub const SIDECAR_BIN: &str = "testpilot-sidecar";
/// 侧车所在的资源子目录名（`tauri.conf.json` 的 resources 目标目录）。
pub const SIDECAR_DIR: &str = "sidecar";
/// 侧车日志环形缓冲容量（行）。
pub const LOG_CAPACITY: usize = 500;
/// 壳侧独立日志文件（JSONL，落在数据根 `logs/` 下）：sidecar 生命周期与重启计数。
pub const SHELL_LOG: &str = "shell.log";
/// 稳定运行超过该秒数即认为"这次启动是成功的"，重启计数清零。
pub const STABLE_SECS: u64 = 30;

/// 平台可执行文件名。
fn exe_name() -> String {
    if cfg!(windows) {
        format!("{SIDECAR_BIN}.exe")
    } else {
        SIDECAR_BIN.to_string()
    }
}

// ---------------------------------------------------------------- 单文件绿色版
//
// 构建期由 scripts/build-single.ps1 注入 `TESTPILOT_EMBED_ENGINE`（指向 onefile
// 引擎，如 dist/Code.exe），build.rs 据此定义 cfg `embedded_engine`。此时壳二进制
// **自带引擎**，运行时释放到数据根 `engine/` 子目录再拉起 —— 产物是免安装单 exe。
// 普通构建（build-shell.ps1 / cargo test / tauri dev）不含此段，行为不变。

/// 嵌入的引擎字节（onefile 引擎整文件）。
#[cfg(embedded_engine)]
const EMBEDDED_ENGINE: &[u8] = include_bytes!(env!("TESTPILOT_EMBED_ENGINE_PATH"));
/// 嵌入式引擎释放后的文件名（数据根 `engine/` 下）。
#[cfg(embedded_engine)]
pub const EMBEDDED_ENGINE_NAME: &str = "Code.exe";

/// 确保嵌入式引擎已释放到 `<数据根>/engine/Code.exe`，返回其路径。
///
/// 以**文件大小**判定是否需要重写：引擎随版本更新时大小必变，自动覆盖；
/// 平时命中缓存零成本（一次 metadata 调用）。重写走"临时文件 + rename"，
/// 避免写一半崩溃留下残缺引擎。rename 失败（如旧引擎进程仍占用文件）时
/// 只要旧文件存在就沿用旧版 —— 跑旧引擎好过没引擎。
#[cfg(embedded_engine)]
fn ensure_embedded_engine(app: &AppHandle) -> Option<PathBuf> {
    let dir = data_dir(app).join("engine");
    let path = dir.join(EMBEDDED_ENGINE_NAME);
    let stale = std::fs::metadata(&path)
        .map(|m| m.len() != EMBEDDED_ENGINE.len() as u64)
        .unwrap_or(true);
    if stale {
        std::fs::create_dir_all(&dir).ok()?;
        let tmp = dir.join(".Code.exe.tmp");
        std::fs::write(&tmp, EMBEDDED_ENGINE).ok()?;
        if std::fs::rename(&tmp, &path).is_err() && !path.exists() {
            return None;
        }
    }
    Some(path)
}

/// 非嵌入构建：永远返回 None（resolve_sidecar 走既有链路）。
#[cfg(not(embedded_engine))]
fn ensure_embedded_engine(_app: &AppHandle) -> Option<PathBuf> {
    None
}

/// 解析 sidecar 可执行文件路径：环境变量 → 打包资源目录 → 开发目录 → 嵌入式引擎。
fn resolve_sidecar(app: &AppHandle) -> Result<PathBuf, String> {
    let name = exe_name();

    if let Ok(explicit) = std::env::var("TESTPILOT_SIDECAR") {
        let path = PathBuf::from(&explicit);
        if path.exists() {
            return Ok(path);
        }
        return Err(format!("TESTPILOT_SIDECAR 指向的文件不存在: {explicit}"));
    }

    if let Ok(dir) = app.path().resource_dir() {
        for rel in [vec![SIDECAR_DIR, name.as_str()],
                    vec!["binaries", SIDECAR_DIR, name.as_str()]] {
            let candidate: PathBuf = rel.iter().fold(dir.clone(), |acc, p| acc.join(p));
            if candidate.exists() {
                return Ok(candidate);
            }
        }
    }

    // 开发态（cargo/`npm run shell:dev`）：源码树里的 binaries/sidecar
    let dev = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("binaries")
        .join(SIDECAR_DIR)
        .join(&name);
    if dev.exists() {
        return Ok(dev);
    }

    // 单文件绿色版兜底：释放嵌入的引擎（普通构建此分支恒 None）
    if let Some(path) = ensure_embedded_engine(app) {
        return Ok(path);
    }

    Err(format!(
        "未找到引擎 sidecar（{name}）。请先执行 scripts/build-sidecar.ps1 生成 \
         binaries/{SIDECAR_DIR}/，或用 TESTPILOT_SIDECAR 指定路径"
    ))
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct EngineStatus {
    /// starting | running | restarting | stopped
    pub state: String,
    pub pid: Option<u32>,
    pub restarts: u32,
    pub rpc_connected: bool,
    pub detail: String,
}

impl Default for EngineStatus {
    fn default() -> Self {
        Self {
            state: "starting".into(),
            pid: None,
            restarts: 0,
            rpc_connected: false,
            detail: String::new(),
        }
    }
}

pub struct Supervisor {
    pub status: Arc<Mutex<EngineStatus>>,
    log: Arc<Mutex<VecDeque<String>>>,
    child: Arc<Mutex<Option<CommandChild>>>,
    shutdown: Arc<AtomicBool>,
    /// 监督循环是否存活。循环退出（重启耗尽/启动失败）后置 false，
    /// 手动重启命令据此把监督重新拉起，否则"重启引擎"按钮会永远无效。
    supervising: Arc<AtomicBool>,
}

impl Supervisor {
    pub fn new() -> Self {
        Self {
            status: Arc::new(Mutex::new(EngineStatus::default())),
            log: Arc::new(Mutex::new(VecDeque::with_capacity(LOG_CAPACITY))),
            child: Arc::new(Mutex::new(None)),
            shutdown: Arc::new(AtomicBool::new(false)),
            supervising: Arc::new(AtomicBool::new(false)),
        }
    }

    pub fn is_supervising(&self) -> bool {
        self.supervising.load(Ordering::SeqCst)
    }

    pub fn snapshot(&self) -> EngineStatus {
        self.status.lock().unwrap_or_else(|p| p.into_inner()).clone()
    }

    /// 前端徽章显示的状态：把 RPC 连接态并入（sidecar 活着但 RPC 没连上也算异常）。
    pub fn snapshot_with_rpc(&self, rpc_connected: bool) -> EngineStatus {
        let mut st = self.snapshot();
        st.rpc_connected = rpc_connected;
        st
    }

    pub fn push(&self, channel: &str, line: &str) {
        let mut buf = self.log.lock().unwrap_or_else(|p| p.into_inner());
        while buf.len() >= LOG_CAPACITY {
            buf.pop_front();
        }
        buf.push_back(format!("[{channel}] {line}"));
    }

    /// 最近 `lines` 行日志（按时间正序返回，前端抽屉直接渲染）。
    pub fn tail(&self, lines: usize) -> Vec<String> {
        let buf = self.log.lock().unwrap_or_else(|p| p.into_inner());
        let skip = buf.len().saturating_sub(lines);
        buf.iter().skip(skip).cloned().collect()
    }

    /// 停止监督并结束 sidecar（壳退出时调用）。
    pub fn shutdown(&self) {
        self.shutdown.store(true, Ordering::SeqCst);
        if let Some(child) = self.take_child() {
            let _ = child.kill();
        }
    }

    /// 取出当前 sidecar 句柄（手动重启用：杀掉后由监督循环重新拉起）。
    pub fn take_child(&self) -> Option<CommandChild> {
        self.child.lock().unwrap_or_else(|p| p.into_inner()).take()
    }
}

impl Default for Supervisor {
    fn default() -> Self {
        Self::new()
    }
}

/// 在 AppHandle 上记一行壳日志（rpc 模块复用）。
pub fn log_line(app: &AppHandle, msg: &str) {
    if let Some(state) = app.try_state::<crate::AppState>() {
        state.sup.push("shell", msg);
    }
}

fn set_status(app: &AppHandle, sup: &Supervisor, state: &str, pid: Option<u32>,
              restarts: u32, detail: &str) {
    {
        let mut st = sup.status.lock().unwrap_or_else(|p| p.into_inner());
        st.state = state.to_string();
        st.pid = pid;
        st.restarts = restarts;
        st.detail = detail.to_string();
    }
    // 壳侧独立落盘：引擎日志里看不到"sidecar 重启过几次/为什么重启"（设计 10 章）
    append_shell_log(app, state, pid, restarts, detail);
    let _ = app.emit(&format!("{EVENT_PREFIX}engine.status"), json!({
        "state": state,
        "pid": pid,
        "restarts": restarts,
        "detail": detail,
    }));
}

/// 启动监督循环（幂等；循环已退出时可再次调用把它拉回来）。
pub fn start(app: AppHandle, sup: Arc<Supervisor>) {
    // swap 幂等：监督已在跑时直接返回（手动重启只是杀 sidecar，循环会自动拉起）
    if sup.supervising.swap(true, Ordering::SeqCst) {
        return;
    }
    tauri::async_runtime::spawn(async move {
        supervise(app, sup.clone()).await;
        sup.supervising.store(false, Ordering::SeqCst);
    });
}

async fn supervise(app: AppHandle, sup: Arc<Supervisor>) {
    // 监督参数来自随包 `default.yaml`（`restart_max` 必须在启动前就能读到，
    // 那时 RPC 尚未建立，所以不能靠引擎下发）。
    let cfg = config::load(&app);
    sup.push("shell", &format!(
        "监督参数：supervised={} restart_max={} health_interval_s={}（来源 default.yaml）",
        cfg.supervised, cfg.restart_max, cfg.health_interval_s));

    let mut restarts = 0u32;
    loop {
        set_status(&app, &sup, "starting", None, restarts, "正在启动引擎 sidecar");
        let started = Instant::now();
        match spawn_once(&app, &sup) {
            Ok((pid, mut events)) => {
                set_status(&app, &sup, "running", Some(pid), restarts, "引擎运行中");
                drain_logs(&sup, &mut events).await;   // 侧车退出后返回
                if started.elapsed() >= Duration::from_secs(STABLE_SECS) {
                    // 稳定跑够时长后清零：手动重启/偶发闪退不会累积吃掉重启额度
                    sup.push("shell", "引擎稳定运行已超阈值，重启计数清零");
                    restarts = 0;
                }
            }
            Err(e) => {
                sup.push("shell", &format!("引擎启动失败: {e}"));
                set_status(&app, &sup, "stopped", None, restarts,
                           &format!("启动失败: {e}（可在前端点「重启引擎」重试）"));
                return;
            }
        }

        if sup.shutdown.load(Ordering::SeqCst) {
            set_status(&app, &sup, "stopped", None, restarts, "引擎已停止");
            return;
        }
        if !cfg.supervised {
            sup.push("shell", "supervised=false：引擎已退出，不再自动重启");
            set_status(&app, &sup, "stopped", None, restarts,
                       "引擎已退出（supervised=false，未自动重启）");
            return;
        }
        restarts += 1;
        if restarts > cfg.restart_max {
            sup.push("shell", &format!(
                "已达重启上限（{} 次），停止监督（前端「重启引擎」可重新拉起）",
                cfg.restart_max));
            set_status(&app, &sup, "stopped", None, restarts - 1,
                       &format!("已达重启上限（{} 次），请手动重启引擎", cfg.restart_max));
            return;
        }
        let backoff = 1000u64 * (1 << (restarts - 1).min(2));   // 1s/2s/4s
        set_status(&app, &sup, "restarting", None, restarts,
                   &format!("引擎意外退出，{backoff}ms 后重启（第 {restarts} 次）"));
        tokio::time::sleep(Duration::from_millis(backoff)).await;
    }
}

/// 周期性健康检查（`health_interval_s` 驱动）。
///
/// 为什么不能只靠前端轮询：前端只在页面打开时建立连接，**关页面/最小化后就没人在看**。
/// 引擎"进程还在但 RPC 不响应"（假死）时，监督循环因进程未退出而不会重启，
/// 状态徽章又会一直显示"运行中"。这里独立于 UI 探测，发现假死就记进 detail，
/// 让用户切回窗口时能看到问题而不是被蒙在鼓里。
///
/// 注意：**不主动杀进程**。假死也可能是引擎正在跑长用例（单步超时可达分钟级），
/// 误杀会丢掉整轮轨迹。只做"如实上报"。
pub fn spawn_health_probe(app: AppHandle, sup: Arc<Supervisor>, rpc: Arc<EngineRpc>) {
    let cfg = config::load(&app);
    let interval = cfg.health_interval_s;
    tauri::async_runtime::spawn(async move {
        let mut consecutive_failures = 0u32;
        loop {
            tokio::time::sleep(Duration::from_secs(interval)).await;
            if sup.shutdown.load(Ordering::SeqCst) {
                return;
            }
            // 引擎没在跑就没什么可探的（监督循环会负责拉起）
            {
                let st = sup.status.lock().unwrap_or_else(|p| p.into_inner());
                if st.state != "running" {
                    consecutive_failures = 0;
                    continue;
                }
            }
            match rpc.call("health", json!({})).await {
                Ok(_) => {
                    if consecutive_failures > 0 {
                        sup.push("shell", "引擎健康检查恢复正常");
                        consecutive_failures = 0;
                    }
                }
                Err(e) => {
                    consecutive_failures += 1;
                    // 单次失败可能只是重连中，连续 3 次才提示（避免噪声）
                    if consecutive_failures >= 3 {
                        sup.push("shell", &format!(
                            "引擎连续 {consecutive_failures} 次健康检查失败（可能假死）: {e}"));
                        let (pid, restarts) = {
                            let st = sup.status.lock().unwrap_or_else(|p| p.into_inner());
                            (st.pid, st.restarts)
                        };
                        set_status(&app, &sup, "running", pid, restarts,
                                   &format!("引擎无响应（连续 {consecutive_failures} 次健康检查失败）"));
                    }
                }
            }
        }
    });
}

/// 可写数据根：安装到 `Program Files` 后 cwd 只读，DB/日志/截图必须落到用户目录。
///
/// 解析顺序：`TESTPILOT_HOME` 环境变量 → Tauri 的 app_data_dir（`%LOCALAPPDATA%\com.testpilot.desktop`）
/// → 系统临时目录兜底（宁可写在临时目录，也不要因只读目录直接崩）。
fn data_dir(app: &AppHandle) -> PathBuf {
    if let Ok(explicit) = std::env::var("TESTPILOT_HOME") {
        if !explicit.trim().is_empty() {
            return PathBuf::from(explicit);
        }
    }
    app.path()
        .app_data_dir()
        .unwrap_or_else(|_| std::env::temp_dir().join("TestPilot"))
}

/// 壳侧独立日志路径（`<数据根>/logs/shell.log`）。
pub fn shell_log_path(app: &AppHandle) -> PathBuf {
    data_dir(app).join("logs").join(SHELL_LOG)
}

/// 追加一行壳日志（JSONL）。写失败静默：日志问题不该影响监督主循环。
fn append_shell_log(app: &AppHandle, state: &str, pid: Option<u32>, restarts: u32,
                    detail: &str) {
    let path = shell_log_path(app);
    let Some(dir) = path.parent() else { return };
    if std::fs::create_dir_all(dir).is_err() {
        return;
    }
    let ts_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    use std::io::Write;
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true).append(true).open(&path)
    {
        let _ = writeln!(f, "{}", shell_log_line(state, pid, restarts, detail, ts_ms));
    }
}

/// 组装壳日志 JSONL 行（纯函数便于单测）。
fn shell_log_line(state: &str, pid: Option<u32>, restarts: u32, detail: &str,
                  ts_ms: u128) -> String {
    json!({
        "ts_ms": ts_ms,
        "state": state,
        "pid": pid,
        "restarts": restarts,
        "detail": detail,
    }).to_string()
}

/// 拉起侧车，返回 (pid, 事件流)；**不阻塞**，由调用方消费事件流。
fn spawn_once(
    app: &AppHandle,
    sup: &Supervisor,
) -> Result<(u32, tauri::async_runtime::Receiver<CommandEvent>), String> {
    let program = resolve_sidecar(app)?;
    let data = data_dir(app);
    if let Err(e) = std::fs::create_dir_all(&data) {
        sup.push("shell", &format!("数据目录创建失败（继续尝试）: {} {e}", data.display()));
    }
    sup.push("shell", &format!("拉起引擎: {}（数据根 {}）", program.display(), data.display()));

    let home = data.display().to_string();
    let cmd = app
        .shell()
        .command(program.to_string_lossy().to_string())
        .args(sidecar_args())
        // 关键：cwd 设为可写目录，并把数据根显式告知引擎（引擎会据此重定位相对路径）
        .current_dir(data)
        .env("TESTPILOT_HOME", home)
        .env("TESTPILOT_RPC_TOKEN", crate::rpc::rpc_token());
    let (events, child) = cmd.spawn().map_err(|e| format!("侧车启动失败: {e}"))?;
    let pid = child.pid();
    *sup.child.lock().unwrap_or_else(|p| p.into_inner()) = Some(child);
    Ok((pid, events))
}

/// 消费侧车事件流直到其退出（stdout/stderr 进环形缓冲，退出码入日志）。
async fn drain_logs(
    sup: &Supervisor,
    events: &mut tauri::async_runtime::Receiver<CommandEvent>,
) {
    while let Some(event) = events.recv().await {
        match event {
            CommandEvent::Stdout(bytes) => {
                for line in split_lines(&bytes) {
                    sup.push("out", &line);
                }
            }
            CommandEvent::Stderr(bytes) => {
                for line in split_lines(&bytes) {
                    sup.push("err", &line);
                }
            }
            CommandEvent::Error(err) => sup.push("shell", &format!("侧车错误: {err}")),
            CommandEvent::Terminated(payload) => {
                sup.push("shell", &format!(
                    "侧车退出 code={:?} signal={:?}", payload.code, payload.signal));
                break;
            }
            _ => {}
        }
    }
    *sup.child.lock().unwrap_or_else(|p| p.into_inner()) = None;
}

/// `serve` + 可选的用户覆盖（`TESTPILOT_CONFIG`/`TESTPILOT_RPC_PORT`）。
fn sidecar_args() -> Vec<String> {
    let mut args = vec!["serve".to_string()];
    if let Ok(cfg) = std::env::var("TESTPILOT_CONFIG") {
        if !cfg.trim().is_empty() {
            args.push("--config".into());
            args.push(cfg);
        }
    }
    args
}

/// 字节流按行切分（保留非 UTF-8 的损失转换，日志不该因编码中断监督）。
fn split_lines(bytes: &[u8]) -> Vec<String> {
    String::from_utf8_lossy(bytes)
        .split(['\n', '\r'])
        .filter(|s| !s.trim().is_empty())
        .map(|s| s.to_string())
        .collect()
}

/// `engine.status` 事件的公共 payload（main.rs 的命令复用）。
pub fn status_payload(st: &EngineStatus) -> Value {
    json!({
        "state": st.state,
        "pid": st.pid,
        "restarts": st.restarts,
        "rpcConnected": st.rpc_connected,
        "detail": st.detail,
    })
}

#[cfg(test)]
mod tests {
    use super::shell_log_line;

    #[test]
    fn shell_log_line_is_single_jsonl_row() {
        let line = shell_log_line("restarting", None, 2, "引擎意外退出", 1_726_000_000_000);
        assert!(line.contains("\"state\":\"restarting\""));
        assert!(line.contains("\"restarts\":2"));
        assert!(line.contains("\"ts_ms\":1726000000000"));
        assert!(line.contains("\"detail\":\"引擎意外退出\""));
        assert!(!line.contains('\n'), "JSONL 行不能含换行");
    }
}
