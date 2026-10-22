//! 壳行为配置：从随包 `default.yaml` 读取 `desktop.sidecar` 三项。
//!
//! **为什么不引入 YAML 依赖**：本工程只需要 3 个标量（`supervised` / `restart_max` /
//! `health_interval_s`），为它拉进 `serde_yaml`（已停止维护）不划算，且会让
//! release 构建再增依赖树。这里用最小解析：按缩进定位 `desktop.sidecar` 段，
//! 只认 `键: 值` 形式，遇到不认识的行就跳过。
//!
//! **为什么壳要自己读、而不是让引擎通过 RPC 下发**：`restart_max` 在**引擎启动之前**
//! 就要用（决定崩溃后还重不重启），那时 RPC 还没建立。所以必须在拉起 sidecar 前读到。
//!
//! 配置文件位置：随 sidecar 一起打包，位于资源目录下
//! `sidecar/_internal/testpilot/config/default.yaml`（开发态则在源码树里）。
//! 用户覆盖层（`config.override.yaml`）**不参与** —— `desktop.sidecar.*` 不在引擎的
//! `EDITABLE` 列表里，设置页改不到它，所以不存在"壳读到旧值"的问题。

use std::path::{Path, PathBuf};

use tauri::{AppHandle, Manager};

/// 壳监督行为（默认值与 `default.yaml` 保持一致）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DesktopConfig {
    /// 崩溃是否自动重启。`false` 时监督循环退出后不再拉起。
    pub supervised: bool,
    /// 自动重启次数上限。
    pub restart_max: u32,
    /// 健康检查间隔（秒）：壳按此周期探测引擎，用于状态徽章与"假死"发现。
    pub health_interval_s: u64,
}

impl Default for DesktopConfig {
    fn default() -> Self {
        Self { supervised: true, restart_max: 3, health_interval_s: 10 }
    }
}

impl DesktopConfig {
    /// 带下限保护：配置写成 0 会让监督循环疯狂重启（1ms 退避也扛不住），
    /// 健康间隔写 0 会变成忙轮询。宁可钳到安全值也不要让 UI 卡死。
    pub fn sanitized(self) -> Self {
        Self {
            supervised: self.supervised,
            restart_max: self.restart_max.min(100),
            health_interval_s: self.health_interval_s.clamp(1, 3600),
        }
    }
}

/// 候选配置文件路径（按优先级）。开发态与打包态的目录结构不同，都要覆盖。
fn candidates(app: &AppHandle) -> Vec<PathBuf> {
    let mut out = Vec::new();

    // 运维显式指定优先（与 sidecar_args 的 TESTPILOT_CONFIG 语义一致）
    if let Ok(p) = std::env::var("TESTPILOT_CONFIG") {
        if !p.trim().is_empty() {
            out.push(PathBuf::from(p));
        }
    }

    if let Ok(dir) = app.path().resource_dir() {
        // 打包态：sidecar 是 onedir 产物，依赖与数据都在 _internal 下
        out.push(dir.join("sidecar").join("_internal")
            .join("testpilot").join("config").join("default.yaml"));
        out.push(dir.join("sidecar").join("testpilot")
            .join("config").join("default.yaml"));
    }

    // 开发态（cargo/`npm run shell:dev`）：源码树里的 testpilot/config
    out.push(Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()                       // src-tauri -> desktop
        .and_then(|p| p.parent())       // desktop -> app
        .and_then(|p| p.parent())       // app -> testpilot(包)
        .map(|p| p.join("config").join("default.yaml"))
        .unwrap_or_else(|| PathBuf::from("default.yaml")));

    out
}

/// 解析 `desktop.sidecar` 段里的三个标量。
///
/// 只认最朴素的 `键: 值`（可带 `#` 行尾注释、值可带引号）。刻意**不做**完整 YAML：
/// 支持的语法越少，越不容易在缺依赖的环境里静默解析错。
fn parse(text: &str) -> Option<DesktopConfig> {
    let mut cfg = DesktopConfig::default();
    let mut in_desktop = false;
    let mut in_sidecar = false;
    let mut seen = false;

    for raw in text.lines() {
        let line = raw.split('#').next().unwrap_or("");
        if line.trim().is_empty() {
            continue;
        }
        let indent = line.len() - line.trim_start().len();
        let body = line.trim();

        // 顶层键（缩进 0）决定是否进入 desktop 段
        if indent == 0 {
            in_desktop = body.starts_with("desktop:");
            in_sidecar = false;
            continue;
        }
        if !in_desktop {
            continue;
        }
        // 二级键（缩进 2）决定是否进入 sidecar 子段
        if indent <= 2 {
            in_sidecar = body.starts_with("sidecar:");
            continue;
        }
        if !in_sidecar {
            continue;
        }
        let Some((key, value)) = body.split_once(':') else { continue };
        let key = key.trim();
        // 去掉引号与空白（YAML 里 `true` / `"true"` 都常见）
        let value = value.trim().trim_matches(|c| c == '"' || c == '\'');
        match key {
            "supervised" => {
                if let Some(b) = as_bool(value) {
                    cfg.supervised = b;
                    seen = true;
                }
            }
            "restart_max" => {
                if let Ok(n) = value.parse::<u32>() {
                    cfg.restart_max = n;
                    seen = true;
                }
            }
            "health_interval_s" => {
                if let Ok(n) = value.parse::<u64>() {
                    cfg.health_interval_s = n;
                    seen = true;
                }
            }
            _ => {}
        }
    }
    if seen { Some(cfg) } else { None }
}

