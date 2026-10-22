"""配置加载单测 — ${ENV} 展开 + **配置键与代码的契约门禁**（防配置漂移）。

第二个测试是有意为之的"漂移门禁"：M1 期间出现过"配置写了但没人读"（死配置）与
"代码读了但配置没有"（缺配置）两类问题，这里把最小必需键固定下来。
"""
import yaml

from testpilot.config.settings import load_settings

TOP_LEVEL = {"app", "driver", "uia", "vision", "emulator", "rpc", "desktop",
             "llm", "zentao", "log", "knowledge"}


def test_loads_packaged_default_yaml():
    cfg = load_settings()
    assert TOP_LEVEL <= set(cfg)


def test_code_required_keys_are_present():
    """代码实际读取的键必须都存在（缺失会变成运行期 KeyError）。"""
    cfg = load_settings()
    required = [
        ("app", "db"), ("app", "screenshot_root"), ("app", "report_root"),
        ("app", "test_app_version"), ("app", "test_app_exe"),
        ("driver", "mode"), ("driver", "vision_min_confidence"),
        ("driver", "retry_on_failure"),
        ("uia", "backend"), ("uia", "input_mode"), ("uia", "uia_timeout_ms"),
        ("uia", "ready_poll_ms"),
        ("vision", "template_dir"), ("vision", "template_threshold"),
        ("vision", "vision_min_confidence"), ("vision", "locate_poll_ms"),
        ("vision", "learn_from_vlm"), ("vision", "grab_backend"),
        ("emulator", "endpoint"), ("emulator", "connect_timeout_s"),
        ("emulator", "call_timeout_s"), ("emulator", "protocol_ver"),
        ("rpc", "host"), ("rpc", "port"),
        ("llm", "model"), ("llm", "api_base"), ("llm", "api_key_env"),
        ("llm", "temperature"), ("llm", "max_tokens"), ("llm", "timeout_s"),
        ("zentao", "base_url"), ("zentao", "token_env"), ("zentao", "product_id"),
        ("zentao", "sync_interval_min"), ("zentao", "create_task"),
        ("log", "level"), ("log", "dir"), ("log", "per_run_file"),
        ("knowledge", "element_stale_days"), ("knowledge", "router_fail_streak"),
        ("knowledge", "fuzzy_top_n"), ("knowledge", "similar_top_n"),
    ]
    missing = [f"{sec}.{key}" for sec, key in required
               if key not in (cfg.get(sec) or {})]
    assert missing == [], f"配置缺键: {missing}"


def test_driver_mode_is_valid_enum_value():
    from testpilot.core.dsl import DriverMode
    assert DriverMode(load_settings()["driver"]["mode"]) is DriverMode.AUTO


def test_env_placeholder_expanded_from_environ(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_API_BASE", "http://llm.internal:8000/v1")
    assert load_settings()["llm"]["api_base"] == "http://llm.internal:8000/v1"


def test_env_placeholder_kept_when_unset(monkeypatch):
    monkeypatch.delenv("LLM_API_BASE", raising=False)
    assert load_settings()["llm"]["api_base"] == "http://192.168.1.77:8989/v1"


def test_env_expansion_applies_to_nested_containers(monkeypatch, tmp_path):
    monkeypatch.setenv("FOO", "bar")
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump({
        "a": {"b": "${FOO}", "c": ["${FOO}", {"d": "${FOO}"}], "e": 1},
        "f": "${MISSING_XYZ}",
    }), encoding="utf-8")

    cfg = load_settings(str(path))
    assert cfg["a"]["b"] == "bar"
    assert cfg["a"]["c"] == ["bar", {"d": "bar"}]
    assert cfg["a"]["e"] == 1
    assert cfg["f"] == "${MISSING_XYZ}"


def test_empty_yaml_returns_empty_dict(tmp_path):
    path = tmp_path / "empty.yaml"
    path.write_text("", encoding="utf-8")
    assert load_settings(str(path)) == {}


