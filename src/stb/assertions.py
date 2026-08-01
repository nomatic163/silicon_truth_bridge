from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Callable


_IDENTIFIER_RE = re.compile(
    r"(?<![$'`])\b[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)*\b"
)
_MACRO_RE = re.compile(r"`[A-Za-z_][A-Za-z0-9_$]*")
_SYSTEM_FUNCTION_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_$]*")
_SAMPLED_FUNCTIONS = {
    "$past",
    "$rose",
    "$fell",
    "$stable",
}
_EXPRESSION_KEYWORDS = {
    "and",
    "assert",
    "assume",
    "begin",
    "disable",
    "else",
    "end",
    "endproperty",
    "false",
    "iff",
    "negedge",
    "or",
    "posedge",
    "property",
    "true",
}
_ADVANCED_KEYWORDS = (
    "and",
    "or",
    "first_match",
    "intersect",
    "throughout",
    "within",
    "until",
    "until_with",
    "s_until",
    "s_until_with",
    "always",
    "eventually",
    "nexttime",
    "s_nexttime",
    "strong",
    "weak",
)


def strip_numbered_source(text: str) -> str:
    return "\n".join(
        re.sub(r"^\s*\d+:\s?", "", line)
        for line in text.splitlines()
    )


def parse_assertion_source(
    assertion_source: str,
    *,
    property_source: str | None = None,
) -> dict[str, Any]:
    diagnostics: list[dict[str, Any]] = []
    assertion = _extract_assertion(assertion_source)
    if assertion is None:
        return _unsupported_result(
            assertion_source,
            diagnostics,
            "assertion_statement_not_found",
        )

    property_form = "inline"
    property_name = None
    property_declaration = None
    body = assertion["property_text"]
    if re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)*",
        body.strip(),
    ):
        property_form = "named"
        property_name = body.strip()
        if property_source is None:
            return _unsupported_result(
                assertion["raw"],
                diagnostics,
                "named_property_source_unavailable",
                assertion=assertion,
                property_form=property_form,
                property_name=property_name,
            )
        property_declaration = _extract_property_declaration(property_source)
        if property_declaration is None:
            return _unsupported_result(
                assertion["raw"],
                diagnostics,
                "property_declaration_not_found",
                assertion=assertion,
                property_form=property_form,
                property_name=property_name,
            )
        if property_declaration["formal_arguments"]:
            return _unsupported_result(
                assertion["raw"],
                diagnostics,
                "property_formal_arguments_unsupported",
                assertion=assertion,
                property_form=property_form,
                property_name=property_name,
                property_declaration=property_declaration,
            )
        body = property_declaration["body"]
    elif _looks_like_property_call(body):
        return _unsupported_result(
            assertion["raw"],
            diagnostics,
            "property_arguments_unsupported",
            assertion=assertion,
        )

    parsed = _parse_property_body(body)
    parsed["assertion"] = {
        "kind": assertion["kind"],
        "label": assertion["label"],
        "property_form": property_form,
        "property_name": property_name,
        "raw": assertion["raw"],
    }
    if property_declaration is not None:
        parsed["property_declaration"] = property_declaration
    parsed["diagnostics"] = diagnostics + parsed["diagnostics"]
    return parsed


def resolve_structure_dependencies(
    structure: dict[str, Any],
    resolver: Callable[[str], dict[str, Any] | None],
) -> dict[str, Any]:
    result = deepcopy(structure)
    expressions = list(_iter_expressions(result))
    statuses = []
    for expression in expressions:
        if expression.get("macro_tokens"):
            expression["dependency_status"] = "opaque"
            expression["resolved_identifiers"] = []
            statuses.append("opaque")
            continue
        if expression.get("unsupported_system_functions") or any(
            item.get("status") != "exact"
            for item in expression.get("sampled_functions", [])
        ):
            expression["dependency_status"] = "opaque"
            statuses.append("opaque")
        else:
            statuses.append("exact")
        resolved = []
        unresolved = []
        for token in expression.get("identifier_tokens", []):
            summary = resolver(token)
            if summary is None:
                unresolved.append(token)
            else:
                resolved.append({"token": token, "object": summary})
        expression["resolved_identifiers"] = resolved
        expression["unresolved_identifiers"] = unresolved
        if expression["dependency_status"] != "opaque":
            expression["dependency_status"] = (
                "exact" if not unresolved else "unresolved"
            )
            statuses[-1] = expression["dependency_status"]
    if "opaque" in statuses:
        result["fidelity"]["dependencies"] = "opaque"
    elif "unresolved" in statuses:
        result["fidelity"]["dependencies"] = "unresolved"
    elif statuses:
        result["fidelity"]["dependencies"] = "exact"
    else:
        result["fidelity"]["dependencies"] = "unavailable"
    result["sampling_requirements"] = _sampling_requirements(result)
    return result


