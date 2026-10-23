"""禅道适配单测 — 步骤归一化、分页、回读校验、回写重试、认证自愈（设计 8.10 / 3.5）。"""
import pytest

from testpilot.adapters.zentao_adapter import (ZentaoAdapter, ZentaoHttp,
                                               _zentao_ids, normalize_case_steps,
                                               steps_to_text)
from testpilot.core.errors import ZentaoError


class FakeHttp:
    """可编排的 HTTP 门面替身：按调用顺序返回预设响应。"""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def _take(self, method, path, params=None, json=None):
        self.calls.append({"method": method, "path": path, "params": params,
                           "json": json})
        if not self.responses:
            raise AssertionError(f"未预设响应: {method} {path}")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, path, params=None):
        return self._take("GET", path, params=params)

    def post(self, path, json=None):
        return self._take("POST", path, json=json)

    def put(self, path, json=None):
        return self._take("PUT", path, json=json)


# ---------- 步骤归一化 ----------

def test_normalize_structured_steps():
    steps = normalize_case_steps([
        {"desc": "点击<b>开始</b>", "expect": "出现表格"},
        {"step": "输入编号", "expect": ""},
    ])
    assert steps == [{"desc": "点击开始", "expect": "出现表格"},
                     {"desc": "输入编号", "expect": ""}]


def test_normalize_html_and_entities():
    html = "<p>1. 启动软件</p><br/>2. 点击「开始」&amp;等待<br/>"
    assert [s["desc"] for s in normalize_case_steps(html)] == ["启动软件", "点击「开始」&等待"]


def test_normalize_plain_text_strips_numbering():
    text = "1. 第一步\n2) 第二步\n- 第三步\n\n   "
    assert [s["desc"] for s in normalize_case_steps(text)] == ["第一步", "第二步", "第三步"]


def test_normalize_empty_inputs():
    assert normalize_case_steps(None) == []
    assert normalize_case_steps("") == []
    assert normalize_case_steps([]) == []
    assert normalize_case_steps([{"desc": ""}]) == []


def test_steps_to_text():
    text = steps_to_text([{"desc": "点开始", "expect": "有结果"},
                          {"desc": "关闭窗口", "expect": ""}])
    assert text == "1. 点开始  [期望: 有结果]\n2. 关闭窗口"


def test_zentao_ids_derivation():
    assert _zentao_ids({"zentao_case_ids": [1, 2]}) == [1, 2]
    assert _zentao_ids({"case_ids": ["TC-1024", "TC-7"]}) == [1024, 7]
    with pytest.raises(ZentaoError):
        _zentao_ids({"case_ids": ["bad"]})


# ---------- 拉取 ----------

def test_fetch_case_normalizes():
    http = FakeHttp([{"id": 1024, "title": "全流程", "steps": [
        {"desc": "启动", "expect": ""}], "lastEditedDate": "2026-01-01"}])
    adapter = ZentaoAdapter({}, http)
    raw = adapter.fetch_case(1024)
    assert raw["case_id"] == "TC-1024"
    assert raw["zentao_case_id"] == 1024
    assert raw["title"] == "全流程"
    assert raw["updated_at"] == "2026-01-01"
    assert raw["steps_text"] == "1. 启动"


def test_fetch_cases_paginates_until_total():
    page1 = {"total": 3, "testcases": [
        {"id": 1, "title": "a", "steps": "1. x", "lastEditedDate": "2026-01-01"},
        {"id": 2, "title": "b", "steps": "1. y", "lastEditedDate": "2026-01-01"}]}
    page2 = {"total": 3, "testcases": [
        {"id": 3, "title": "c", "steps": "1. z", "lastEditedDate": "2026-01-01"}]}
    http = FakeHttp([page1, page2])
    cases = ZentaoAdapter({}, http).fetch_cases(product_id=7)
    assert [c["case_id"] for c in cases] == ["TC-1", "TC-2", "TC-3"]
    assert http.calls[0]["params"] == {"page": 1, "limit": 100}
    assert http.calls[1]["params"] == {"page": 2, "limit": 100}


