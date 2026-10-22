"""截图差异（``infra/imaging``）与 ``run.shotDiff`` 单测。

要点：差异预览与 ``verifier`` 的 diff 断言**必须共用同一套像素语义**
（否则会出现"预览图看着变了、断言判没变"的自相矛盾），这里把关键行为固定下来。
"""
import pytest

from testpilot.infra.imaging import decode_png, diff_overlay, imread, imwrite

cv2 = pytest.importorskip("cv2", reason="视觉依赖未安装（cv2）")
import numpy as np  # noqa: E402


def _png(img) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


@pytest.fixture()
def blank():
    return np.full((60, 80, 3), 240, dtype=np.uint8)


# ---------------------------------------------------------------- 读写

def test_imread_imwrite_roundtrip_with_chinese_path(tmp_path, blank):
    """中文路径必须能写能读（cv2.imread/imwrite 走 ANSI API 会静默失败）。"""
    path = tmp_path / "模板" / "开始检测.png"
    assert imwrite(path, blank) is True
    assert path.exists()
    back = imread(path)
    assert back is not None
    assert back.shape == blank.shape


def test_imread_missing_file_returns_none(tmp_path):
    assert imread(tmp_path / "不存在.png") is None


def test_imread_garbage_returns_none(tmp_path):
    """非图片内容不能让调用方崩（模板库被外部污染的场景）。"""
    path = tmp_path / "bad.png"
    path.write_bytes(b"not an image at all")
    assert imread(path) is None


def test_imwrite_rejects_invalid_image(tmp_path):
    assert imwrite(tmp_path / "x.png", "definitely-not-an-image") is False


def test_decode_png_handles_empty_bytes():
    assert decode_png(b"") is None


# ---------------------------------------------------------------- 差异

def test_identical_images_report_zero_ratio(blank):
    overlay, stats = diff_overlay(_png(blank), _png(blank))
    assert stats["same_size"] is True
    assert stats["ratio"] == 0.0
    assert stats["changed"] == 0
    assert stats["boxes"] == 0
    # 契约是"PNG 字节"（消费方 write_bytes 直接落盘）：历史上零差异分支
    # 返回过 ndarray，run.shotDiff 一调就崩
    assert isinstance(overlay, (bytes, bytearray))
    assert decode_png(overlay) is not None


def test_changed_region_is_located_and_boxed(blank):
    """差异必须被**定位成框**（用户要知道"哪里变了"，整图叠加看不出来）。"""
    after = blank.copy()
    after[10:25, 20:45] = (0, 0, 255)          # 明显色块变化
    overlay, stats = diff_overlay(_png(blank), _png(after))
    assert stats["same_size"] is True
    assert stats["changed"] > 0
    assert stats["ratio"] > 0
    assert stats["boxes"] >= 1
    assert overlay is not None
    drawn = decode_png(overlay)
    assert drawn is not None
    assert drawn.shape == blank.shape


def test_pixel_noise_below_delta_is_ignored(blank):
    """轻微噪声不该被当成"变了"（否则每次 diff 都满屏红框，失去信号意义）。"""
    after = blank.copy()
    after[5:15, 5:15] = 244                    # 仅 +4，低于默认容差 12
    _, stats = diff_overlay(_png(blank), _png(after))
    assert stats["changed"] == 0
    assert stats["ratio"] == 0.0


def test_tiny_speck_is_filtered_by_min_area(blank):
    """极小面积噪点被 min_area 过滤（形态学 + 面积阈值）。"""
    after = blank.copy()
    after[30, 30] = (0, 0, 0)                  # 单像素变化
    _, stats = diff_overlay(_png(blank), _png(after), min_area=4)
    assert stats["boxes"] == 0
    assert stats["changed"] == 1               # 像素确实变了，只是不作为框输出


def test_size_change_is_flagged(blank):
    """尺寸变化无法逐像素对齐：必须显式标记，不能让用户误以为"没变化"。"""
    bigger = np.full((60, 120, 3), 240, dtype=np.uint8)
    overlay, stats = diff_overlay(_png(blank), _png(bigger))
    assert stats["same_size"] is False
    assert stats["changed"] == -1              # 约定：-1 = 尺寸不一致
    assert stats["size_before"] == "80x60"
    assert stats["size_after"] == "120x60"
    assert overlay is not None


def test_undecodable_input_returns_none_stats():
    overlay, stats = diff_overlay(b"garbage", b"garbage")
    assert overlay is None
    assert stats["ratio"] == 0.0
    assert stats["same_size"] is False


def test_box_count_is_capped(blank):
    """差异框数量要有上限：整屏滚动/换页时框会有上百个，前端渲染会卡。"""
    after = blank.copy()
    for i in range(0, 60, 6):
        for j in range(0, 80, 6):
            after[i:i + 3, j:j + 3] = (0, 0, 0)
    _, stats = diff_overlay(_png(blank), _png(after), max_boxes=5)
    assert stats["boxes"] <= 5


# ---------------------------------------------------------------- 与断言口径一致

def test_preview_and_assertion_share_pixel_semantics(blank):
    """预览"有变化" <=> 断言判 pass（同一份像素容差与比较方式）。

    这是防止两处阈值漂移的关键回归点：预览用绝对值差的像素计数，
    断言也是；如果哪天改成"各写一份"，这里会红。
    """
    from testpilot.core.contracts import ExecContext, Screenshot
    from testpilot.core.dsl import TestCase, VerifySpec
    from testpilot.engine.verifier import Verifier

    changed = blank.copy()
    changed[10:25, 20:45] = (0, 0, 255)
    before = Screenshot(png=_png(blank))
    after = Screenshot(png=_png(changed))

    _, stats = diff_overlay(before.png, after.png)
    assert stats["ratio"] > 0

    verifier = Verifier({}, emulator=None, llm=None, cfg={})
    ctx = ExecContext(run_id=1, case_exec_id=1, case=TestCase(case_id="TC-1"))
    verify = VerifySpec(type="screenshot_diff")
    result = verifier._verify_diff(before, after, verify)
    assert result.status == "pass", result.detail
    assert "变化像素比" in result.detail
    assert ctx.run_id == 1


def test_assertion_reports_unavailable_when_opencv_broken(monkeypatch, blank):
    """OpenCV 不可用时断言必须报 unknown（不能误判 pass/fail）。"""
    from testpilot.core.contracts import Screenshot
    from testpilot.core.dsl import VerifySpec
    from testpilot.engine.verifier import Verifier

    def boom():
        raise ImportError("模拟 OpenCV 不可用")

    monkeypatch.setattr("testpilot.infra.opencv.load_cv2", boom)
    verifier = Verifier({}, emulator=None, llm=None, cfg={})
    result = verifier._verify_diff(Screenshot(png=_png(blank)),
                                  Screenshot(png=_png(blank)),
                                  VerifySpec(type="screenshot_diff"))
    assert result.status == "unknown"
    assert "OpenCV 不可用" in result.detail