def _unsupported_result(
    raw: str,
    diagnostics: list[dict[str, Any]],
    reason: str,
    **fields: Any,
) -> dict[str, Any]:
    return {
        "schema_version": "stb.assertion-structure.v1",
        "fidelity": {
            "syntax": "unsupported",
            "temporal": "unsupported",
            "dependencies": "unavailable",
        },
        "raw": raw.strip(),
        "unsupported_constructs": [reason],
        "diagnostics": diagnostics
        + [{"code": "STB-ASRT-U001", "severity": "warning", "reason": reason}],
        **fields,
    }


def _iter_expressions(structure: dict[str, Any]):
    clock = structure.get("clock")
    if isinstance(clock, dict) and isinstance(clock.get("expression"), dict):
        yield clock["expression"]
    disable = structure.get("disable_condition")
    if isinstance(disable, dict):
        yield disable
    for side in ("antecedent", "consequent"):
        section = structure.get(side)
        if not isinstance(section, dict):
            continue
        for step in section.get("steps", []):
            if isinstance(step, dict) and isinstance(step.get("expression"), dict):
                yield step["expression"]


def _sampling_requirements(structure: dict[str, Any]) -> list[dict[str, Any]]:
    requirements = []
    clock = structure.get("clock")
    if isinstance(clock, dict):
        requirements.append(
            {
                "role": "clock",
                "edge": clock.get("edge"),
                "expression": clock.get("expression"),
            }
        )
    disable = structure.get("disable_condition")
    if isinstance(disable, dict):
        requirements.append({"role": "disable", "expression": disable})
    if structure.get("fidelity", {}).get("temporal") != "exact":
        return requirements
    for side in ("antecedent", "consequent"):
        for step in structure.get(side, {}).get("steps", []):
            requirements.append(
                {
                    "role": side,
                    "relative_window": step["relative_window"],
                    "expression": step["expression"],
                }
            )
    return requirements


def _extract_assertion(source: str) -> dict[str, Any] | None:
    mask = _mask_noncode(source)
    match = re.search(
        r"(?:(?P<label>[A-Za-z_][A-Za-z0-9_$]*)\s*:\s*)?"
        r"(?P<kind>assert)\s+property\s*\(",
        mask,
    )
    if match is None:
        return None
    open_index = mask.find("(", match.start(), match.end())
    close_index = _find_matching(mask, open_index, "(", ")")
    if close_index is None:
        return None
    end_index = close_index + 1
    while end_index < len(mask) and mask[end_index].isspace():
        end_index += 1
    if end_index < len(mask) and mask[end_index] == ";":
        end_index += 1
    return {
        "kind": match.group("kind"),
        "label": match.group("label"),
        "property_text": source[open_index + 1 : close_index].strip(),
        "raw": source[match.start() : end_index].strip(),
    }


def _extract_property_declaration(source: str) -> dict[str, Any] | None:
    mask = _mask_noncode(source)
    match = re.search(
        r"\bproperty\s+(?P<name>[A-Za-z_][A-Za-z0-9_$]*)",
        mask,
    )
    if match is None:
        return None
    header_end = _find_top_level_char(mask, ";", match.end())
    if header_end is None:
        return None
    end_match = re.search(r"\bendproperty\b", mask[header_end + 1 :])
    if end_match is None:
        return None
    body_end = header_end + 1 + end_match.start()
    header = source[match.end() : header_end]
    formal_arguments = ""
    open_index = header.find("(")
    if open_index >= 0:
        header_mask = _mask_noncode(header)
        close_index = _find_matching(header_mask, open_index, "(", ")")
        if close_index is None:
            return None
        formal_arguments = header[open_index + 1 : close_index].strip()
    return {
        "name": match.group("name"),
        "formal_arguments": formal_arguments,
        "body": source[header_end + 1 : body_end].strip(),
        "raw": source[match.start() : body_end + len("endproperty")].strip(),
    }


