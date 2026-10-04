"""车型基线规则库后端。

提供基线规则版本管理、车型适用条件、生效区间、发布签署、
替代关系与历史冻结解析能力。
"""
from .conditions import Condition, condition_matches, find_self_conflicts, are_compatible
from .errors import (
    AmbiguousRuleError,
    BaselineError,
    ForbiddenError,
    InvalidTransitionError,
    NotFoundError,
    RetroactiveDeniedError,
    RuleConflictError,
    SignatureRequiredError,
    ValidationError,
)
from .model import (
    ROLE_ACCOUNTANT,
    ROLE_AUDITOR,
    ROLE_ENTERPRISE_FILER,
    ROLE_OPERATOR,
    STATUS_CONFIRMED,
    STATUS_DRAFT,
    STATUS_PENDING,
    STATUS_SEALED,
    Actor,
    RuleVersion,
    intervals_overlap,
    lifecycle_state,
    parse_date,
)
from .resolver import resolve
from .service import RuleService

__all__ = [
    "Condition",
    "condition_matches",
    "find_self_conflicts",
    "are_compatible",
    "BaselineError",
    "ValidationError",
    "NotFoundError",
    "ForbiddenError",
    "InvalidTransitionError",
    "SignatureRequiredError",
    "RuleConflictError",
    "RetroactiveDeniedError",
    "AmbiguousRuleError",
    "Actor",
    "RuleVersion",
    "intervals_overlap",
    "lifecycle_state",
    "parse_date",
    "resolve",
    "RuleService",
    "ROLE_ENTERPRISE_FILER",
    "ROLE_ACCOUNTANT",
    "ROLE_OPERATOR",
    "ROLE_AUDITOR",
    "STATUS_DRAFT",
    "STATUS_PENDING",
    "STATUS_CONFIRMED",
    "STATUS_SEALED",
]
