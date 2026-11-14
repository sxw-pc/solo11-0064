"""带稳定错误码的领域异常。"""

from __future__ import annotations


class ErrorCode:
    USAGE = "FC_USAGE"
    IO = "FC_IO"
    INPUT_TOO_LARGE = "FC_INPUT_TOO_LARGE"
    SPEC_TOO_LARGE = "FC_SPEC_TOO_LARGE"
    SPEC_INVALID = "FC_SPEC_INVALID"
    INPUT_ENCODING = "FC_INPUT_ENCODING"
    INPUT_INVALID = "FC_INPUT_INVALID"
    RUN_NOT_FOUND = "FC_RUN_NOT_FOUND"
    ARTIFACT_MISSING = "FC_ARTIFACT_MISSING"
    HOST_NOT_ALLOWED = "FC_HOST_NOT_ALLOWED"
    WEB_ERROR = "FC_WEB_ERROR"


class FunnelCheckError(Exception):
    """可被 CLI 与 Web 识别的异常。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        exit_code: int = 2,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.exit_code = exit_code

    def as_dict(self) -> dict[str, str]:
        payload = {"code": self.code, "message": self.message}
        if self.hint:
            payload["hint"] = self.hint
        return payload


class SpecError(FunnelCheckError):
    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(ErrorCode.SPEC_INVALID, message, hint=hint)


class InputError(FunnelCheckError):
    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(ErrorCode.INPUT_INVALID, message, hint=hint)


class NotFoundError(FunnelCheckError):
    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(ErrorCode.RUN_NOT_FOUND, message, hint=hint, exit_code=1)
