//! TestPilot 桌面壳入口（设计 2.5 / 14 章）。
//!
//! 壳只做三件事，**不含任何测试业务逻辑**：
//! 1. 监督 Python 引擎 sidecar（启动/崩溃重启/优雅退出）；
//! 2. 把前端的 `rpc_call` 转发到引擎 WebSocket，把引擎事件转成 Tauri 事件；
//! 3. 在**白名单目录**内提供报告/截图等静态资源读取（前端不直连文件系统）。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod config;
mod engine;
mod rpc;

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use serde_json::{json, Value};
use tauri::{AppHandle, Manager, RunEvent};

use engine::Supervisor;
use rpc::EngineRpc;

/// 截图/报告单文件读取上限（8 MiB）：超出说明前端该用缩略图而不是原图。
const MAX_ASSET_BYTES: u64 = 8 * 1024 * 1024;
/// 引擎 RPC 默认端口（与 default.yaml 的 rpc.port 一致）。
const DEFAULT_RPC_PORT: u16 = 9301;

struct AppState {
    rpc: EngineRpc,
    sup: Arc<Supervisor>,
    /// 允许读取的根目录（由前端在用 `health.paths` 之后调用 `sync_roots` 注册）。
    roots: Mutex<Vec<PathBuf>>,
}

// ---------------------------------------------------------------- 命令

#[tauri::command]
fn engine_status(state: tauri::State<'_, AppState>) -> Value {
    let st = state.sup.snapshot_with_rpc(state.rpc.is_connected());
    engine::status_payload(&st)
}

#[tauri::command]
fn engine_log(state: tauri::State<'_, AppState>, lines: Option<usize>) -> Vec<String> {
    state.sup.tail(lines.unwrap_or(200))
}

/// 手动重启引擎：结束 sidecar 进程，监督循环会自动拉起（稳定运行后计数自愈）。
///
/// 监督循环自身已退出（启动失败/重启耗尽）时这里负责**重新拉起监督**——
/// 否则按钮看似成功、引擎却永远不会回来，唯一恢复手段只剩重启整个壳。
#[tauri::command]
fn engine_restart(app: AppHandle, state: tauri::State<'_, AppState>) -> Value {
    state.sup.push("shell", "收到手动重启请求");
    if let Some(child) = state.sup.take_child() {
        let pid = child.pid();
        let _ = child.kill();
        return json!({"ok": true, "killedPid": pid});
    }
    if !state.sup.is_supervising() {
        engine::start(app, state.sup.clone());
        return json!({"ok": true, "detail": "监督循环已退出，已重新拉起"});
    }
    json!({"ok": false, "detail": "sidecar 未在运行（监督循环会自动拉起）"})
}

/// 壳侧独立运行指标（设计 10 章）：重启次数、RPC 时延/计数、壳日志落盘路径。
#[tauri::command]
fn shell_metrics(app: AppHandle, state: tauri::State<'_, AppState>) -> Value {
    let st = state.sup.snapshot_with_rpc(state.rpc.is_connected());
    let rpc = state.rpc.metrics();
    json!({
        "state": st.state,
        "restarts": st.restarts,
        "rpcConnected": st.rpc_connected,
        "rpcCalls": rpc.calls,
        "rpcErrors": rpc.errors,
        "rpcLastMs": rpc.rtt_last_ms,
        "rpcAvgMs": rpc.rtt_avg_ms,
        "shellLog": engine::shell_log_path(&app).display().to_string(),
    })
}

/// 转发一次引擎 RPC（前端唯一的业务入口）。
#[tauri::command]
async fn rpc_call(
    state: tauri::State<'_, AppState>,
    method: String,
    params: Option<Value>,
) -> Result<Value, String> {
    let params = params.unwrap_or_else(|| json!({}));
    state.rpc.call(&method, params).await
}

/// 注册资源白名单根目录（前端取到 `health.paths` 后调用一次）。
///
/// 引擎会连同 `db` 一起下发，而 `db` 是**文件**路径。若原样登记，下面
/// `starts_with(root)` 的白名单判断就只有这个文件本身能通过，等于白登记一条。
/// 所以这里对文件路径改用其**父目录** —— 数据根（`shots/`、`reports/`、`*.db`）
/// 本就是本应用自己的产出目录，授权它符合白名单的本意。
#[tauri::command]
fn sync_roots(state: tauri::State<'_, AppState>, paths: Vec<String>) -> Value {
    let mut roots = state.roots.lock().unwrap_or_else(|p| p.into_inner());
    roots.clear();
    for p in paths {
        if p.trim().is_empty() {
            continue;
        }
        let Ok(canonical) = Path::new(&p).canonicalize() else {
            continue;                       // 不存在/不可访问：跳过（不阻断其他根）
        };
        let root = if canonical.is_file() {
            match canonical.parent() {
                Some(parent) => parent.to_path_buf(),
                None => canonical,
            }
        } else {
            canonical
        };
        roots.push(root);
    }
    json!({"roots": roots.iter().map(|r| r.display().to_string()).collect::<Vec<_>>()})
}

