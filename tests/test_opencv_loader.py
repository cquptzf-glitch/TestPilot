"""OpenCV 装载韧性单测（infra/opencv.py）。

要防的退化：``import cv2`` 依赖 ``cv2/__init__.py`` 这个**文本文件**可读。
企业环境下它可能读不了（DLP 加密返回密文 -> `source code string cannot contain null bytes`、
装包中断残留、AV 隔离），于是「视觉定位 / 视觉断言」整条能力线静默消失。

已验证的事实（本机实测）：同一份 ``cv2/__init__.py``，白名单进程读到明文、
非白名单进程读到密文、而 ``cv2.pyd``（二进制）始终可读 ——
所以「降级为直接装载 .pyd」是有效且必要的兜底。
"""
import sys
import types
from pathlib import Path

import pytest

from testpilot.infra import opencv as cv2mod


@pytest.fixture(autouse=True)
def _clean_cache():
    """每个用例都从干净缓存开始（缓存是模块级，会串味）。"""
    cv2mod.reset_cache()
    yield
    cv2mod.reset_cache()


def test_load_cv2_returns_working_module():
    """正常路径：能拿到模块且具备视觉驱动用到的关键 API。"""
    cv2 = cv2mod.load_cv2()
    for api in ("imread", "imdecode", "imencode", "matchTemplate", "TM_CCOEFF_NORMED"):
        assert hasattr(cv2, api), f"cv2 缺少 {api}（真实安装损坏？）"


def test_load_cv2_is_cached():
    """同一进程内不重复走失败/装载路径。"""
    first = cv2mod.load_cv2()
    assert cv2mod.load_cv2() is first


def test_broken_bootstrap_falls_back_to_pyd(monkeypatch):
    """引导文件不可读（模拟 DLP 密文）时，必须降级为直接装载原生扩展。

    做法：让 ``import cv2`` 抛异常（真实症状是 SyntaxError/UnicodeDecodeError），
    同时保证 ``_load_pyd_directly`` 能成功 —— 断言最终仍拿到可用的 cv2。
    """
    real_import = __import__
    sentinel = types.SimpleNamespace(__version__="fake",
                                     imread=lambda *a, **k: None)

    def fake_import(name, *args, **kwargs):
        if name == "cv2":
            raise SyntaxError("source code string cannot contain null bytes")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    monkeypatch.setattr(cv2mod, "_load_pyd_directly", lambda pyd: sentinel)
    # 给降级分支一个"存在"的候选路径（真实装载已被替换，内容不重要）
    monkeypatch.setattr(cv2mod, "_candidate_pyds",
                        lambda: [Path(__file__).resolve()])

    cv2 = cv2mod.load_cv2()
    assert cv2 is sentinel, "未走到降级分支"


def test_all_pyd_candidates_failing_raises_clear_error(monkeypatch):
    """降级也失败时必须抛清晰错误（调用方据此报"视觉不可用"）。"""
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "cv2":
            raise SyntaxError("source code string cannot contain null bytes")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    monkeypatch.setattr(cv2mod, "_candidate_pyds", lambda: [])
    with pytest.raises(ImportError) as exc:
        cv2mod.load_cv2()
    assert "降级装载也未找到可用的 cv2.pyd" in str(exc.value)


def test_candidate_pyds_are_deduped_and_absolute():
    """候选路径去重且为绝对路径（避免在 cwd 里误撞同名文件）。"""
    cands = cv2mod._candidate_pyds()
    assert cands, "未找到任何候选 cv2.pyd 路径"
    assert all(p.is_absolute() for p in cands)
    keys = [str(p).lower() for p in cands]
    assert len(keys) == len(set(keys)), "候选路径存在重复"


def test_clear_broken_cv2_removes_partial_modules(monkeypatch):
    """半途失败的 cv2 空壳必须被清掉，否则降级装载会被挡住。"""
    sys.modules["cv2"] = types.ModuleType("cv2")
    sys.modules["cv2.something"] = types.ModuleType("cv2.something")
    cv2mod._clear_broken_cv2()
    assert "cv2" not in sys.modules
    assert "cv2.something" not in sys.modules
