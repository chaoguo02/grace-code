"""
hitl/policy.py

策略引擎：基于 YAML 规则自动 approve/deny 工具调用。
规则存储在 .forge-agent/hitl/policies.yaml。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore


@dataclass
class PolicyRule:
    """一条审批策略规则。"""
    id: str
    tool_name: str                  # "*" 匹配所有工具
    action: str                     # "approve" | "deny"
    condition: dict[str, Any]       # 匹配条件
    source: str = "user"            # "user" | "learned"
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def matches(self, tool_name: str, params: dict[str, Any]) -> bool:
        """检查此规则是否匹配给定的工具调用。"""
        # 工具名匹配
        if self.tool_name != "*" and self.tool_name != tool_name:
            return False

        # 无条件匹配
        if not self.condition or self.condition.get("always"):
            return True

        # param_contains: 参数值包含指定子串
        if "param_contains" in self.condition:
            for key, substr in self.condition["param_contains"].items():
                val = str(params.get(key, ""))
                if substr not in val:
                    return False
            return True

        # param_regex: 参数值匹配正则
        if "param_regex" in self.condition:
            for key, pattern in self.condition["param_regex"].items():
                val = str(params.get(key, ""))
                if not re.search(pattern, val):
                    return False
            return True

        # param_equals: 参数值精确匹配
        if "param_equals" in self.condition:
            for key, expected in self.condition["param_equals"].items():
                if params.get(key) != expected:
                    return False
            return True

        return False


class PolicyEngine:
    """
    策略引擎。维护规则列表，按顺序匹配。

    规则优先级：列表顺序（第一条匹配的生效）。
    """

    def __init__(self, policies_path: str | None = None) -> None:
        self._rules: list[PolicyRule] = []
        self._path = Path(policies_path) if policies_path else None
        if self._path:
            self._load()

    @property
    def rules(self) -> list[PolicyRule]:
        return list(self._rules)

    def match(self, tool_name: str, params: dict[str, Any]) -> PolicyRule | None:
        """找到第一条匹配的规则。无匹配返回 None。"""
        for rule in self._rules:
            if rule.matches(tool_name, params):
                return rule
        return None

    def add_rule(self, rule: PolicyRule) -> None:
        """添加规则并持久化。"""
        self._rules.append(rule)
        self._save()

    def remove_rule(self, rule_id: str) -> bool:
        """按 ID 删除规则。返回是否成功。"""
        before = len(self._rules)
        self._rules = [r for r in self._rules if r.id != rule_id]
        if len(self._rules) < before:
            self._save()
            return True
        return False

    def create_rule(
        self,
        tool_name: str,
        action: str,
        condition: dict[str, Any],
        source: str = "user",
    ) -> PolicyRule:
        """创建并添加一条新规则。"""
        rule = PolicyRule(
            id=str(uuid.uuid4())[:8],
            tool_name=tool_name,
            action=action,
            condition=condition,
            source=source,
        )
        self.add_rule(rule)
        return rule

    def _load(self) -> None:
        """从 YAML 文件加载规则。"""
        if not self._path or not self._path.exists():
            return
        if yaml is None:
            return

        try:
            data = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        except Exception:
            return

        if not data or "rules" not in data:
            return

        for entry in data["rules"]:
            try:
                self._rules.append(PolicyRule(
                    id=entry.get("id", str(uuid.uuid4())[:8]),
                    tool_name=entry.get("tool_name", "*"),
                    action=entry.get("action", "approve"),
                    condition=entry.get("condition", {}),
                    source=entry.get("source", "user"),
                    created_at=entry.get("created_at", ""),
                ))
            except Exception:
                continue

    def _save(self) -> None:
        """持久化规则到 YAML 文件。"""
        if not self._path or yaml is None:
            return

        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "rules": [
                {
                    "id": r.id,
                    "tool_name": r.tool_name,
                    "action": r.action,
                    "condition": r.condition,
                    "source": r.source,
                    "created_at": r.created_at,
                }
                for r in self._rules
            ]
        }
        self._path.write_text(
            yaml.dump(data, default_flow_style=False, allow_unicode=True),
            encoding="utf-8",
        )