def _parse_property_body(body: str) -> dict[str, Any]:
    diagnostics: list[dict[str, Any]] = []
    remaining = _trim_outer_parentheses(body.strip().rstrip(";").strip())
    clock, remaining = _consume_clock(remaining)
    if clock is None:
        return _property_unsupported(body, "explicit_clock_required")

    disable, remaining = _consume_disable(remaining)
    if disable is None and re.match(r"disable\b", remaining):
        return _property_unsupported(body, "invalid_disable_condition")
    remaining = _trim_outer_parentheses(remaining.strip().rstrip(";").strip())
    implication = _find_top_level_implication(remaining)
    if implication is None:
        return _property_unsupported(body, "implication_required")
    operator, index = implication
    antecedent_raw = remaining[:index].strip()
    consequent_raw = remaining[index + len(operator) :].strip()
    if not antecedent_raw or not consequent_raw:
        return _property_unsupported(body, "empty_implication_operand")

    antecedent = _parse_sequence(antecedent_raw)
    consequent = _parse_sequence(consequent_raw)
    unsupported = list(
        dict.fromkeys(
            antecedent.get("unsupported_constructs", [])
            + consequent.get("unsupported_constructs", [])
        )
    )
    temporal_status = "unsupported" if unsupported else "exact"
    dependencies = _dependency_status(
        [clock["expression"]]
        + ([disable] if disable else [])
        + antecedent.get("steps", [])
        + consequent.get("steps", [])
    )
    result = {
        "schema_version": "stb.assertion-structure.v1",
        "fidelity": {
            "syntax": "exact",
            "temporal": temporal_status,
            "dependencies": dependencies,
        },
        "raw": body.strip(),
        "clock": clock,
        "disable_condition": disable,
        "implication": {
            "operator": operator,
            "consequent_start": (
                "same_cycle_as_antecedent_match"
                if operator == "|->"
                else "next_cycle_after_antecedent_match"
            ),
        },
        "antecedent": {
            "raw": antecedent_raw,
            "steps": antecedent.get("steps", []) if not unsupported else [],
        },
        "consequent": {
            "raw": consequent_raw,
            "steps": consequent.get("steps", []) if not unsupported else [],
        },
        "unsupported_constructs": unsupported,
        "diagnostics": diagnostics,
    }
    if unsupported:
        result["diagnostics"].append(
            {
                "code": "STB-ASRT-U002",
                "severity": "warning",
                "constructs": unsupported,
            }
        )
    return result


def _property_unsupported(body: str, reason: str) -> dict[str, Any]:
    return {
        "schema_version": "stb.assertion-structure.v1",
        "fidelity": {
            "syntax": "unsupported",
            "temporal": "unsupported",
            "dependencies": "unavailable",
        },
        "raw": body.strip(),
        "unsupported_constructs": [reason],
        "diagnostics": [
            {"code": "STB-ASRT-U003", "severity": "warning", "reason": reason}
        ],
    }


def _consume_clock(text: str) -> tuple[dict[str, Any] | None, str]:
    value = text.lstrip()
    if not value.startswith("@"):
        return None, text
    index = 1
    while index < len(value) and value[index].isspace():
        index += 1
    if index >= len(value) or value[index] != "(":
        return None, text
    mask = _mask_noncode(value)
    close_index = _find_matching(mask, index, "(", ")")
    if close_index is None:
        return None, text
    event = value[index + 1 : close_index].strip()
    match = re.fullmatch(r"(posedge|negedge)\s+(.+)", event, re.DOTALL)
    if match is None:
        return None, text
    expression = _parse_expression(match.group(2).strip())
    return (
        {
            "raw": value[: close_index + 1].strip(),
            "edge": match.group(1),
            "expression": expression,
        },
        value[close_index + 1 :].lstrip(),
    )


def _consume_disable(text: str) -> tuple[dict[str, Any] | None, str]:
    value = text.lstrip()
    match = re.match(r"disable\s+iff\s*\(", _mask_noncode(value))
    if match is None:
        return None, text
    open_index = value.find("(", 0, match.end())
    close_index = _find_matching(_mask_noncode(value), open_index, "(", ")")
    if close_index is None:
        return None, text
    return (
        _parse_expression(value[open_index + 1 : close_index].strip()),
        value[close_index + 1 :].lstrip(),
    )


def _find_top_level_implication(text: str) -> tuple[str, int] | None:
    mask = _mask_noncode(text)
    depth = {"(": 0, "[": 0, "{": 0}
    pairs = {")": "(", "]": "[", "}": "{"}
    for index, char in enumerate(mask):
        if char in depth:
            depth[char] += 1
            continue
        if char in pairs:
            depth[pairs[char]] = max(0, depth[pairs[char]] - 1)
            continue
        if any(depth.values()):
            continue
        if mask.startswith("|->", index):
            return "|->", index
        if mask.startswith("|=>", index):
            return "|=>", index
    return None


