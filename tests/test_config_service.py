"""配置读写服务单测 —— 覆盖层语义、逐项校验、**防止死配置**。

这个文件的重点是三条不变量：

1. **可编辑字段必须真实存在于 default.yaml**：设置页能改、引擎却没人读 = 假配置
   （比"缺配置"更危险，因为用户以为生效了）；
2. **覆盖层只存差异**：写回默认值不应留在文件里，否则用户改回默认后文件仍"脏"，
   升级时默认值演进会被旧值悄悄压住；
3. **非法值必须被挡住**：写进去不会立刻报错，而是下次运行时变成莫名其妙的失败。
"""
import yaml

import pytest

from testpilot.config.service import (ConfigError, ConfigService, EDITABLE, GROUPS,
                                      load_with_override)
from testpilot.config.settings import load_settings


@pytest.fixture()
def svc(cfg, tmp_path):
    return ConfigService(cfg, tmp_path / "data" / "config.override.yaml")


# ---------------------------------------------------------------- 数值防线

def test_number_field_rejects_nan(svc):
    """NaN 与任何 min/max 比较都是 False：不挡住就会整体绕过范围门禁，
    写进阈值类配置（如 vision_min_confidence）后下游判定全部失效。"""
    path = next(p for p, s in EDITABLE.items() if s["type"] == "number")
    with pytest.raises(ConfigError):
        svc.apply({path: "nan"})
    with pytest.raises(ConfigError):
        svc.apply({path: "inf"})


# ---------------------------------------------------------------- 字段规格自洽

def test_editable_keys_all_exist_in_default_yaml():
    """每个可编辑字段都必须在 default.yaml 里有对应键（否则是死配置）。"""
    base = load_settings(env={})
    missing = []
    for path in EDITABLE:
        node = base
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                missing.append(path)
                break
            node = node[part]
    assert missing == [], f"这些可编辑项在内置配置里不存在：{missing}"


def test_groups_cover_all_editable_keys_exactly_once():
    """分组必须与字段表一一对应：漏分组会让某些配置在页面上永远看不到。"""
    grouped: list[str] = []
    for _, _, paths in GROUPS:
        grouped.extend(paths)
    assert sorted(grouped) == sorted(EDITABLE), (
        f"未分组: {sorted(set(EDITABLE) - set(grouped))}；"
        f"分组里有不存在的键: {sorted(set(grouped) - set(EDITABLE))}")
    assert len(grouped) == len(set(grouped)), "同一个字段被分到多个组"


def test_no_secret_field_is_editable():
    """密钥样字段名必须显式声明 type="secret" 才能进可编辑表。

    type="secret" 的字段（禅道账密等本地凭据）：脱敏展示 + 可写入覆盖层；
    其余密钥样命名仍然禁止。用子串匹配会误伤 `llm.max_tokens`（含 "token"
    但不是密钥）—— 所以判据是 `SECRET_LIKE` 里的精确名，并钉住 max_tokens。
    """
    from testpilot.config.service import SECRET_LIKE

    untyped = [p for p in EDITABLE
               if any(s in p for s in SECRET_LIKE)
               and EDITABLE[p].get("type") != "secret"]
    assert untyped == [], f"密钥样字段未声明 secret 类型（会被 apply 拒绝）：{untyped}"
    assert "llm.max_tokens" in EDITABLE, \
        "llm.max_tokens 被密钥规则误伤（它只是 token 上限，不是密钥）"


def test_defaults_match_shipped_yaml():
    """`default` 必须与 default.yaml 的实际值一致。

    否则"恢复默认"会把值写成一个与出厂不同、且用户无从察觉的数
    （如阈值 0.5 vs 0.86）。
    """
    base = load_settings(env={})
    wrong = []
    for path, spec in EDITABLE.items():
        node = base
        for part in path.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if node != spec.get("default"):
            wrong.append(f"{path}: spec={spec.get('default')!r} yaml={node!r}")
    assert wrong == [], "字段默认值与 default.yaml 不一致：\n" + "\n".join(wrong)


# ---------------------------------------------------------------- 读

def test_describe_lists_groups_with_effective_values(svc):
    out = svc.describe()
    assert out["exists"] is False
    groups = [g["group"] for g in out["groups"]]
    assert groups == [g[0] for g in GROUPS]
    field = _find(out, "driver.mode")
    assert field["value"] == "auto"          # 无覆盖 -> 默认值
    assert field["overridden"] is False
    assert field["enum"]                     # 下拉框需要枚举


