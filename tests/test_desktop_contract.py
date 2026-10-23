"""壳 ↔ 引擎跨语言契约门禁（M4-1/M4-2/M4-3）。

前端（TypeScript）与壳（Rust）都不在 pytest 的编译范围内，一旦两侧漂移，
问题要到运行时才暴露（方法名拼错 -> 静默 404；命令名改错 -> 点击无响应）。
这里把"必须一致"的四处写成断言：

1. ``bridge.ts`` 的 ``RPC_METHODS`` ≡ ``RpcService.methods()``；
2. ``bridge.ts`` 的 ``RPC_EVENTS`` ≡ ``core/events.py`` 里领域事件名；
3. ``bridge.ts`` 里 ``invoke(...)`` 用到的壳命令 ≡ ``src-tauri/src/main.rs`` 注册的命令；
4. ``tauri.conf.json`` 的 ``externalBin`` 名 ≡ Rust 侧 ``SIDECAR_BIN``。
"""
import json
import re
import tomllib
from pathlib import Path

import pytest

from testpilot.core import events as core_events
from testpilot.rpc import RpcService

DESKTOP = Path(__file__).resolve().parents[1] / "testpilot" / "app" / "desktop"
BRIDGE = DESKTOP / "src" / "bridge.ts"
MAIN_RS = DESKTOP / "src-tauri" / "src" / "main.rs"
TS_TYPES = DESKTOP / "src" / "types.ts"
ROOT = Path(__file__).resolve().parents[1]
ENGINE_RS = DESKTOP / "src-tauri" / "src" / "engine.rs"
TAURI_CONF = DESKTOP / "src-tauri" / "tauri.conf.json"
CARGO_TOML = DESKTOP / "src-tauri" / "Cargo.toml"
CARGO_LOCK = DESKTOP / "src-tauri" / "Cargo.lock"
PACKAGE_JSON = DESKTOP / "package.json"
PYPROJECT = ROOT / "pyproject.toml"

pytestmark = pytest.mark.skipif(not BRIDGE.exists(),
                                reason="桌面壳源码不在（仅引擎仓部署）")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _ts_string_array(source: str, name: str) -> list[str]:
    """取出 ``export const NAME = [ ... ] as const;`` 里的字符串字面量。"""
    match = re.search(rf"export const {name} = \[(.*?)\]\s*as const;", source, re.S)
    assert match, f"bridge.ts 中找不到 {name} 数组"
    return re.findall(r"'([^']+)'", match.group(1))


def test_release_versions_are_consistent():
    """发布版本必须在 Python / npm / Tauri / Cargo 四个入口保持一致。"""
    py_version = tomllib.loads(_read(PYPROJECT))["project"]["version"]
    package_version = json.loads(_read(PACKAGE_JSON))["version"]
    tauri_version = json.loads(_read(TAURI_CONF))["version"]

    cargo_text = _read(CARGO_TOML)
    package_block = cargo_text.split("[package]", 1)[1].split("\n[", 1)[0]
    cargo_match = re.search(r'^version\s*=\s*"([^"]+)"', package_block, re.M)
    assert cargo_match, "Cargo.toml [package] 未声明 version"
    cargo_version = cargo_match.group(1)

    lock_match = re.search(
        r'\[\[package\]\]\s*\nname = "testpilot-shell"\s*\nversion = "([^"]+)"',
        _read(CARGO_LOCK),
    )
    assert lock_match, "Cargo.lock 未找到 testpilot-shell"
    lock_version = lock_match.group(1)

    versions = {
        "pyproject": py_version,
        "package.json": package_version,
        "tauri.conf": tauri_version,
        "Cargo.toml": cargo_version,
        "Cargo.lock": lock_version,
    }
    assert len(set(versions.values())) == 1, f"版本号漂移: {versions}"
    assert re.fullmatch(r"\d+\.\d+\.\d+", py_version), f"版本号不是 SemVer: {py_version}"