def _parse_sequence(text: str) -> dict[str, Any]:
    value = _trim_outer_parentheses(text.strip())
    unsupported = _advanced_constructs(value)
    if unsupported:
        return {"steps": [], "unsupported_constructs": unsupported}

    mask = _mask_noncode(value)
    steps: list[dict[str, Any]] = []
    min_offset = 0
    max_offset = 0
    expression_start = 0
    index = 0
    saw_delay = False
    depth = {"(": 0, "[": 0, "{": 0}
    pairs = {")": "(", "]": "[", "}": "{"}
    while index < len(mask):
        char = mask[index]
        if char in depth:
            depth[char] += 1
            index += 1
            continue
        if char in pairs:
            depth[pairs[char]] = max(0, depth[pairs[char]] - 1)
            index += 1
            continue
        if not any(depth.values()) and mask.startswith("##", index):
            expression = value[expression_start:index].strip()
            if expression:
                steps.append(
                    _sequence_step(expression, min_offset, max_offset)
                )
            elif saw_delay:
                return {
                    "steps": [],
                    "unsupported_constructs": ["empty_sequence_term"],
                }
            delay = _parse_delay(value, mask, index + 2)
            if delay is None:
                return {
                    "steps": [],
                    "unsupported_constructs": ["invalid_or_unbounded_delay"],
                }
            min_offset += delay["min"]
            max_offset += delay["max"]
            index = delay["end"]
            expression_start = index
            saw_delay = True
            continue
        index += 1

    expression = value[expression_start:].strip()
    if expression:
        steps.append(_sequence_step(expression, min_offset, max_offset))
    if not steps:
        return {"steps": [], "unsupported_constructs": ["empty_sequence"]}
    return {"steps": steps, "unsupported_constructs": []}


def _sequence_step(raw: str, min_offset: int, max_offset: int) -> dict[str, Any]:
    return {
        "relative_window": {"min_cycles": min_offset, "max_cycles": max_offset},
        "expression": _parse_expression(_trim_outer_parentheses(raw)),
    }


def _parse_delay(text: str, mask: str, index: int) -> dict[str, int] | None:
    while index < len(mask) and mask[index].isspace():
        index += 1
    number = re.match(r"\d+", mask[index:])
    if number is not None:
        value = int(number.group(0))
        return {"min": value, "max": value, "end": index + len(number.group(0))}
    if index >= len(mask) or mask[index] != "[":
        return None
    close_index = _find_matching(mask, index, "[", "]")
    if close_index is None:
        return None
    content = text[index + 1 : close_index].strip()
    match = re.fullmatch(r"(\d+)\s*:\s*(\d+)", content)
    if match is None:
        single = re.fullmatch(r"\d+", content)
        if single is None:
            return None
        minimum = maximum = int(single.group(0))
    else:
        minimum = int(match.group(1))
        maximum = int(match.group(2))
        if maximum < minimum:
            return None
    return {"min": minimum, "max": maximum, "end": close_index + 1}


def _parse_expression(text: str) -> dict[str, Any]:
    macros = list(dict.fromkeys(_MACRO_RE.findall(text)))
    sampled = _sampled_functions(text)
    system_functions = list(dict.fromkeys(_SYSTEM_FUNCTION_RE.findall(text)))
    unsupported_system_functions = [
        name for name in system_functions if name not in _SAMPLED_FUNCTIONS
    ]
    identifiers = []
    for match in _IDENTIFIER_RE.finditer(_mask_noncode(text)):
        name = match.group(0)
        if name in _EXPRESSION_KEYWORDS:
            continue
        identifiers.append(name)
    return {
        "raw": text.strip(),
        "representation": "raw",
        "dependency_status": (
            "opaque"
            if macros
            or unsupported_system_functions
            or any(item["status"] != "exact" for item in sampled)
            else "unresolved"
        ),
        "identifier_tokens": list(dict.fromkeys(identifiers)),
        "macro_tokens": macros,
        "sampled_functions": sampled,
        "unsupported_system_functions": unsupported_system_functions,
    }


def _sampled_functions(text: str) -> list[dict[str, Any]]:
    mask = _mask_noncode(text)
    result = []
    index = 0
    while index < len(mask):
        match = re.search(
            r"\$(past|rose|fell|stable)\s*\(",
            mask[index:],
        )
        if match is None:
            break
        start = index + match.start()
        open_index = mask.find("(", start, index + match.end())
        close_index = _find_matching(mask, open_index, "(", ")")
        if close_index is None:
            break
        name = "$" + match.group(1)
        args = _split_top_level_commas(text[open_index + 1 : close_index])
        entry: dict[str, Any] = {
            "name": name,
            "raw": text[start : close_index + 1].strip(),
            "arguments": [arg.strip() for arg in args],
            "status": "exact",
        }
        if name == "$past":
            if len(args) == 1:
                entry["depth_cycles"] = 1
            elif len(args) == 2 and re.fullmatch(r"\d+", args[1].strip()):
                entry["depth_cycles"] = int(args[1].strip())
            else:
                entry["status"] = "opaque"
        elif len(args) != 1:
            entry["status"] = "opaque"
        result.append(entry)
        index = close_index + 1
    return result


