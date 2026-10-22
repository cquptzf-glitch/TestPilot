"""禅道适配 — 测试域语义映射：用例拉取 / 测试任务创建 / 任务级状态回写（设计 8.10 / 3.5）。

本层职责边界（设计 3.5）：
- 拉取用例（含步骤）→ 本地；
- 建测试任务（拿到 zentao_task_id）+ **创建后立即回读校验**（断链防线）；
- 任务结束时**仅回写汇总**（状态 + 通过率），执行明细只留本地；
- 回写失败重试 3 次后抛 ``ZentaoError``，由上层标记 ``sync_pending`` 走补偿入口。

认证：优先环境变量 Token；无 Token 时用 account/password 换 Token；
收到 401/403 自动重新登录并重放一次（设计 M2-3 的认证自愈）。
M2-3 收编说明：IssuePilot 移植的 async `ZentaoClient`（zentao_http.py，bug 域）
在本仓始终零引用，已随收编删除；本模块的同步实现（`ZentaoHttp`）是唯一实现，
功能等价（认证自愈/分页/重试/回读校验）且有测试覆盖。
"""
import html
import logging
import os
import re
import time
from datetime import date

from testpilot.core.errors import ZentaoError

log = logging.getLogger("testpilot.zentao")

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<\s*(br|/p|/div|/li)\s*/?\s*>", re.IGNORECASE)
_LEADING_NO_RE = re.compile(r"^\s*(\d+[.、)]|[-*•])\s*")


class ZentaoHttp:
    """同步 HTTP 门面：Token 头 + 认证自愈 + 错误归一化。"""

    def __init__(self, cfg: dict, client=None):
        import httpx

        self.cfg = cfg or {}
        if self.cfg.get("api_version", "v1") != "v1":
            raise ZentaoError("当前适配器支持禅道 REST v1；v2 不能混用，请选择 v1 接口")
        self.base_url = str(self.cfg.get("base_url", "")).rstrip("/")
        self.token_env = self.cfg.get("token_env", "ZENTAO_TOKEN")
        self.account = self.cfg.get("account") or os.environ.get("ZENTAO_ACCOUNT", "")
        self.password = self.cfg.get("password") or os.environ.get("ZENTAO_PASSWORD", "")
        self._token = os.environ.get(self.token_env, "")
        self._client = client or httpx.Client(base_url=self.base_url, timeout=30.0)

    # ---------- 认证 ----------

    @property
    def token(self) -> str:
        return self._token

    @property
    def logged_in(self) -> bool:
        return bool(self._token)

    def login(self) -> str:
        """用 account/password 换 Token（禅道 v1 REST: POST /api.php/v1/tokens）。"""
        if not (self.account and self.password):
            raise ZentaoError("未配置禅道 Token，且缺少 account/password，无法登录")
        data = self._send("POST", "/api.php/v1/tokens",
                          json={"account": self.account, "password": self.password},
                          with_auth=False)
        token = str((data or {}).get("token", ""))
        if not token:
            raise ZentaoError("禅道登录未返回 token")
        self._token = token
        log.info("禅道登录成功（token 已刷新）")
        return token

    def _headers(self, with_auth: bool = True) -> dict:
        if with_auth and self._token:
            return {"Token": self._token}
        return {}

    # ---------- 请求 ----------

    def _send(self, method: str, path: str, *, params=None, json=None,
              with_auth: bool = True) -> dict:
        try:
            resp = self._client.request(method, path, params=params, json=json,
                                        headers=self._headers(with_auth))
        except Exception as e:                      # 网络层
            error = ZentaoError(f"禅道网络异常 {method} {path}: {e}")
            error.creation_uncertain = method == "POST" and path.endswith("/testtasks")
            raise error from e
        status = getattr(resp, "status_code", 200)
        if status in (401, 403):
            if path.endswith("/tokens"):
                # 登录端点 401 是"账号/密码错误"，报"认证失效"会把排查引向 token
                raise ZentaoError(
                    f"禅道登录失败 {status}（检查 account/password 是否正确）")
            raise _AuthExpired(f"禅道认证失效 {status} {method} {path}")
        if status >= 400:
            raise ZentaoError(f"禅道接口报错 {status} {method} {path}: "
                              f"{getattr(resp, 'text', '')[:200]}")
        try:
            data = resp.json()
        except Exception as e:
            error = ZentaoError(f"禅道返回非 JSON {method} {path}: {e}")
            error.creation_uncertain = method == "POST" and path.endswith("/testtasks")
            raise error from e
        return _business_result(data)

    def request(self, method: str, path: str, *, params=None, json=None) -> dict:
        """带认证自愈的请求：401/403 时重登一次后重放。"""
        try:
            return self._send(method, path, params=params, json=json)
        except _AuthExpired as e:
            log.warning(f"{e}；尝试重新登录")
            self.login()
            return self._send(method, path, params=params, json=json)

    def get(self, path: str, params: dict | None = None) -> dict:
        return self.request("GET", path, params=params)

    def post(self, path: str, json: dict) -> dict:
        return self.request("POST", path, json=json)

    def put(self, path: str, json: dict) -> dict:
        return self.request("PUT", path, json=json)


