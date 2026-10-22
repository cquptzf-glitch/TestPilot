"""配置加载 — yaml + 环境变量覆盖（${ENV} 占位）+ 可写数据根重定位。

**为什么需要 `TESTPILOT_HOME`**：桌面壳安装到 `Program Files` 后，进程 cwd 是**只读**的，
而 default.yaml 里的 `./data/testpilot.db`、`./logs` 都是相对路径 —— 直接跑会在
"建库/写日志"时失败（install 目录不允许写）。壳会把 `TESTPILOT_HOME` 指向
`%LOCALAPPDATA%\\TestPilot`（并把 sidecar 的 cwd 也设成那里），本模块负责把
**相对路径**重定位到该目录；绝对路径与空值一律不动（尊重运维显式配置）。
"""
import logging
import os
import re
from pathlib import Path

import yaml

log = logging.getLogger("testpilot.settings")

_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")

# 需要跟随数据根的键（(节, 键)）——都必须是"可写产出"路径
_HOME_BASED_KEYS = (
    ("app", "db"),
    ("app", "screenshot_root"),
    ("app", "report_root"),
    ("log", "dir"),
    ("vision", "template_dir"),
)


def _expand(value, env: dict[str, str]):
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: env.get(m.group(1), m.group(0)), value)
    if isinstance(value, dict):
        return {k: _expand(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, env) for v in value]
    return value


def resolve_home(env: dict[str, str] | None = None) -> Path | None:
    """数据根（`TESTPILOT_HOME`）；未设置返回 None（CLI 形态按 cwd 走）。

    ``env=None`` 表示"读进程环境"；**显式传入的字典（含空字典）一律照用**。
    不能写成 ``env or os.environ`` —— 空字典是 falsy，会静默回退到进程环境，
    让调用方"传空 env 以隔离宿主变量"的意图失效（表现为测试随宿主环境飘，
    且 shell 里残留一个 TESTPILOT_HOME 就能改变行为）。
    """
    source = os.environ if env is None else env
    raw = str(source.get("TESTPILOT_HOME") or "").strip()
    return Path(raw).expanduser() if raw else None


def apply_home_base(cfg: dict, base: Path) -> list[str]:
    """把 `_HOME_BASED_KEYS` 里的相对路径重定位到 `base`（原地修改），返回被改动的键。

    - 绝对路径：原样保留（显式配置优先）；
    - 空值 / 非字符串：跳过；
    - `base` 会被创建（缺目录时 SQLite 与日志都会失败）。
    """
    base = Path(base)
    changed: list[str] = []
    for section, key in _HOME_BASED_KEYS:
        node = cfg.get(section)
        if not isinstance(node, dict):
            continue
        value = node.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        path = Path(value)
        if path.is_absolute():
            continue
        node[key] = str(base / path)
        changed.append(f"{section}.{key}={node[key]}")
    if changed:
        base.mkdir(parents=True, exist_ok=True)
    return changed


def load_settings(path: str | Path | None = None,
                  env: dict[str, str] | None = None) -> dict:
    """加载 default.yaml + 展开 ${ENV} 占位 + 重定位数据根 + 合并用户覆盖层。

    顺序很重要：
    1. 读内置 default.yaml；
    2. 展开 ``${ENV}``（未设置的保留占位，由调用方/运维感知）；
    3. 按 ``TESTPILOT_HOME`` 先重定位一次相对产出路径
       （ConfigService 要按数据根定位覆盖层文件）；
    4. 合并用户覆盖层（``config.override.yaml``，设置页写入的差异项），
       覆盖层里的 ``${ENV}`` 在合并后再展开一次，然后补一次重定位
       —— 覆盖层新写的相对路径也要跟随数据根。

    ``TESTPILOT_NO_OVERRIDE=1`` 可跳过第 4 步（排障时用来确认配置本身是否正常）。
    """
    env = dict(os.environ) if env is None else dict(env)
    if path is None:
        path = Path(__file__).parent / "default.yaml"
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cfg = _expand(cfg, env)

    no_override = str(env.get("TESTPILOT_NO_OVERRIDE") or "").strip() \
        in ("1", "true", "yes")

    # 数据根先重定位一次：ConfigService 要按数据根定位覆盖层文件（app.db 等路径）
    base = resolve_home(env)
    if base is not None:
        changed = apply_home_base(cfg, base)
        if changed:
            log.info(f"数据根已重定位到 {base}（{'；'.join(changed)}）")

    if not no_override:
        from testpilot.config.service import load_with_override
        load_with_override(cfg)
        # 覆盖层同样支持 ${ENV}（用户照抄出厂写法不应静默失效）；
        # 已展开的值不含占位符，重复展开幂等
        cfg = _expand(cfg, env)
        if base is not None:
            # 再重定位一次：覆盖层新写的相对路径也要跟随数据根
            # （绝对路径跳过、默认值此时已是绝对路径，幂等）
            apply_home_base(cfg, base)
    # Explicit environment endpoints still override the bundled default, including an optional API key.
    if env.get("LLM_API_BASE"):
        cfg.setdefault("llm", {})["api_base"] = env["LLM_API_BASE"]
    if env.get("VLM_API_BASE"):
        cfg.setdefault("vision", {}).setdefault("vlm", {})["api_base"] = env["VLM_API_BASE"]
    if env.get("TESTPILOT_RPC_PORT"):
        try:
            port = int(env["TESTPILOT_RPC_PORT"])
        except ValueError as e:
            raise ValueError("TESTPILOT_RPC_PORT 必须是整数") from e
        if not 1 <= port <= 65535:
            raise ValueError("TESTPILOT_RPC_PORT 必须在 1..65535 范围内")
        cfg.setdefault("rpc", {})["port"] = port
    return cfg
