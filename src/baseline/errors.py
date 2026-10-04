"""领域异常与 HTTP 状态映射。"""
from __future__ import annotations

from typing import Any


class BaselineError(Exception):
    """所有领域异常的基类。"""

    code = "BASELINE_ERROR"
    http_status = 400

    def __init__(self, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        result = {"code": self.code, "message": self.message}
        if self.details is not None:
            result["details"] = self.details
        return result


class ValidationError(BaselineError):
    """请求内容或规则内容不合法。"""

    code = "VALIDATION_ERROR"
    http_status = 400


class NotFoundError(BaselineError):
    """规则或版本不存在。"""

    code = "NOT_FOUND"
    http_status = 404


class ForbiddenError(BaselineError):
    """当前角色无权执行该操作。"""

    code = "FORBIDDEN"
    http_status = 403


class InvalidTransitionError(BaselineError):
    """规则版本状态不允许该生命周期迁移。"""

    code = "INVALID_TRANSITION"
    http_status = 409


class SignatureRequiredError(BaselineError):
    """发布前签署角色不齐备，或签署人不满足要求。"""

    code = "SIGNATURE_REQUIRED"
    http_status = 422


class RuleConflictError(BaselineError):
    """发布前检测到生效区间重叠且车型条件相容。"""

    code = "RULE_CONFLICT"
    http_status = 409


class RetroactiveDeniedError(BaselineError):
    """操作试图回溯改写已经发生的适用区间。"""

    code = "RETROACTIVE_DENIED"
    http_status = 409


class AmbiguousRuleError(BaselineError):
    """同一日期与车型解析出多个有效版本（发布约束被破坏时的防御性异常）。"""

    code = "AMBIGUOUS_RULE"
    http_status = 500