class _AuthExpired(ZentaoError):
    """Token 失效（401/403）。

    必须是 ZentaoError 子类：重登再失败时它会从 request() 穿透到调用方，
    而上层（如编排层的建任务降级）只按契约捕 ZentaoError —— 裸 Exception
    会击穿降级路径，把"建禅道任务失败转本地任务"变成整个命令失败。
    """
    """内部信号：认证失效，需要重登。"""


class ZentaoAdapter:
    def __init__(self, cfg: dict, http=None):
        self.cfg = cfg or {}
        if self.cfg.get("api_version", "v1") != "v1":
            raise ZentaoError("当前适配器支持禅道 REST v1；v2 不能混用")
        self.http = http or ZentaoHttp(self.cfg)

    # ---------- 可用性 ----------

    def is_configured(self) -> bool:
        base = str(self.cfg.get("base_url", ""))
        return bool(base) and "example.com" not in base

    # ---------- 用例拉取 ----------

    def fetch_cases(self, product_id: int | None = None,
                    since: str | None = None) -> list[dict]:
        """分页拉取产品下全部测试用例（含步骤）；``since`` 之后更新的才算。

        ``since`` 为 ISO 时间串（与禅道 ``lastEditedDate`` 同格式）比较。
        """
        pid = product_id or self.cfg.get("product_id")
        if not pid:
            raise ZentaoError("未指定 product_id（cfg.zentao.product_id）")
        cases, page, limit, collected = [], 1, 100, 0
        while True:
            data = self.http.get(f"/api.php/v1/products/{pid}/testcases",
                                 params={"page": page, "limit": limit})
            rows = data.get("testcases") or data.get("cases") or []
            for row in rows:
                if since and str(row.get("lastEditedDate") or "") <= str(since):
                    continue
                if "steps" not in row:
                    raw = self.fetch_case(_positive_id(row.get("id"), "用例 id"))
                else:
                    raw = self._to_raw_case(row)
                if raw:
                    cases.append(raw)
            # 翻页判据必须用"已取回的行数"，不能用过滤后的条数
            # （带 since 时会永远小于 total 而无限翻页）
            collected += len(rows)
            total = int(data.get("total") or 0)
            if not rows:
                break
            if total <= 0:
                # 旧版禅道可能不回 total：按"整页未取满"判断是否还有下一页，
                # 否则超过一页的产品用例会被静默截断
                if len(rows) < limit or page >= 100:
                    break
            elif collected >= total or page >= 100:
                break
            page += 1
        # 分组名一次性解析（一次请求换全部用例的可读分组；失败只留 id）
        modules = self.fetch_modules(pid)
        for case in cases:
            if not case.get("module_name") and case.get("module") is not None:
                try:
                    case["module_name"] = modules.get(int(case["module"]))
                except (TypeError, ValueError):
                    pass
        log.info(f"拉取用例 {len(cases)} 条 (product={pid})",
                 extra={"count": len(cases), "task_id": pid})
        return cases

    def fetch_case(self, zentao_case_id: int) -> dict:
        """按 ID 拉取单条用例详情（含步骤）。"""
        row = self.http.get(f"/api.php/v1/testcases/{zentao_case_id}")
        if not isinstance(row, dict) or "steps" not in row:
            raise ZentaoError(f"禅道用例详情缺少 steps: {zentao_case_id}")
        if _positive_id(row.get("id"), "用例 id") != int(zentao_case_id):
            raise ZentaoError(f"禅道用例详情 id 不匹配: {zentao_case_id}")
        return self._to_raw_case(row)

    def fetch_modules(self, product_id: int | None = None) -> dict[int, str]:
        """拉取产品下用例分组（模块）树，返回 ``{id: 名称}``。

        分组是禅道"用例库-模块"维度；接口缺失（旧版本禅道对该端点回 404）或
        返回形态不符时返回空映射 —— 分组名解析不出来**不影响**用例同步本身，
        此时本地只存 module id（原文 raw_json 保有原始字段）。
        """
        pid = product_id or self.cfg.get("product_id")
        if not pid:
            return {}
        try:
            data = self.http.get(f"/api.php/v1/products/{pid}/modules")
        except Exception as e:      # 尽力而为：任何形态的不可用都只降级为"仅 id"
            log.info(f"分组（模块）接口不可用，仅按 module id 记录: {e}")
            return {}
        rows = data.get("modules") if isinstance(data, dict) else data
        out: dict[int, str] = {}
        for row in rows or []:
            if isinstance(row, dict) and row.get("id") is not None:
                out[int(row["id"])] = str(row.get("name") or "")
        return out

    def _to_raw_case(self, row: dict) -> dict:
        case_id = int(row.get("id") or row.get("case") or 0)
        if not case_id:
            # id 缺失的行入库只会产出 TC-0 假用例：跳过并留痕
            log.warning(f"禅道用例行缺少 id，已跳过: keys={sorted(row)[:8]}")
            return {}
        steps = normalize_case_steps(row.get("steps"))
        return {
            "zentao_case_id": case_id,
            "case_id": f"TC-{case_id}",
            "title": str(row.get("title", "")),
            "steps": steps,
            "steps_text": steps_to_text(steps),
            "preconditions": _split_lines(html.unescape(_TAG_RE.sub("",
                _BR_RE.sub("\n", str(row.get("precondition") or ""))))),
            "product": row.get("product"),
            "module": row.get("module"),
            "module_name": (str(row.get("moduleName"))
                            if row.get("moduleName") else None),
            "updated_at": str(row.get("lastEditedDate")
                              or row.get("openedDate") or ""),
        }

    # ---------- 测试任务 ----------

    def create_testtask(self, spec: dict) -> int:
        """Create once, retain the remote ID, and verify the actual linked range."""
        existing = spec.get("zentao_task_id")
        if existing:
            task_id = _positive_id(existing, "测试单 id")
            try:
                self.verify_testtask(task_id, spec)
            except ZentaoError as exc:
                exc.task_id = task_id
                raise
            return task_id
        if spec.get("creation_uncertain") or spec.get("creation_attempted"):
            raise ZentaoError("上次创建结果不确定，需先在禅道核对并填写已有测试单 ID，避免重复创建")
        build = spec.get("build") or self.cfg.get("build")
        if not build:
            raise ZentaoError("创建禅道测试单需要明确的构建/版本 build")
        begin = str(spec.get("begin") or date.today().isoformat())
        end = str(spec.get("end") or begin)
        try:
            if date.fromisoformat(begin) > date.fromisoformat(end):
                raise ValueError("结束日期早于开始日期")
        except ValueError as exc:
            raise ZentaoError(f"测试单起止日期非法: {exc}") from exc
        payload = {
            "product": spec.get("product_id") or self.cfg.get("product_id"),
            "name": spec["name"], "build": build, "begin": begin, "end": end,
            "cases": ",".join(str(c) for c in _zentao_ids(spec)),
            "desc": "TestPilot 自动化测试；用例范围需回读确认",
        }
        for key in ("owner", "execution"):
            if spec.get(key) is not None:
                payload[key] = spec[key]
        resp = _business_result(self.http.post("/api.php/v1/testtasks", json=payload))
        try:
            task_id = _positive_id(resp.get("id"), "测试单 id")
        except ZentaoError as exc:
            exc.creation_uncertain = True
            raise
        try:
            self.verify_testtask(task_id, spec)
        except Exception as exc:
            error = ZentaoError(f"测试单 #{task_id} 已创建，待核对关联用例: {exc}")
            error.task_id = task_id
            raise error from exc
        return task_id

    def verify_testtask(self, task_id: int, spec: dict) -> dict:
        back = _business_result(self.http.get(f"/api.php/v1/testtasks/{task_id}"))
        if _positive_id(back.get("id"), "测试单 id") != task_id:
            raise ZentaoError(f"禅道任务回读 id 不匹配: {task_id}")
        expected = set(_zentao_ids(spec))
        # Only actual testcase associations count. An echoed 'cases' request field is not proof.
        rows = back.get("testcases")
        if rows is None:
            raise ZentaoError("回读未提供 testcases 关联范围，请先在禅道关联用例再补偿")
        if not isinstance(rows, list):
            raise ZentaoError("回读 testcases 必须为数组")
        linked = {_positive_id(r.get("case", r.get("id")) if isinstance(r, dict) else r,
                               "关联用例 id") for r in rows}
        if linked != expected:
            raise ZentaoError(f"测试单用例范围不匹配: 期望 {sorted(expected)}，实际 {sorted(linked)}")
        return back

    def update_task_status(self, task_id: int, status: str,
                           summary: dict, retries: int = 3) -> None:
        text = (f"TestPilot 回归: 通过 {summary.get('pass', 0)}/{summary.get('total', 0)}"
                f"（失败 {summary.get('fail', 0)}，阻塞 {summary.get('blocked', 0)}），"
                f"报告见本地 run#{summary.get('run_id')}")
        last = None
        for attempt in range(max(1, retries)):
            try:
                back = _business_result(self.http.get(f"/api.php/v1/testtasks/{task_id}"))
                if _positive_id(back.get("id"), "测试单 id") != task_id:
                    raise ZentaoError("回写前测试单 id 不匹配")
                if back.get("status") == status and text in _clean_text(back.get("desc")):
                    return
                body = {key: back[key] for key in
                        ("name", "build", "execution", "owner", "begin", "end") if key in back}
                # Preserve the user's description and replace our own summary, making retries idempotent.
                desc = re.sub(r'<div data-testpilot-summary="1">.*?</div>', "",
                              str(back.get("desc") or ""), flags=re.S).rstrip()
                body.update(status=status,
                            desc=desc + '<div data-testpilot-summary="1">' + html.escape(text) + "</div>")
                _business_result(self.http.put(f"/api.php/v1/testtasks/{task_id}", json=body))
                saved = _business_result(self.http.get(f"/api.php/v1/testtasks/{task_id}"))
                if (saved.get("id") != back["id"] or saved.get("status") != status
                        or text not in _clean_text(saved.get("desc"))):
                    raise ZentaoError("回写后状态或汇总回读不一致")
                return
            except Exception as exc:
                last = exc
                if attempt + 1 < max(1, retries):
                    time.sleep(0.5 * (attempt + 1))
        raise ZentaoError(f"禅道回写未确认(已尝试 {retries} 次): {task_id}: {last}")