fn as_bool(v: &str) -> Option<bool> {
    match v.to_ascii_lowercase().as_str() {
        "true" | "yes" | "on" | "1" => Some(true),
        "false" | "no" | "off" | "0" => Some(false),
        _ => None,
    }
}

/// 读配置；任何一步失败都回落到默认值（**不报错** —— 壳必须能起来）。
///
/// 失败回落的代价只是"用默认的监督参数"，而中断启动的代价是"整个应用打不开"。
pub fn load(app: &AppHandle) -> DesktopConfig {
    for path in candidates(app) {
        let Ok(text) = std::fs::read_to_string(&path) else { continue };
        if let Some(cfg) = parse(&text) {
            return cfg.sanitized();
        }
    }
    DesktopConfig::default()
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = r#"
app:
  name: TestPilot
  db: ./data/testpilot.db

desktop:                   # Rust 壳行为（Tauri 侧读取）
  sidecar:
    supervised: true       # 壳监督：崩溃自动重启（指数退避，上限 3 次）
    restart_max: 3
    health_interval_s: 10

llm:
  model: "company-llm"
  max_tokens: 4096
"#;

    #[test]
    fn parses_desktop_section() {
        let cfg = parse(SAMPLE).expect("应解析出 desktop.sidecar");
        assert_eq!(cfg, DesktopConfig { supervised: true, restart_max: 3, health_interval_s: 10 });
    }

    #[test]
    fn ignores_neighbouring_sections() {
        // app.db / llm.max_tokens 也在样本里，绝不能被误读成监督参数
        let text = SAMPLE
            .replace("restart_max: 3", "restart_max: 7")
            .replace("health_interval_s: 10", "health_interval_s: 25");
        let cfg = parse(&text).unwrap();
        assert_eq!(cfg.restart_max, 7);
        assert_eq!(cfg.health_interval_s, 25);
    }

    #[test]
    fn tolerates_quotes_and_case() {
        let text = "desktop:\n  sidecar:\n    supervised: \"false\"\n    restart_max: 5\n";
        let cfg = parse(text).unwrap();
        assert!(!cfg.supervised);
        assert_eq!(cfg.restart_max, 5);
        assert_eq!(cfg.health_interval_s, 10, "未给的键应保留默认");
    }

    #[test]
    fn returns_none_when_section_absent() {
        assert!(parse("app:\n  name: x\n").is_none());
        assert!(parse("").is_none());
        // 有 desktop 但没有 sidecar 子段
        assert!(parse("desktop:\n  other: 1\n").is_none());
    }

    #[test]
    fn clamps_dangerous_values() {
        // restart_max=0 + 健康间隔 0 会让监督循环忙轮询
        let cfg = DesktopConfig { supervised: true, restart_max: 0, health_interval_s: 0 }
            .sanitized();
        assert_eq!(cfg.restart_max, 0, "0 是合法语义（不重启），不该被改成别的数");
        assert_eq!(cfg.health_interval_s, 1, "健康间隔 0 → 忙轮询，必须钳住");
        let hi = DesktopConfig { supervised: true, restart_max: 9999, health_interval_s: 99999 }
            .sanitized();
        assert_eq!(hi.restart_max, 100);
        assert_eq!(hi.health_interval_s, 3600);
    }

    #[test]
    fn malformed_lines_do_not_abort_parsing() {
        // 坏行（缺冒号、值非法）应被跳过，其余键仍生效
        let text = "desktop:\n  sidecar:\n    restart_max\n    health_interval_s: abc\n    supervised: false\n";
        let cfg = parse(text).unwrap();
        assert!(!cfg.supervised);
        assert_eq!(cfg.restart_max, 3, "非法值应保留默认");
        assert_eq!(cfg.health_interval_s, 10);
    }
}