def test_tauri_js_and_rust_minor_versions_match():
    """Tauri CLI 会拒绝 JS API 与 Rust crate 跨 minor 漂移，必须在干净机也可重现构建。"""
    package = json.loads(_read(PACKAGE_JSON))
    api_version = package["dependencies"]["@tauri-apps/api"].lstrip("^~")
    cli_version = package["devDependencies"]["@tauri-apps/cli"].lstrip("^~")

    lock_text = _read(CARGO_LOCK)
    rust_match = re.search(
        r'\[\[package\]\]\s*\nname = "tauri"\s*\nversion = "([^"]+)"',
        lock_text,
    )
    assert rust_match, "Cargo.lock 未找到 tauri crate"
    rust_version = rust_match.group(1)

    def minor(v: str) -> tuple[int, int]:
        m = re.match(r"^(\d+)\.(\d+)\.", v)
        assert m, f"非法 Tauri 版本: {v}"
        return int(m.group(1)), int(m.group(2))

    assert minor(api_version) == minor(rust_version), (
        f"@tauri-apps/api={api_version} 与 tauri={rust_version} minor 不一致")
    assert minor(cli_version) == minor(rust_version), (
        f"@tauri-apps/cli={cli_version} 与 tauri={rust_version} minor 不一致")


def test_step_editor_supports_extended_computer_use_actions():
    source = _read(DESKTOP / "src" / "components" / "StepEditor.tsx")
    for action in ("ui.right_click", "ui.scroll", "ui.drag"):
        assert action in source
    assert 'aria-label="滚动方向"' in source
    assert 'aria-label="拖拽 X 偏移"' in source
    assert 'aria-label="拖拽 Y 偏移"' in source


def test_frontend_rpc_methods_match_engine():
    decl = set(_ts_string_array(_read(BRIDGE), "RPC_METHODS"))
    engine = set(RpcService(None).methods())
    assert decl == engine, (
        f"前端独有: {sorted(decl - engine)}；引擎独有: {sorted(engine - decl)}")
    assert len(engine) >= 30


def test_frontend_events_match_domain_events():
    declared = set(_ts_string_array(_read(BRIDGE), "RPC_EVENTS"))
    domain = {
        cls.__dataclass_fields__["event"].default
        for cls in (core_events.StepFinished, core_events.CaseFinished,
                    core_events.RunFinished)
    }
    assert declared == domain


def test_frontend_invoke_names_are_registered_in_shell():
    used = set(re.findall(r"invoke(?:<[^>]*>)?\('([a-z_]+)'", _read(BRIDGE)))
    assert used, "bridge.ts 未发现任何 invoke 调用"
    registered = set(re.findall(r"^\s+([a-z_]+),$", _read(MAIN_RS), re.M))
    missing = used - registered
    assert not missing, f"壳未注册这些命令: {sorted(missing)}（generate_handler! 漏了）"


def test_health_paths_keys_are_consumed_everywhere():
    """`health.paths` 的键名是跨语言契约（引擎产出 → 前端注册 → Rust 白名单）。

    这条门禁的由来：引擎**从未返回** `paths`，而前端/Rust 早就写好了消费逻辑
    （`bridge.ts` 注册根目录、`RunDetailPage` 读截图与报告、`SettingsPage` 显示路径）。
    契约门禁此前只比对 RPC 方法名与事件名，比对不到"结构体里的字段"，
    于是这条链路断了很久却全绿 —— 表现为截图永久空白、报告按钮点了没反应。
    """
    orch_src = _read(ROOT / "testpilot" / "engine" / "orchestrator.py")
    assert '"paths": self.resource_paths()' in orch_src, \
        "health() 未暴露 paths —— 壳注册不了资源根目录，截图/报告全部读不出来"

    # 引擎实际产出的键
    m = re.search(r'def resource_paths\(self\).*?for key in \(([^)]+)\)', orch_src, re.S)
    assert m, "orchestrator.resource_paths 的键列表未找到（改名了？同步更新本门禁）"
    produced = set(re.findall(r'"([a-z_]+)"', m.group(1)))

    # 前端消费的键（bridge.ts 的 bootstrapRoots + types.ts 的声明）
    bridge = _read(BRIDGE)
    consumed = set(re.findall(r"paths\.([a-z_]+)", bridge))
    assert consumed, "bridge.ts 未消费任何 paths 键"
    types = _read(TS_TYPES)
    tm = re.search(r"paths\?: \{([^}]+)\}", types)
    assert tm, "types.ts 未声明 health.paths"
    declared = set(re.findall(r"([a-z_]+)\??:", tm.group(1)))

    assert consumed <= produced, \
        f"前端读取了引擎不产的键（恒 undefined）: {sorted(consumed - produced)}"
    assert declared <= produced, \
        f"types.ts 声明了引擎不产的键: {sorted(declared - produced)}"
    # Rust 侧把文件路径转父目录，所以 db 必须仍然下发（否则数据根授权不到）
    assert "db" in produced, "db 必须下发（Rust 侧靠它授权数据根）"

    # Rust 必须处理"根是文件"的情况，否则 db 那条白登记
    main_rs = _read(MAIN_RS)
    sync_fn = main_rs[main_rs.index("fn sync_roots"):]
    sync_fn = sync_fn[:sync_fn.index("\n}")]
    assert "is_file()" in sync_fn, \
        "sync_roots 未处理文件路径 → 传入 db 时授权不到其父目录（白登记一条）"