# ---------- 步骤归一化 ----------

def _positive_id(value, label="id") -> int:
    if isinstance(value, bool) or not str(value).isdigit() or int(value) <= 0:
        raise ZentaoError(f"{label} 必须为正整数，实际 {value!r}")
    return int(value)


def _business_result(data) -> dict:
    if not isinstance(data, dict):
        raise ZentaoError("禅道返回体必须为对象")
    if (str(data.get("status", "")).lower() in ("fail", "failed", "error")
            or data.get("success") is False or data.get("error") or data.get("errors")):
        raise ZentaoError(f"禅道业务失败: {str(data)[:300]}")
    return data


def _zentao_ids(spec: dict) -> list[int]:
    if "zentao_case_ids" in spec:
        return [_positive_id(v, "禅道用例 id") for v in spec["zentao_case_ids"]]
    # Legacy callers may use the canonical TC-<id> name; arbitrary local names are never guessed.
    ids = []
    for cid in spec.get("case_ids") or []:
        match = re.fullmatch(r"TC-([1-9][0-9]*)", str(cid))
        if not match:
            raise ZentaoError(f"用例 {cid!r} 没有明确的禅道 ID 映射")
        ids.append(int(match[1]))
    return ids


def normalize_case_steps(raw) -> list[dict]:
    """禅道步骤字段归一化为 ``[{"desc": str, "expect": str}]``。

    兼容三种形态：结构化数组、富文本 HTML、纯文本多行。
    """
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        out = []
        for item in raw:
            if isinstance(item, dict):
                desc = _clean_text(item.get("desc") or item.get("step")
                                   or item.get("name") or "")
                expect = _clean_text(item.get("expect") or "")
            else:
                desc, expect = _clean_text(str(item)), ""
            if desc or expect:
                out.append({"desc": desc, "expect": expect})
        return out
    text = str(raw)
    if "<" in text and ">" in text:
        text = _BR_RE.sub("\n", text)
        text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    return [{"desc": line, "expect": ""}
            for line in _split_lines(text)]


def steps_to_text(steps: list[dict]) -> str:
    """步骤数组 -> 多行文本（LLM prompt 与本地原文快照共用）。"""
    lines = []
    for i, s in enumerate(steps, start=1):
        desc = s.get("desc", "")
        expect = s.get("expect", "")
        line = f"{i}. {desc}"
        if expect:
            line += f"  [期望: {expect}]"
        lines.append(line)
    return "\n".join(lines)


def _clean_text(text) -> str:
    s = str(text or "")
    if "<" in s and ">" in s:
        s = _BR_RE.sub("\n", s)
        s = _TAG_RE.sub("", s)
    return " ".join(html.unescape(s).split())


def _split_lines(text: str) -> list[str]:
    out = []
    for line in text.replace("\r\n", "\n").split("\n"):
        cleaned = _LEADING_NO_RE.sub("", line.strip()).strip()
        if cleaned:
            out.append(cleaned)
    return out


def create(cfg: dict) -> ZentaoAdapter:
    return ZentaoAdapter(cfg)
