from __future__ import annotations

import fnmatch
import re
from typing import Any, Callable

from stb.errors import StbError


QUERY_OPERATORS = {
    "eq",
    "ne",
    "lt",
    "le",
    "gt",
    "ge",
    "in",
    "not_in",
    "exists",
    "glob",
    "regex",
    "all",
    "any",
    "not",
}


def evaluate_where(
    node: dict[str, Any] | None, property_getter: Callable[[str], Any]
) -> bool:
    if node is None:
        return True
    if not isinstance(node, dict):
        raise StbError("invalid_request", "where node must be an object")
    op = node.get("op")
    if op not in QUERY_OPERATORS:
        raise StbError("invalid_request", f"unsupported where operator: {op}")
    if op in {"all", "any"}:
        args = node.get("args")
        if not isinstance(args, list):
            raise StbError("invalid_request", f"where.{op} requires args[]")
        values = [evaluate_where(child, property_getter) for child in args]
        return all(values) if op == "all" else any(values)
    if op == "not":
        if "arg" not in node:
            raise StbError("invalid_request", "where.not requires arg")
        return not evaluate_where(node["arg"], property_getter)

    prop = node.get("property")
    if not isinstance(prop, str) or not prop:
        raise StbError("invalid_request", f"where.{op} requires property")
    value = property_getter(prop)
    if op == "exists":
        expected = bool(node.get("value", True))
        return (value is not None) == expected
    expected = node.get("value")
    if op == "eq":
        return value == expected
    if op == "ne":
        return value != expected
    if op == "lt":
        return value is not None and value < expected
    if op == "le":
        return value is not None and value <= expected
    if op == "gt":
        return value is not None and value > expected
    if op == "ge":
        return value is not None and value >= expected
    if op in {"in", "not_in"}:
        if not isinstance(expected, list):
            raise StbError("invalid_request", f"where.{op} value must be an array")
        matched = value in expected
        return matched if op == "in" else not matched
    if not isinstance(value, str) or not isinstance(expected, str):
        return False
    if op == "glob":
        return fnmatch.fnmatchcase(value, expected)
    try:
        return re.search(expected, value) is not None
    except re.error as exc:
        raise StbError("invalid_request", f"invalid where regex: {exc}") from exc
