//! 引擎 RPC 客户端（壳 -> Python sidecar）：JSON-RPC 2.0 over WebSocket。
//!
//! 职责边界（设计 2.5）：
//! - **只做转发与通道复用**，不含任何测试业务逻辑；
//! - 引擎的领域事件（`run.progress` / `case.finished` / `step.finished`）是
//!   WebSocket **通知**（带 `method`、无 `id`），原样转成 Tauri 事件
//!   `engine://<event>` 推给前端，前端无需轮询；
//! - 断线时把所有在途请求立即失败（不让前端挂在 30 分钟的一次 `task.run` 上），
//!   同时按固定间隔自动重连 —— sidecar 重启期间前端照常可用。
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tauri::{AppHandle, Emitter};
use tokio::sync::{broadcast, mpsc, oneshot};
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;

pub fn rpc_token() -> &'static str {
    static TOKEN: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    TOKEN.get_or_init(|| {
        if let Ok(token) = std::env::var("TESTPILOT_RPC_TOKEN") {
            if token.len() >= 32 { return token; }
        }
        let mut bytes = [0u8; 32];
        #[cfg(windows)]
        {
            #[link(name = "bcrypt")]
            extern "system" {
                fn BCryptGenRandom(algorithm: *mut std::ffi::c_void, buffer: *mut u8,
                                   len: u32, flags: u32) -> i32;
            }
            let status = unsafe { BCryptGenRandom(std::ptr::null_mut(),
                                                  bytes.as_mut_ptr(), 32, 2) };
            assert!(status >= 0, "无法生成 RPC 会话密钥");
        }
        #[cfg(not(windows))]
        {
            use std::io::Read;
            std::fs::File::open("/dev/urandom").and_then(|mut f| f.read_exact(&mut bytes))
                .expect("无法生成 RPC 会话密钥");
        }
        bytes.iter().map(|b| format!("{b:02x}")).collect()
    })
}
use tokio_tungstenite::tungstenite::Message;

use crate::engine;

/// 壳侧事件名前缀：与 Tauri 内建事件隔离，前端只监听 `engine://*`。
pub const EVENT_PREFIX: &str = "engine://";
/// 断线重连间隔（sidecar 未就绪时也按此节奏探活）。
const RECONNECT_MS: u64 = 500;

enum Cmd {
    Call { id: u64, method: String, params: Value },
}

type Pending = Arc<Mutex<HashMap<u64, oneshot::Sender<Result<Value, String>>>>>;

/// 壳侧 RPC 指标（设计 10 章：Tauri 侧独立观测，不依赖引擎自报）。
#[derive(Clone, Copy, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RpcMetrics {
    pub calls: u64,
    pub errors: u64,
    pub rtt_last_ms: u64,
    pub rtt_avg_ms: u64,
}

#[derive(Clone)]
pub struct EngineRpc {
    tx: mpsc::Sender<Cmd>,
    pending: Pending,
    disconnect: broadcast::Sender<()>,
    next_id: Arc<AtomicU64>,
    connected: Arc<AtomicBool>,
    calls: Arc<AtomicU64>,
    errors: Arc<AtomicU64>,
    rtt_last_ms: Arc<AtomicU64>,
    rtt_total_ms: Arc<AtomicU64>,
}

impl EngineRpc {
    /// 创建客户端并启动后台连接循环（立即返回，不等连接成功）。
    pub fn spawn(app: AppHandle, host: &str, port: u16) -> Self {
        let url = format!("ws://{host}:{port}");
        let (tx, rx) = mpsc::channel::<Cmd>(64);
        let pending: Pending = Arc::new(Mutex::new(HashMap::new()));
        let (disconnect, _) = broadcast::channel::<()>(8);
        let connected = Arc::new(AtomicBool::new(false));

        let rpc = Self {
            tx,
            pending: pending.clone(),
            disconnect: disconnect.clone(),
            next_id: Arc::new(AtomicU64::new(1)),
            connected: connected.clone(),
            calls: Arc::new(AtomicU64::new(0)),
            errors: Arc::new(AtomicU64::new(0)),
            rtt_last_ms: Arc::new(AtomicU64::new(0)),
            rtt_total_ms: Arc::new(AtomicU64::new(0)),
        };

        let worker = Worker { app, url, pending, disconnect, connected };
        tauri::async_runtime::spawn(worker.run(rx));
        rpc
    }

