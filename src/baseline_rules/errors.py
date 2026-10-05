"""领域异常。"""
from __future__ import annotations


class BaselineRuleError(Exception):
    """所有规则库错误的基类。"""


class InvalidRuleContent(BaselineRuleError):
    """草稿内容不合法（参数、条件、区间等）。"""


class ConflictError(BaselineRuleError):
    """发布前检测到区间重叠且条件冲突。

    :param conflicts: 每项形如 ``{"other": version_id, "window": ..., "reason": ...}``。
    """

    def __init__(self, message: str, conflicts: list[dict] | None = None) -> None:
        super().__init__(message)
        self.conflicts = conflicts or []


class NotFoundError(BaselineRuleError):
    """引用的规则或版本不存在。"""


class AmbiguousRuleError(BaselineRuleError):
    """同一日期与车型解析出多个有效规则（不应发生，发布门禁负责拦截）。"""


class NoApplicableRuleError(BaselineRuleError):
    """指定日期与车型没有任何适用规则。"""