def test_broken_override_file_does_not_break_settings_page(svc):
    """覆盖层被写坏时按"无覆盖"处理：设置页仍要能打开（否则用户无法自救）。"""
    svc.path.parent.mkdir(parents=True, exist_ok=True)
    svc.path.write_text(": : not yaml [", encoding="utf-8")
    assert svc.override() == {}
    assert _find(svc.describe(), "driver.mode")["value"] == "auto"


# ---------------------------------------------------------------- 写

def test_apply_writes_override_and_reports_restart(svc):
    out = svc.apply({"driver.mode": "vision_first", "uia.uia_timeout_ms": 5000})
    assert out["restart_required"] is True
    assert out["written"] == 2
    assert svc.path.exists()

    data = yaml.safe_load(svc.path.read_text(encoding="utf-8"))
    assert data["driver"]["mode"] == "vision_first"
    assert data["uia"]["uia_timeout_ms"] == 5000

    field = _find(svc.describe(), "driver.mode")
    assert field["overridden"] is True
    assert field["value"] == "vision_first"


def test_writing_default_value_clears_override(svc):
    """改回默认值不应在覆盖层留残留（否则升级时默认演进被旧值压住）。"""
    svc.apply({"driver.mode": "vision_first"})
    assert _find(svc.describe(), "driver.mode")["overridden"] is True

    svc.apply({"driver.mode": "auto"})            # auto 就是默认值
    assert _find(svc.describe(), "driver.mode")["overridden"] is False
    # 覆盖层里最后一项被清掉后，文件应被删除（而不是留个空壳让 UI 以为"还改过"）
    assert not svc.path.exists(), "清空最后一项后覆盖层文件仍然存在"
    assert svc.describe()["exists"] is False


def test_empty_string_clears_override(svc):
    """空串语义 = 清除该项覆盖（而不是写入空字符串）。"""
    svc.apply({"zentao.base_url": "http://z.example.com"})
    assert _find(svc.describe(), "zentao.base_url")["overridden"] is True
    out = svc.apply({"zentao.base_url": ""})
    assert out["cleared"] == 1
    assert _find(svc.describe(), "zentao.base_url")["overridden"] is False
    assert not svc.path.exists(), "清空最后一项后覆盖层文件仍然存在"


def test_reset_removes_all_overrides(svc):
    svc.apply({"driver.mode": "uia_only", "llm.temperature": 0.9})
    assert _find(svc.describe(), "driver.mode")["overridden"] is True
    out = svc.apply({}, reset=True)
    assert out["overridden"] == 0
    assert _find(svc.describe(), "driver.mode")["overridden"] is False
    assert _find(svc.describe(), "llm.temperature")["overridden"] is False


# ---------------------------------------------------------------- 校验

@pytest.mark.parametrize("path,raw,keyword", [
    ("driver.vision_min_confidence", 1.5, "不能大于"),
    ("driver.vision_min_confidence", -0.1, "不能小于"),
    ("uia.uia_timeout_ms", 10, "不能小于"),
    ("driver.mode", "telepathy", "只能是"),
    ("log.level", "TRACE", "只能是"),
    ("llm.max_tokens", "abc", "需要整数"),
    ("driver.retry_on_failure", "maybe", "需要 true/false"),
])
def test_invalid_values_rejected(svc, path, raw, keyword):
    with pytest.raises(ConfigError) as e:
        svc.apply({path: raw})
    assert keyword in str(e.value)
    assert not svc.path.exists(), "校验失败时不得落盘"


def test_unknown_key_rejected(svc):
    with pytest.raises(ConfigError) as e:
        svc.apply({"app.db": "/tmp/evil.db"})
    assert "不可编辑" in str(e.value)


def test_secret_like_unknown_key_rejected(svc):
    """不在 EDITABLE 的密钥样键（如 api_key_env）依旧进不来。"""
    with pytest.raises(ConfigError) as e:
        svc.apply({"llm.api_key_env": "MY_KEY"})
    assert "不可编辑" in str(e.value)


