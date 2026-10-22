//! Tauri 构建脚本。
//!
//! 支持构建**单文件绿色版**：环境变量 `TESTPILOT_EMBED_ENGINE` 指向一个
//! PyInstaller onefile 引擎（如 `dist/Code.exe`，由 scripts/build-single.ps1 注入），
//! 存在时：
//! - 定义 cfg `embedded_engine`（engine.rs 据此编译 `include_bytes!` 嵌入）；
//! - 通过 rustc-env 把**绝对路径**（正斜杠）交给 `include_bytes!`（Windows
//!   反斜杠在宏里会出问题，统一转写）；
//! - `rerun-if-changed` 挂在引擎文件上：引擎更新后壳自动重编。
//!
//! 未设置该变量时（默认 build-shell.ps1 / cargo test / tauri dev），不嵌入任何
//! 引擎，行为与 onedir resources 形态完全一致。

use std::path::Path;

fn main() {
    println!("cargo:rerun-if-env-changed=TESTPILOT_EMBED_ENGINE");
    println!("cargo:rustc-check-cfg=cfg(embedded_engine)");

    if let Ok(path) = std::env::var("TESTPILOT_EMBED_ENGINE") {
        let p = Path::new(&path);
        if !p.is_file() {
            panic!("TESTPILOT_EMBED_ENGINE 指向的文件不存在: {path}");
        }
        println!("cargo:rustc-env=TESTPILOT_EMBED_ENGINE_PATH={}", path.replace('\\', "/"));
        println!("cargo:rerun-if-changed={}", p.display());
        println!("cargo:rustc-cfg=embedded_engine");
    }

    tauri_build::build()
}