/// 读取白名单内的文本资源（Markdown 报告等）。
#[tauri::command]
fn read_text_file(state: tauri::State<'_, AppState>, path: String) -> Result<String, String> {
    let resolved = resolve_asset(&state, &path)?;
    // 与 read_asset_data_url 同一上限：超大文件整读进内存再经 IPC 序列化会拖垮壳
    let meta = std::fs::metadata(&resolved)
        .map_err(|e| format!("读取失败 {}: {e}", resolved.display()))?;
    if meta.len() > MAX_ASSET_BYTES {
        return Err(format!("资源过大（{} 字节），请改用分段读取", meta.len()));
    }
    std::fs::read_to_string(&resolved)
        .map_err(|e| format!("读取失败 {}: {e}", resolved.display()))
}

/// 读取白名单内的二进制资源并返回 data URL（前端直接 `<img src>`）。
#[tauri::command]
fn read_asset_data_url(
    state: tauri::State<'_, AppState>,
    path: String,
) -> Result<String, String> {
    let resolved = resolve_asset(&state, &path)?;
    let meta = std::fs::metadata(&resolved)
        .map_err(|e| format!("读取失败 {}: {e}", resolved.display()))?;
    if meta.len() > MAX_ASSET_BYTES {
        return Err(format!("资源过大（{} 字节），请改用缩略图",
                           meta.len()));
    }
    let bytes = std::fs::read(&resolved)
        .map_err(|e| format!("读取失败 {}: {e}", resolved.display()))?;
    let mime = match resolved
        .extension()
        .and_then(|e| e.to_str())
        .map(str::to_ascii_lowercase)
        .as_deref()
    {
        Some("png") => "image/png",
        Some("jpg") | Some("jpeg") => "image/jpeg",
        Some("md") | Some("txt") | Some("log") => "text/plain",
        Some("html") => "text/html",
        _ => "application/octet-stream",
    };
    Ok(format!("data:{mime};base64,{}", base64(&bytes)))
}

// ---------------------------------------------------------------- 内部

/// 解析并校验资源路径：必须落在已注册根目录内，否则拒绝。
fn resolve_asset(state: &tauri::State<'_, AppState>, path: &str) -> Result<PathBuf, String> {
    let roots = state.roots.lock().unwrap_or_else(|p| p.into_inner()).clone();
    if roots.is_empty() {
        return Err("资源根目录未注册（请先调用 sync_roots）".into());
    }
    let candidate = Path::new(path)
        .canonicalize()
        .map_err(|e| format!("路径不可访问 {path}: {e}"))?;
    if roots.iter().any(|root| candidate.starts_with(root)) {
        Ok(candidate)
    } else {
        Err(format!("路径不在白名单内: {}", candidate.display()))
    }
}

/// 标准 base64（避免为一个命令引入额外依赖）。
fn base64(data: &[u8]) -> String {
    const TABLE: &[u8; 64] =
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b = [chunk[0], *chunk.get(1).unwrap_or(&0), *chunk.get(2).unwrap_or(&0)];
        let n = ((b[0] as u32) << 16) | ((b[1] as u32) << 8) | b[2] as u32;
        out.push(TABLE[(n >> 18) as usize & 63] as char);
        out.push(TABLE[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 { TABLE[(n >> 6) as usize & 63] as char } else { '=' });
        out.push(if chunk.len() > 2 { TABLE[n as usize & 63] as char } else { '=' });
    }
    out
}

fn build_state(app: &AppHandle) -> AppState {
    let port = std::env::var("TESTPILOT_RPC_PORT")
        .ok()
        .and_then(|p| p.parse::<u16>().ok())
        .unwrap_or(DEFAULT_RPC_PORT);
    let host = std::env::var("TESTPILOT_RPC_HOST")
        .unwrap_or_else(|_| "127.0.0.1".to_string());
    AppState {
        rpc: EngineRpc::spawn(app.clone(), &host, port),
        sup: Arc::new(Supervisor::new()),
        roots: Mutex::new(Vec::new()),
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let handle = app.handle().clone();
            let state = build_state(&handle);
            let sup = state.sup.clone();
            let rpc = state.rpc.clone();
            app.manage(state);
            engine::start(handle.clone(), sup.clone());
            // 独立于 UI 的周期健康检查（间隔取自 default.yaml 的 health_interval_s）
            engine::spawn_health_probe(handle, sup, Arc::new(rpc));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            engine_status,
            engine_log,
            engine_restart,
            shell_metrics,
            rpc_call,
            sync_roots,
            read_text_file,
            read_asset_data_url,
        ])
        .build(tauri::generate_context!())
        .expect("TestPilot 壳启动失败")
        .run(|app, event| {
            // 退出前必须杀掉 sidecar，否则会留下孤儿 Python 进程占住 9301 端口
            if let RunEvent::ExitRequested { .. } = &event {
                if let Some(state) = app.try_state::<AppState>() {
                    state.sup.push("shell", "壳退出：结束引擎 sidecar");
                    state.sup.shutdown();
                }
            }
        });
}