def test_similar_refs_fields_match_frontend():
    """相似历史对照的字段名必须两端一致。

    由来：引擎 `FailureKnowledge.as_ref()` 产出 `hits`，而 TS 声明的是 `count`，
    前端渲染 `{s.count} 次` → 界面上恒显示 `undefined 次`。
    报告侧读的是 `ref.get('hits', 0)`（正确），所以**错的是前端** —— 这条门禁
    同时钉住"两端都读同一个名字"，避免下次又改错方向。
    """
    svc = _read(ROOT / "testpilot" / "knowledge" / "service.py")
    m = re.search(r"def as_ref\(self\) -> dict:.*?return \{(.*?)\}", svc, re.S)
    assert m, "未找到 FailureKnowledge.as_ref（改名了？同步更新本门禁）"
    produced = set(re.findall(r'"([a-z_]+)":', m.group(1)))
    assert "hits" in produced, "as_ref 必须产出 hits（报告侧按此名读取）"

    # 报告侧消费
    rep = _read(ROOT / "testpilot" / "report" / "reporter.py")
    assert "ref.get('hits'" in rep, "报告侧未按 hits 读取相似历史命中次数"

    # 前端消费：similar 元素里用到的键必须都在 produced 里
    types = _read(TS_TYPES)
    tm = re.search(r"similar\?:\s*\[?\s*\{(.*?)\}", types, re.S)
    assert tm, "types.ts 未声明 RootCause.similar"
    declared = set(re.findall(r"([a-z_]+)\??:", tm.group(1)))
    assert declared <= produced, \
        f"types.ts 的 similar 声明了引擎不产的键（恒 undefined）: {sorted(declared - produced)}"

    page = _read(DESKTOP / "src" / "pages" / "RunDetailPage.tsx")
    # 只取 similar 那段 map 回调里的 s.xxx —— 页面上还有别的 `s.` 变量
    # （步骤、diff stats），全局正则会把它们混进来造成假失败。
    sm = re.search(r"root_cause\.similar!?\.map\(\s*\((\w+)\)\s*=>\s*\((.*?)\)\)\}", page, re.S)
    assert sm, "RunDetailPage 未找到 similar 的 map 回调（改名了？同步更新本门禁）"
    var, body = sm.group(1), sm.group(2)
    used = set(re.findall(rf"\b{re.escape(var)}\.([a-z_]+)", body))
    assert used, "similar 的 map 回调里没有读取任何字段"
    assert used <= produced, \
        f"RunDetailPage 读取了引擎不产的键（恒 undefined）: {sorted(used - produced)}"


def test_sidecar_packaging_is_onedir_via_resources():
    """sidecar 是 PyInstaller **onedir** 产物，只能走 resources（externalBin 只带单个文件）。

    历史上设计文档写的是 externalBin；这里把它固化成"必须有 resources + 资源目录解析"，
    避免有人照着文档改回 externalBin 造成"打包成功但运行起不来"的隐性故障。

    **glob 必须递归**（``**``）：实测 ``binaries/sidecar/*`` 只匹配到目录本身，
    Tauri 生成的 NSIS 脚本里就只剩 ``testpilot-sidecar.exe`` 一个文件，
    ``_internal/``（python313.dll、cv2.pyd、全部依赖，168 MB）静默丢失 ——
    安装后一拉引擎就崩，而构建过程**全程无报错**。
    """
    const = re.search(r'pub const SIDECAR_DIR: &str = "([^"]+)"', _read(ENGINE_RS))
    assert const, "engine.rs 未定义 SIDECAR_DIR"
    conf = json.loads(_read(TAURI_CONF))
    assert "externalBin" not in conf["bundle"], "onedir 不能用 externalBin"
    resources = conf["bundle"]["resources"]
    assert f"binaries/{const.group(1)}/**/*" in resources, (
        f"resources={resources} 必须用递归 glob `binaries/{const.group(1)}/**/*`："
        f"单层 `*` 不会带上 _internal/，产物将缺少 Python 运行时")

    engine_src = _read(ENGINE_RS)
    assert "resource_dir" in engine_src, "壳必须按资源目录解析 sidecar 路径"
    assert "TESTPILOT_SIDECAR" in engine_src, "壳需支持环境变量覆盖 sidecar 路径"