    pub fn is_connected(&self) -> bool {
        self.connected.load(Ordering::SeqCst)
    }

    /// 壳侧指标快照（重启/连接态由监督器另算，见 `shell_metrics` 命令）。
    pub fn metrics(&self) -> RpcMetrics {
        let calls = self.calls.load(Ordering::SeqCst);
        let total = self.rtt_total_ms.load(Ordering::SeqCst);
        RpcMetrics {
            calls,
            errors: self.errors.load(Ordering::SeqCst),
            rtt_last_ms: self.rtt_last_ms.load(Ordering::SeqCst),
            rtt_avg_ms: if calls > 0 { total / calls } else { 0 },
        }
    }

    /// 发起一次 RPC。引擎未连接时立即失败（前端据此提示"引擎未就绪"）。
    pub async fn call(&self, method: &str, params: Value) -> Result<Value, String> {
        let t0 = Instant::now();
        let out = self.call_inner(method, params).await;
        let ms = t0.elapsed().as_millis() as u64;
        self.calls.fetch_add(1, Ordering::SeqCst);
        self.rtt_last_ms.store(ms, Ordering::SeqCst);
        self.rtt_total_ms.fetch_add(ms, Ordering::SeqCst);
        if out.is_err() {
            self.errors.fetch_add(1, Ordering::SeqCst);
        }
        out
    }

    async fn call_inner(&self, method: &str, params: Value) -> Result<Value, String> {
        if !self.is_connected() { return Err("引擎未连接，请稍后重试".into()); }
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let (otx, orx) = oneshot::channel();
        self.pending.lock().unwrap_or_else(|p| p.into_inner()).insert(id, otx);

        let mut disc = self.disconnect.subscribe();
        if self
            .tx
            .send(Cmd::Call { id, method: method.to_string(), params })
            .await
            .is_err()
        {
            self.pending.lock().unwrap_or_else(|p| p.into_inner()).remove(&id);
            return Err("引擎 RPC 通道已关闭".into());
        }

        let deadline = if method == "task.run" { 24 * 60 * 60 } else if matches!(method, "case.pull" | "case.sync" | "case.parse") { 24 * 60 * 60 } else if method == "health" { 8 } else { 180 };
        let result = tokio::select! {
            got = orx => got.map_err(|_| "引擎未返回结果".to_string())?,
            _ = disc.recv() => Err("引擎连接已断开".into()),
            _ = tokio::time::sleep(Duration::from_secs(deadline)) => Err("请求超时，请检查引擎状态".into()),
        };
        self.pending.lock().unwrap_or_else(|p| p.into_inner()).remove(&id);
        result
    }
}

struct Worker {
    app: AppHandle,
    url: String,
    pending: Pending,
    disconnect: broadcast::Sender<()>,
    connected: Arc<AtomicBool>,
}