def _dependency_status(values: list[Any]) -> str:
    expressions: list[dict[str, Any]] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, dict) and "expression" in value:
            expressions.append(value["expression"])
        elif isinstance(value, dict) and "raw" in value:
            expressions.append(value)
    if any(
        item.get("macro_tokens")
        or item.get("unsupported_system_functions")
        or any(
            sampled.get("status") != "exact"
            for sampled in item.get("sampled_functions", [])
        )
        for item in expressions
    ):
        return "opaque"
    return "unresolved"


def _advanced_constructs(text: str) -> list[str]:
    mask = _mask_noncode(text)
    found = []
    for keyword in _ADVANCED_KEYWORDS:
        if re.search(rf"\b{re.escape(keyword)}\b", mask):
            found.append(keyword)
    if re.search(r"\[\s*(?:\*|=|->)", mask):
        found.append("sequence_repetition")
    return found


def _looks_like_property_call(text: str) -> bool:
    return bool(
        re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)*\s*\(.*\)",
            text.strip(),
            re.DOTALL,
        )
    )


def _trim_outer_parentheses(text: str) -> str:
    value = text.strip()
    while value.startswith("("):
        mask = _mask_noncode(value)
        close_index = _find_matching(mask, 0, "(", ")")
        if close_index != len(value) - 1:
            break
        value = value[1:-1].strip()
    return value


def _split_top_level_commas(text: str) -> list[str]:
    mask = _mask_noncode(text)
    result = []
    start = 0
    depth = {"(": 0, "[": 0, "{": 0}
    pairs = {")": "(", "]": "[", "}": "{"}
    for index, char in enumerate(mask):
        if char in depth:
            depth[char] += 1
        elif char in pairs:
            depth[pairs[char]] = max(0, depth[pairs[char]] - 1)
        elif char == "," and not any(depth.values()):
            result.append(text[start:index])
            start = index + 1
    result.append(text[start:])
    return result


def _find_top_level_char(text: str, wanted: str, start: int = 0) -> int | None:
    depth = {"(": 0, "[": 0, "{": 0}
    pairs = {")": "(", "]": "[", "}": "{"}
    for index in range(start, len(text)):
        char = text[index]
        if char in depth:
            depth[char] += 1
        elif char in pairs:
            depth[pairs[char]] = max(0, depth[pairs[char]] - 1)
        elif char == wanted and not any(depth.values()):
            return index
    return None


def _find_matching(
    text: str,
    open_index: int,
    opener: str,
    closer: str,
) -> int | None:
    if open_index < 0 or open_index >= len(text) or text[open_index] != opener:
        return None
    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == opener:
            depth += 1
        elif text[index] == closer:
            depth -= 1
            if depth == 0:
                return index
    return None


def _mask_noncode(text: str) -> str:
    chars = list(text)
    index = 0
    state = "code"
    while index < len(chars):
        char = chars[index]
        next_char = chars[index + 1] if index + 1 < len(chars) else ""
        if state == "code":
            if char == "/" and next_char == "/":
                chars[index] = chars[index + 1] = " "
                index += 2
                state = "line_comment"
                continue
            if char == "/" and next_char == "*":
                chars[index] = chars[index + 1] = " "
                index += 2
                state = "block_comment"
                continue
            if char == '"':
                chars[index] = " "
                index += 1
                state = "string"
                continue
        elif state == "line_comment":
            if char == "\n":
                state = "code"
            else:
                chars[index] = " "
            index += 1
            continue
        elif state == "block_comment":
            if char == "*" and next_char == "/":
                chars[index] = chars[index + 1] = " "
                index += 2
                state = "code"
                continue
            if char != "\n":
                chars[index] = " "
            index += 1
            continue
        elif state == "string":
            if char == "\\" and next_char:
                chars[index] = chars[index + 1] = " "
                index += 2
                continue
            if char == '"':
                chars[index] = " "
                state = "code"
            elif char != "\n":
                chars[index] = " "
            index += 1
            continue
        index += 1
    return "".join(chars)