def test_fetch_cases_since_filter():
    page = {"total": 2, "testcases": [
        {"id": 1, "title": "a", "steps": "", "lastEditedDate": "2026-01-01"},
        {"id": 2, "title": "b", "steps": "", "lastEditedDate": "2026-03-01"}]}
    http = FakeHttp([page])
    cases = ZentaoAdapter({}, http).fetch_cases(product_id=1, since="2026-02-01")
    assert [c["case_id"] for c in cases] == ["TC-2"]


def test_fetch_cases_since_includes_equal_watermark_boundary():
    page = {"total": 3, "testcases": [
        {"id": 1, "title": "old", "steps": "", "lastEditedDate": "2026-01-31T23:59:59"},
        {"id": 2, "title": "edge", "steps": "", "lastEditedDate": "2026-02-01T00:00:00"},
        {"id": 3, "title": "new", "steps": "", "lastEditedDate": "2026-02-01T00:00:01"},
    ]}
    http = FakeHttp([page])
    cases = ZentaoAdapter({}, http).fetch_cases(
        product_id=1, since="2026-02-01T00:00:00")
    assert [c["case_id"] for c in cases] == ["TC-2", "TC-3"]


def test_fetch_cases_requires_product():
    with pytest.raises(ZentaoError):
        ZentaoAdapter({}, FakeHttp([])).fetch_cases()


def test_fetch_cases_accepts_legacy_cases_key():
    page = {"total": 1, "cases": [{"id": 5, "title": "x", "steps": "", "lastEditedDate": ""}]}
    http = FakeHttp([page])
    assert ZentaoAdapter({}, http).fetch_cases(product_id=1)[0]["case_id"] == "TC-5"


# ---------- 建任务 / 回写 ----------

def test_create_testtask_readback_ok():
    http = FakeHttp([{"id": 555}, {"id": 555, "testcases": [{"id": 1}, {"id": 2}]}])
    adapter = ZentaoAdapter({"product_id": 1, "build": "7"}, http)
    task_id = adapter.create_testtask({"name": "回归", "case_ids": ["TC-1", "TC-2"]})
    assert task_id == 555
    assert http.calls[0]["json"]["cases"] == "1,2"
    assert http.calls[0]["json"]["product"] == 1
    assert http.calls[1]["path"].endswith("/testtasks/555")


def test_create_testtask_readback_mismatch_raises():
    http = FakeHttp([{"id": 555}, {"id": 999}])
    with pytest.raises(ZentaoError):
        ZentaoAdapter({}, http).create_testtask({"name": "n", "case_ids": [1]})


def test_create_testtask_without_id_raises():
    http = FakeHttp([{}])
    with pytest.raises(ZentaoError):
        ZentaoAdapter({}, http).create_testtask({"name": "n", "case_ids": [1]})


def test_update_task_status_retries_then_succeeds():
    text = "TestPilot 回归: 通过 1/2（失败 0，阻塞 0），报告见本地 run#3"
    http = FakeHttp([ZentaoError("500"), ZentaoError("500"),
                     {"id": 9, "status": "wait", "desc": "original"},
                     {"ok": True}, {"id": 9, "status": "done", "desc": text}])
    ZentaoAdapter({}, http).update_task_status(9, "done",
                                              {"pass": 1, "total": 2, "run_id": 3})
    assert len(http.calls) == 5
    assert "1/2" in http.calls[-2]["json"]["desc"]
    assert "run#3" in http.calls[-2]["json"]["desc"]


def test_update_task_status_exhausts_retries():
    http = FakeHttp([ZentaoError("500")] * 3)
    with pytest.raises(ZentaoError):
        ZentaoAdapter({}, http).update_task_status(9, "done", {}, retries=3)
    assert len(http.calls) == 3


def test_is_configured():
    assert ZentaoAdapter({}, FakeHttp([])).is_configured() is False
    assert ZentaoAdapter({"base_url": "https://zt.corp.local"}, FakeHttp([])) \
        .is_configured() is True
    assert ZentaoAdapter({"base_url": "https://zentao.example.com"},
                         FakeHttp([])).is_configured() is False


# ---------- ZentaoHttp（认证自愈 / 错误归一化） ----------

class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _Client:
    def __init__(self, seq):
        self.seq = list(seq)
        self.calls = []

    def request(self, method, path, params=None, json=None, headers=None):
        self.calls.append({"method": method, "path": path, "headers": headers})
        item = self.seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_http_token_from_env(monkeypatch):
    monkeypatch.setenv("ZENTAO_TOKEN", "tok-123")
    http = ZentaoHttp({"base_url": "https://zt.local"}, client=_Client([]))
    assert http.token == "tok-123" and http.logged_in is True


