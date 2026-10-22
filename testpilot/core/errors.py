"""TestPilot exception hierarchy + JSON-RPC error codes.

业务异常体系来自设计文档 5 章；RpcError 及禅道相关错误码沿袭
IssuePilot zentao client 的错误语义（M2-3 收编后唯一实现在本模块）。
"""


class TestPilotError(Exception):
    """Base of all TestPilot business errors."""


class LocateError(TestPilotError):
    """定位失败（含置信度与已尝试的搜索条件）。"""

    def __init__(self, reason, confidence=None, searched=None):
        super().__init__(reason)
        self.reason = reason
        self.confidence = confidence
        self.searched = searched


class ActionError(TestPilotError):
    """操作失败（点击/输入异常）。"""


class VerifyError(TestPilotError):
    """验证失败（含期望值/实际值）。"""


class DeviceError(TestPilotError):
    """模拟器指令失败/超时。"""


class AppError(TestPilotError):
    """被测程序崩溃/未就绪。"""


class AbortRequested(TestPilotError):
    """协作取消信号。"""


class LlmError(TestPilotError):
    """LLM/VLM 调用失败（网关不可用、返回非 JSON、超时）。"""


class ZentaoError(TestPilotError):
    """禅道接口失败（认证失效、业务报错、回读校验不通过）。"""


# ---------- JSON-RPC 错误码（壳<->引擎，见设计 2.5；兼容移植的 zentao client） ----------

class RpcError(Exception):
    """携带 JSON-RPC error 对象三要素的业务异常。"""

    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "data": self.data}


PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

AUTH_REQUIRED = -32001          # 本地 token 校验失败 / 未登录禅道
ZENTAO_UPSTREAM = -32002        # 禅道接口报错
TASK_NOT_FOUND = -32005


def invalid_params(message: str, data=None) -> RpcError:
    return RpcError(INVALID_PARAMS, message, data)


def method_not_found(name: str) -> RpcError:
    return RpcError(METHOD_NOT_FOUND, f"method not found: {name}")