def test_sidecar_onedir_payload_is_actually_present():
    """递归 glob 真的能匹配到 onedir 的全部内容（含 _internal 与 python*.dll）。

    只断言配置字符串不够：还要确认磁盘上确实有可被匹配到的 _internal 与运行时 DLL，
    否则 `**/*` 也只是"看起来对"。
    """
    sidecar_dir = DESKTOP / "src-tauri" / "binaries" / "sidecar"
    if not (sidecar_dir / "testpilot-sidecar.exe").exists():
        pytest.skip("sidecar 尚未构建（先跑 scripts/build-sidecar.ps1）")

    internal = sidecar_dir / "_internal"
    assert internal.is_dir(), (
        "onedir 产物缺少 _internal/（Python 运行时与依赖都在这里）")
    dlls = {p.name.lower() for p in internal.glob("python3*.dll")}
    assert dlls, f"_internal/ 里没有 python3*.dll，产物不完整：{dlls}"
    assert (internal / "cv2").is_dir() or (internal / "cv2.pyd").exists(), \
        "视觉依赖 cv2 不在产物里"
    assert any(internal.glob("testpilot*")), "引擎自身代码不在 _internal/ 里"


def test_shell_redirects_writable_data_to_user_dir():
    """安装到 Program Files 后 cwd 只读：壳必须给 sidecar 指定可写数据根。

    否则打包出来的应用会在"建库/写日志"时失败（install 目录不允许写），
    而这类问题在开发态（cwd = 源码目录）永远复现不出来。
    """
    engine_src = _read(ENGINE_RS)
    assert "app_data_dir" in engine_src, "壳需用 Tauri 的用户数据目录作为数据根"
    assert "current_dir" in engine_src, "壳需把 sidecar 的 cwd 设到可写目录"
    assert "TESTPILOT_HOME" in engine_src, "壳需把数据根显式传给引擎"

    # 引擎侧必须真的消费这个变量（只设不读等于没做）
    settings = _read(Path(__file__).resolve().parents[1]
                     / "testpilot" / "config" / "settings.py")
    assert "TESTPILOT_HOME" in settings, "引擎未消费 TESTPILOT_HOME"
    assert "apply_home_base" in settings, "引擎未重定位相对产出路径"


def test_tauri_config_frontend_dist_and_permissions_present():
    conf = json.loads(_read(TAURI_CONF))
    assert conf["build"]["frontendDist"] == "../dist"
    assert conf["app"]["security"]["csp"]        # 生产环境必须给出 CSP
    caps = json.loads(_read(DESKTOP / "src-tauri" / "capabilities" / "default.json"))
    assert caps["permissions"] == ["core:default"], (
        "前端不应获得 shell 等能力：sidecar 由 Rust 侧启动，业务全部走自定义命令")


# PyInstaller 只收集 .py/.pyd/DLL，非 Python 资源必须显式列进 datas。
# 漏一处就是安装版一启动就 FileNotFoundError，且开发态永远复现不出来。
def test_sidecar_spec_ships_runtime_yaml():
    """`default.yaml` 缺失 => 安装版引擎连配置都读不到（实测 FileNotFoundError）。"""
    spec = _read(Path(__file__).resolve().parents[1] / "packaging" / "sidecar.spec")
    assert "default.yaml" in spec, "spec 的 datas 未带上 default.yaml"
    assert "testpilot/config" in spec, "default.yaml 的目标子目录必须是 testpilot/config"
    assert "datas=_DATAS" in spec or "datas=[" in spec, "Analysis 未消费 _DATAS"


def test_runtime_resource_paths_are_all_shipped():
    """引擎里所有 ``Path(__file__).parent / <文件>`` 的读取点，都必须在 spec 的白名单里。

    这是一条**反向门禁**：将来有人再加一个"随包资源"，忘了更新 spec 时，
    这里会先报错，而不是等到实机联调才发现。
    """
    root = Path(__file__).resolve().parents[1]
    spec = _read(root / "packaging" / "sidecar.spec")
    pattern = re.compile(r'Path\(__file__\)\.parent\s*/\s*"([^"]+)"')
    shipped: list[str] = []
    for py in sorted((root / "testpilot").rglob("*.py")):
        for name in pattern.findall(_read(py)):
            shipped.append(name)
            assert name in spec, (
                f"{py.relative_to(root)} 运行期读取 {name}，但 packaging/sidecar.spec "
                f"的 datas 没有它 —— 打包后必 FileNotFoundError")
    assert shipped, "未发现任何随包资源读取点（正则失配？）"