def test_http_login_uses_account_password(monkeypatch):
    monkeypatch.delenv("ZENTAO_TOKEN", raising=False)
    client = _Client([_Resp(200, {"token": "fresh"})])
    http = ZentaoHttp({"base_url": "https://zt.local", "account": "u",
                       "password": "p"}, client=client)
    assert http.logged_in is False
    assert http.login() == "fresh"
    assert client.calls[0]["path"] == "/api.php/v1/tokens"


def test_http_login_without_credentials_raises(monkeypatch):
    monkeypatch.delenv("ZENTAO_TOKEN", raising=False)
    http = ZentaoHttp({"base_url": "https://zt.local"}, client=_Client([]))
    with pytest.raises(ZentaoError):
        http.login()


def test_http_reauth_on_401_then_replay(monkeypatch):
    monkeypatch.setenv("ZENTAO_TOKEN", "old")
    client = _Client([_Resp(401, text="unauthorized"),          # 第一次 401
                      _Resp(200, {"token": "new"}),             # 重新登录
                      _Resp(200, {"id": 5, "title": "t"})])     # 重放
    http = ZentaoHttp({"base_url": "https://zt.local", "account": "u",
                       "password": "p"}, client=client)
    data = http.get("/api.php/v1/testcases/5")
    assert data == {"id": 5, "title": "t"}
    assert http.token == "new"
    assert len(client.calls) == 3


def test_http_4xx_raises_zentao_error():
    http = ZentaoHttp({"base_url": "https://zt.local"},
                      client=_Client([_Resp(500, text="boom")]))
    with pytest.raises(ZentaoError) as e:
        http.get("/x")
    assert "500" in str(e.value)


def test_http_network_error_wrapped():
    http = ZentaoHttp({"base_url": "https://zt.local"},
                      client=_Client([OSError("connection refused")]))
    with pytest.raises(ZentaoError):
        http.get("/x")


def test_http_non_json_response_raises():
    class _Bad(_Resp):
        def json(self):
            raise ValueError("not json")

    http = ZentaoHttp({"base_url": "https://zt.local"},
                      client=_Client([_Bad(200, text="<html>")]))
    with pytest.raises(ZentaoError):
        http.get("/x")


# ---------- 用例分组（模块，第十五轮） ----------

def test_fetch_cases_resolves_module_names():
    """同步时用一次 modules 请求把分组 id 解析成名称（供 UI 分组/筛选）。"""
    page = {"total": 2, "testcases": [
        {"id": 1, "title": "a", "steps": "1. x", "module": 5,
         "lastEditedDate": "2026-01-01"},
        {"id": 2, "title": "b", "steps": "1. y", "module": 6,
         "lastEditedDate": "2026-01-01"}]}
    modules = {"modules": [{"id": 5, "name": "登录模块"},
                           {"id": 6, "name": "订单模块"}]}
    adapter = ZentaoAdapter({"product_id": 9}, FakeHttp([page, modules]))
    cases = adapter.fetch_cases()
    assert [c["module_name"] for c in cases] == ["登录模块", "订单模块"]
    assert cases[0]["module"] == 5


def test_fetch_cases_module_endpoint_failure_keeps_id():
    """旧版本禅道没有 modules 端点：分组名缺省为空，但 module id 与同步不受影响。"""
    page = {"total": 1, "testcases": [
        {"id": 1, "title": "a", "steps": "1. x", "module": 5,
         "lastEditedDate": "2026-01-01"}]}
    adapter = ZentaoAdapter({"product_id": 9}, FakeHttp([page]))
    cases = adapter.fetch_cases()          # 第二次请求会耗尽 Fake -> 宽捕获降级
    assert cases[0]["module"] == 5
    assert cases[0]["module_name"] is None


def test_to_raw_case_passes_through_modulename_field():
    adapter = ZentaoAdapter({}, FakeHttp())
    raw = adapter._to_raw_case({"id": 7, "title": "t", "steps": [],
                                "module": 3, "moduleName": "设置模块",
                                "lastEditedDate": "2026-01-01"})
    assert raw["module"] == 3
    assert raw["module_name"] == "设置模块"