impl Worker {
    async fn run(self, mut rx: mpsc::Receiver<Cmd>) {
        let mut waits: u64 = 0;
        loop {
            let mut request = self.url.clone().into_client_request().expect("RPC URL");
            request.headers_mut().insert("Authorization",
                format!("Bearer {}", rpc_token()).parse().expect("RPC token"));
            match connect_async(request).await {
                Ok((ws, _)) => {
                    self.connected.store(true, Ordering::SeqCst);
                    engine::log_line(&self.app, "[shell] 已连接引擎 RPC");
                    let (mut sink, mut stream) = ws.split();
                    loop {
                        tokio::select! {
                            cmd = rx.recv() => match cmd {
                                Some(Cmd::Call { id, method, params }) => {
                                    let req = json!({
                                        "jsonrpc": "2.0", "id": id,
                                        "method": method, "params": params,
                                    });
                                    if let Err(e) = sink.send(
                                        Message::Text(req.to_string())).await
                                    {
                                        fail_one(&self.pending, id,
                                                 &format!("发送失败: {e}"));
                                        break;
                                    }
                                }
                                None => return,          // 壳退出
                            },
                            incoming = stream.next() => match incoming {
                                Some(Ok(Message::Text(text))) => {
                                    handle_message(&self.app, &self.pending, &text);
                                }
                                Some(Ok(Message::Binary(_)))
                                | Some(Ok(Message::Ping(_)))
                                | Some(Ok(Message::Pong(_)))
                                | Some(Ok(Message::Frame(_))) => {}
                                Some(Ok(Message::Close(_))) | None => break,
                                Some(Err(e)) => {
                                    engine::log_line(&self.app,
                                                     &format!("[shell] RPC 读取异常: {e}"));
                                    break;
                                }
                            },
                        }
                    }
                    self.connected.store(false, Ordering::SeqCst);
                    let _ = self.disconnect.send(());
                    fail_all_pending(&self.pending);
                    engine::log_line(&self.app, "[shell] 引擎 RPC 连接断开，准备重连");
                }
                Err(e) => {
                    // 引擎尚未起来属正常（sidecar 启动需要时间）：首次记一条，
                    // 之后按 20:1 限频 —— 500ms 一条会把 500 行环形缓冲整段冲掉，
                    // 用户打开日志抽屉就再也看不到 sidecar 的真实输出了
                    waits += 1;
                    if waits == 1 || waits % 20 == 0 {
                        engine::log_line(&self.app,
                                         &format!("[shell] 等待引擎 RPC（已等 {waits} 次）: {e}"));
                    }
                }
            }
            tokio::time::sleep(Duration::from_millis(RECONNECT_MS)).await;
            if self.connected.load(Ordering::SeqCst) {
                waits = 0;      // 连上过就重新计数：下次断线首条日志要能出来
            }
        }
    }
}

/// 处理引擎下行消息：通知 -> Tauri 事件；有 id -> 回填在途请求。
fn handle_message(app: &AppHandle, pending: &Pending, text: &str) {
    let value: Value = match serde_json::from_str(text) {
        Ok(v) => v,
        Err(e) => {
            engine::log_line(app, &format!("[shell] 引擎消息非法 JSON: {e}"));
            return;
        }
    };

    if let Some(name) = value.get("method").and_then(Value::as_str) {
        let payload = value.get("params").cloned().unwrap_or(Value::Null);
        let _ = app.emit(&format!("{EVENT_PREFIX}{name}"), payload);
        return;
    }

    let Some(id) = value.get("id").and_then(Value::as_u64) else {
        return;
    };
    let Some(tx) = pending.lock().unwrap_or_else(|p| p.into_inner()).remove(&id) else {
        return;
    };
    let result = match value.get("error") {
        Some(err) => {
            let msg = err
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("引擎返回未知错误");
            let code = err.get("code").and_then(Value::as_i64).unwrap_or(0);
            Err(format!("[{code}] {msg}"))
        }
        None => Ok(value.get("result").cloned().unwrap_or(Value::Null)),
    };
    let _ = tx.send(result);
}

fn fail_one(pending: &Pending, id: u64, msg: &str) {
    if let Some(tx) = pending.lock().unwrap_or_else(|p| p.into_inner()).remove(&id) {
        let _ = tx.send(Err(msg.to_string()));
    }
}

fn fail_all_pending(pending: &Pending) {
    for (_, tx) in pending.lock().unwrap_or_else(|p| p.into_inner()).drain() {
        let _ = tx.send(Err("引擎连接已断开".into()));
    }
}