def test_explicit_path_overrides_default(tmp_path):
    path = tmp_path / "custom.yaml"
    path.write_text(yaml.safe_dump({"app": {"db": "./x.db"}}), encoding="utf-8")
    cfg = load_settings(str(path), env={})          # 空 env：不受宿主环境干扰
    assert cfg["app"]["db"] == "./x.db"
    assert "driver" not in cfg


# ---------------------------------------------------------------- 数据根重定位

def test_resolve_home_reads_env(tmp_path):
    """未设置/纯空白 -> None（CLI 形态）；设置了则按 Path 语义返回并去首尾空白。

    用 tmp_path 而非硬编码 `/tmp/tp`：后者在 Windows 上会被 Path 归一成 `\tmp\tp`，
    断言就成了"测平台"而不是"测行为"。
    """
    from testpilot.config.settings import resolve_home
    assert resolve_home({}) is None
    assert resolve_home({"TESTPILOT_HOME": "  "}) is None

    target = tmp_path / "tp"
    assert resolve_home({"TESTPILOT_HOME": str(target)}) == target
    assert resolve_home({"TESTPILOT_HOME": f"  {target}  "}) == target


def test_apply_home_base_rebases_relative_and_keeps_absolute(tmp_path):
    """打包后安装目录只读：相对路径必须落到用户数据根；显式绝对路径不得被改。"""
    from testpilot.config.settings import apply_home_base

    cfg = {
        "app": {"db": "./data/a.db", "screenshot_root": "data/shots",
                "report_root": str(tmp_path / "abs_reports")},
        "log": {"dir": "./logs"},
        "vision": {"template_dir": "./data/templates"},
    }
    changed = apply_home_base(cfg, tmp_path / "home")

    assert cfg["app"]["db"] == str(tmp_path / "home" / "data" / "a.db")
    assert cfg["app"]["screenshot_root"] == str(tmp_path / "home" / "data" / "shots")
    assert cfg["app"]["report_root"] == str(tmp_path / "abs_reports")   # 绝对路径不动
    assert cfg["log"]["dir"] == str(tmp_path / "home" / "logs")
    assert cfg["vision"]["template_dir"] == str(
        tmp_path / "home" / "data" / "templates")
    assert len(changed) == 4                                            # 5 个键里有 1 个是绝对路径
    assert (tmp_path / "home").is_dir()                                 # 目录被创建


def test_apply_home_base_skips_empty_and_non_string(tmp_path):
    """空串 / 纯空白 / 非字符串都不算"需要重定位"，也不该触发建目录。"""
    from testpilot.config.settings import apply_home_base

    home = tmp_path / "home"
    cfg = {
        "app": {"db": 123},
        "log": {"dir": "   "},
        "vision": {"template_dir": ""},
    }
    assert apply_home_base(cfg, home) == []
    assert cfg["app"]["db"] == 123
    assert not home.exists()


def test_apply_home_base_tolerates_missing_sections(tmp_path):
    from testpilot.config.settings import apply_home_base
    assert apply_home_base({}, tmp_path) == []
    assert apply_home_base({"app": None}, tmp_path) == []


def test_load_settings_applies_home_from_env(tmp_path):
    """壳形态：TESTPILOT_HOME 一给，相对产出路径全部重定位（且目录已建好）。"""
    home = tmp_path / "locallow"
    cfg = load_settings(env={"TESTPILOT_HOME": str(home)})
    assert cfg["app"]["db"].startswith(str(home))
    assert cfg["app"]["screenshot_root"].startswith(str(home))
    assert cfg["app"]["report_root"].startswith(str(home))
    assert cfg["log"]["dir"].startswith(str(home))
    assert cfg["vision"]["template_dir"].startswith(str(home))
    assert home.is_dir()


def test_load_settings_without_home_keeps_relative_paths():
    """CLI 形态：不给数据根就按 cwd 走（开发/CI 行为不变）。"""
    cfg = load_settings(env={})
    assert cfg["app"]["db"] == "./data/testpilot.db"
    assert cfg["log"]["dir"] == "./logs"

