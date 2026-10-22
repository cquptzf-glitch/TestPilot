# sidecar 资源目录（构建产物投放点）

本目录由 `scripts/build-sidecar.ps1` 投放 **PyInstaller onedir** 产物：

```
testpilot-sidecar.exe      引擎主程序（壳按行读 stdout/stderr 进日志抽屉）
_internal/                 依赖与 Python 运行时（**必须一起投放**）
```

## 为什么必须有这个 README.md

`tauri.conf.json` 的 `bundle.resources` 是 `binaries/sidecar/**/*`，Tauri 要求
**通配符至少匹配到一个文件**，否则构建直接报
`glob pattern ... path not found` 并中断。构建产物不入库，所以用这个文件占位，
保证"干净 checkout 也能构建"。

**不要删除本文件**；`build-sidecar.ps1 -Clean` 也已特意跳过它。

## 两个已踩过的坑

1. **glob 必须递归**（`**/*`）。写成单层 `binaries/sidecar/*` 时，Tauri 生成的
   NSIS 脚本里只会出现 `testpilot-sidecar.exe` 一个文件，**`_internal/` 静默丢失**
   （安装包从 ~180 MB 缩到 ~12 MB），安装后一拉引擎就崩，而构建全程无报错。
   回归门禁：`tests/test_desktop_contract.py::test_sidecar_packaging_is_onedir_via_resources`。
2. **不能用 `externalBin`**：它只搬单个可执行文件，onedir 的 `_internal/` 进不了包。