def test_zentao_credentials_writable_but_never_echoed(svc):
    """禅道账密：可写入覆盖层，但 describe **永不回显真实值**（只给 has_value）。"""
    svc.apply({"zentao.account": "zhangsan", "zentao.password": "pw1"})
    acc = _find(svc.describe(), "zentao.account")
    pwd = _find(svc.describe(), "zentao.password")
    assert acc["value"] == "" and pwd["value"] == "", "secret 值不得回显"
    assert acc["has_value"] is True and pwd["has_value"] is True
    assert acc["overridden"] is True and pwd["overridden"] is True
    # 磁盘上确实写了（这是"本地凭据可界面配置"的既定取舍）
    data = yaml.safe_load(svc.path.read_text(encoding="utf-8"))
    assert data["zentao"]["password"] == "pw1"


def test_zentao_credentials_merge_into_engine_cfg(svc, cfg):
    """写入的凭据经 load_with_override 合并进引擎配置（ZentaoHttp 的读取点）。

    注意显式传覆盖层路径：svc 的覆盖层在 tmp_path（生产链路由 load_settings
    按数据根自动定位同一文件，见 test_settings 的 TESTPILOT_HOME 用例）。
    """
    svc.apply({"zentao.password": "pw1"})
    merged = load_with_override(dict(cfg), svc.path)
    assert merged["zentao"]["password"] == "pw1"


def test_secret_empty_value_clears_override(svc):
    """secret 清空提交 = 清除该覆盖项（回到默认/环境变量回落）。"""
    svc.apply({"zentao.password": "pw1"})
    svc.apply({"zentao.password": ""})
    pwd = _find(svc.describe(), "zentao.password")
    assert pwd["value"] == "" and pwd["has_value"] is False
    assert pwd["overridden"] is False


def test_boolean_accepts_string_from_form(svc):
    """表单/命令行传字符串 'true' 也要能落成真正的布尔值（YAML 里不能是字符串）。"""
    svc.apply({"vision.learn_from_vlm": "false"})
    data = yaml.safe_load(svc.path.read_text(encoding="utf-8"))
    assert data["vision"]["learn_from_vlm"] is False


# ---------------------------------------------------------------- 合并

def test_load_with_override_merges_only_editable_keys(cfg, tmp_path):
    """覆盖层里的未知键不得影响配置（它是用户可写文件，不能变成改任意配置的后门）。"""
    path = tmp_path / "override.yaml"
    path.write_text(yaml.safe_dump({
        "driver": {"mode": "uia_only"},
        "app": {"db": "/tmp/evil.db", "screenshot_root": "/tmp/evil-shots"},
    }, allow_unicode=True), encoding="utf-8")

    merged = load_with_override(dict(cfg), path)
    assert merged["driver"]["mode"] == "uia_only"
    assert merged["app"]["db"] == cfg["app"]["db"], "app.db 不可被覆盖层改动"


def test_load_with_override_ignores_invalid_entry(cfg, tmp_path):
    """一项非法不该让整个引擎起不来：忽略该项 + 记日志，其余照常生效。"""
    path = tmp_path / "override.yaml"
    path.write_text(yaml.safe_dump({
        "driver": {"mode": "telepathy"},
        "llm": {"temperature": 0.5},
    }, allow_unicode=True), encoding="utf-8")

    merged = load_with_override(dict(cfg), path)
    assert merged["driver"]["mode"] == cfg["driver"]["mode"]     # 非法项被忽略
    assert merged["llm"]["temperature"] == 0.5                   # 合法项生效


def test_load_settings_can_skip_override(monkeypatch, tmp_path):
    """`TESTPILOT_NO_OVERRIDE=1` 跳过覆盖层（排障时用来确认配置本身是否正常）。"""
    home = tmp_path / "home"
    monkeypatch.setenv("TESTPILOT_HOME", str(home))
    cfg1 = load_settings(env={"TESTPILOT_HOME": str(home)})
    svc = ConfigService(cfg1)
    svc.apply({"driver.mode": "uia_only"})

    with_override = load_settings(env={"TESTPILOT_HOME": str(home)})
    assert with_override["driver"]["mode"] == "uia_only"

    without = load_settings(env={"TESTPILOT_HOME": str(home),
                                 "TESTPILOT_NO_OVERRIDE": "1"})
    assert without["driver"]["mode"] == "auto"


def _find(described: dict, path: str) -> dict:
    for group in described["groups"]:
        for field in group["fields"]:
            if field["path"] == path:
                return field
    raise AssertionError(f"字段未出现在 describe 结果里：{path}")