# 三处清单各自独立维护，任一漂移都会造成"某个驱动/资源在安装版里必炸"。
# 实测教训：tools/check_bundle.py 曾与 spec 分家，spec 补齐了资源而自检没查，
# 于是"自检通过"掩盖了资源缺失。这里做三方交叉校验。
def test_hidden_import_lists_agree_everywhere():
    """spec 的 _REQUIRED、自检的 REQUIRED_MODULES、构建脚本的 $hidden 必须一致。"""
    root = Path(__file__).resolve().parents[1]
    spec = _read(root / "packaging" / "sidecar.spec")
    bundle = _read(root / "tools" / "check_bundle.py")
    build = _read(root / "scripts" / "build-sidecar.ps1")

    def quoted(text: str) -> set[str]:
        return {m for m in re.findall(r"""['"]([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)['"]""", text)
                if "." in m}

    def spec_required() -> set[str]:
        block = spec[spec.index("_REQUIRED = ["):]
        block = block[:block.index("]")]
        return set(re.findall(r"'([^']+)'", block))

    def py_list(text: str, name: str) -> set[str]:
        # 兼容两种字面量：REQUIRED_MODULES = [ ... ] 与 OPTIONAL_MODULES = { ... }
        idx = text.index(f"{name} = ")
        open_at = min(i for i in (text.find("[", idx), text.find("{", idx)) if i != -1)
        close = "]" if text[open_at] == "[" else "}"
        block = text[open_at:text.index(close, open_at)]
        return set(re.findall(r'"([^"]+)"', block))

    def ps_list(text: str) -> set[str]:
        # $hidden = @( 'a', 'b', ... )
        start = text.index("$hidden")
        start = text.index("(", start)
        block = text[start:text.index(")", start)]
        return set(re.findall(r"'([^']+)'", block))

    a, b, c = spec_required(), py_list(bundle, "REQUIRED_MODULES"), ps_list(build)
    optional = py_list(bundle, "OPTIONAL_MODULES")
    # 自检把 PIL 划为**可选**（打包机没装 Pillow 就不该要求进包），
    # 而 spec 里 PIL 在 _REQUIRED 中——这个差集必须恰好等于 OPTIONAL_MODULES，
    # 不能是别的（否则说明有人悄悄删了某个必进包模块）。
    only_spec = a - b
    assert only_spec == optional, (
        f"spec 比自检多出的模块必须恰好是 OPTIONAL_MODULES：多出 {sorted(only_spec - optional)}，"
        f"缺少 {sorted(optional - only_spec)}")
    assert b - a == set(), f"自检要求进包但 spec 没有（必 MISS）：{sorted(b - a)}"
    assert a - c == set(), f"spec 有但 build-sidecar 脚本未 hidden 声明：{sorted(a - c)}"
    assert c - a == set(), f"build-sidecar 脚本多声明（两处会漂移）：{sorted(c - a)}"
    # 可选模块也必须真的在 spec 清单里（否则打包机装了 Pillow 也不会被收集）
    assert optional <= a, f"OPTIONAL_MODULES 不在 spec._REQUIRED 中：{sorted(optional - a)}"


def test_bundle_check_expects_shipped_datas():
    """自检必须核对随包资源，否则 spec 补齐了资源而自检"通过"会误导发布。"""
    root = Path(__file__).resolve().parents[1]
    bundle = _read(root / "tools" / "check_bundle.py")
    assert "REQUIRED_DATA" in bundle and "_check_datas" in bundle, \
        "tools/check_bundle.py 未核对非 Python 资源（_DATAS）"
    assert "default.yaml" in bundle, "自检未覆盖 spec 的 _DATAS 实际内容"



def test_sidecar_spec_ships_pi_rpc_agent_extension():
    """安装版 Agent 仍必须只有 RPC proxy tools，扩展文件不能漏出 sidecar。"""
    root = Path(__file__).resolve().parents[1]
    spec = (root / "packaging" / "sidecar.spec").read_text(encoding="utf-8")
    build = (root / "scripts" / "build-sidecar.ps1").read_text(
        encoding="utf-8-sig")
    assert "pi_rpc_tools.mjs" in spec
    assert "testpilot/agent" in spec
    assert "pi_rpc_tools.mjs;testpilot/agent" in build.replace("\\", "/")
