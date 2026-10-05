"""车型基线规则库后端。

对外提供：

- :class:`BaselineRuleService`：基线规则、生效区间、签署发布、撤回、紧急勘误与解析；
- :class:`~baseline_rules.models.RuleVersion` 等不可变领域模型；
- :func:`baseline_rules.api.build_server`：基于标准库的 HTTP API。
"""
from .errors import (
    AmbiguousRuleError,
    BaselineRuleError,
    ConflictError,
    InvalidRuleContent,
    NoApplicableRuleError,
    NotFoundError,
)
from .models import Criterion, Draft, Event, Resolution, RuleVersion, Withdrawal
from .service import BaselineRuleService

__all__ = [
    "AmbiguousRuleError",
    "BaselineRuleError",
    "BaselineRuleService",
    "ConflictError",
    "Criterion",
    "Draft",
    "Event",
    "InvalidRuleContent",
    "NoApplicableRuleError",
    "NotFoundError",
    "Resolution",
    "RuleVersion",
    "Withdrawal",
]
