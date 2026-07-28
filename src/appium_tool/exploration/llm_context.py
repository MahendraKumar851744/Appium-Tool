from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Sequence

from appium_tool.exploration.models import JsonObject


def build_llm_context(
    document: JsonObject,
    *,
    max_actions: int | None = None,
    max_text_items: int | None = None,
    max_elements: int | None = None,
    max_chars: int | None = None,
    include_screenshot_reference: bool = True,
    include_device_context: bool = True,
    include_system_context: bool = True,
    include_capture_quality: bool = True,
) -> str:
    """Convert a canonical Appium result into compact LLM ground truth."""
    _validate_document(document)
    elements = list(document.get("elements") or [])
    actions = _actionable_elements(elements)
    visible_text = _visible_text(elements)
    informative = _informative_elements(elements, actions)

    lines = [
        "# CURRENT ANDROID SCREEN CONTEXT",
        (
            "Treat this block as observed ground truth for the current screen. "
            "Do not invent controls or state that are not listed."
        ),
        "",
        "## SCREEN",
        f"- Screen ID: {_value(document, 'screen_id')}",
        (
            f"- App: {_value(document, 'apk.label')} "
            f"({_value(document, 'screen.package')})"
        ),
        f"- Activity: {_value(document, 'screen.activity')}",
        (
            f"- Foreground: {_yes_no(_get(document, 'screen.app_in_foreground'))}; "
            f"orientation: {_value(document, 'screen.orientation')}; "
            f"window: {_size(_get(document, 'screen.window_size'))}"
        ),
        (
            f"- Capture: {_value(document, 'captured_at')}; "
            f"stable: {_yes_no(_get(document, 'stability.stable'))} "
            f"({_value(document, 'stability.samples')} samples, "
            f"{_value(document, 'stability.duration_ms')} ms)"
        ),
    ]
    if include_screenshot_reference:
        lines.append(
            f"- Screenshot: {_value(document, 'artifacts.screenshots.full')}"
        )
    lines.extend(["", f"## AVAILABLE ACTIONS ({len(actions)})"])

    if actions:
        for element in _limited(actions, max_actions):
            lines.append(_format_action(element, elements))
        if max_actions and len(actions) > max_actions:
            lines.append(f"- â€¦ {len(actions) - max_actions} additional actions omitted")
    else:
        lines.append("- No actionable elements were exposed by UIAutomator.")

    lines.extend(["", f"## VISIBLE TEXT ({len(visible_text)} occurrences)"])
    if visible_text:
        for item in _limited(visible_text, max_text_items):
            lines.append(
                f'- [{item["id"]}] "{_clean(item["text"])}" '
                f"| {_short_class(item.get('class'))} | {item.get('bounds_raw') or 'no bounds'}"
            )
        if max_text_items and len(visible_text) > max_text_items:
            lines.append(
                f"- â€¦ {len(visible_text) - max_text_items} additional text items omitted"
            )
    else:
        lines.append("- No visible text was exposed by UIAutomator.")

    if include_system_context:
        lines.extend(
            [
                "",
                "## UI STATE",
                f"- Dialog present: {_yes_no(_get(document, 'system.dialog.present'))}",
                (
                    "- Permission prompt present: "
                    f"{_yes_no(_get(document, 'system.permission_prompt.present'))}"
                ),
                f"- Keyboard visible: {_yes_no(_get(document, 'system.keyboard_visible'))}",
                (
                    f"- Status bar: "
                    f"{_bar_summary(_get(document, 'system.status_bar'))}"
                ),
                (
                    f"- Navigation bar: "
                    f"{_bar_summary(_get(document, 'system.navigation_bar'))}; "
                    f"mode: {_value(document, 'system.navigation.mode')}"
                ),
            ]
        )

    if include_device_context:
        lines.extend(
            [
                "",
                "## DEVICE CONTEXT",
                (
                    f"- {_value(document, 'device.manufacturer')} "
                    f"{_value(document, 'device.model')}; "
                    f"Android {_value(document, 'device.android_version')} "
                    f"(API {_value(document, 'device.api_level')})"
                ),
                (
                    f"- Display: {_size(_get(document, 'display.physical_size'))}; "
                    f"density: {_value(document, 'display.physical_density_dpi')} dpi; "
                    f"rotation: {_value(document, 'display.rotation')}"
                ),
                (
                    f"- Locale: {_value(document, 'device.locale')}; "
                    f"timezone: {_value(document, 'device.timezone')}"
                ),
            ]
        )

    lines.extend(["", f"## OTHER IMPORTANT ELEMENTS ({len(informative)})"])

    if informative:
        for element in _limited(informative, max_elements):
            lines.append(_format_element(element))
        if max_elements and len(informative) > max_elements:
            lines.append(
                f"- â€¦ {len(informative) - max_elements} additional elements omitted"
            )
    else:
        lines.append("- None beyond the actions and visible text above.")

    errors = list(document.get("collection_errors") or [])
    summary = document.get("summary") or {}
    lines.extend(
        [
            "",
            "## CAPTURE QUALITY",
            (
                f"- Elements: {summary.get('total_elements', len(elements))} total, "
                f"{summary.get('visible_elements', '?')} visible, "
                f"{summary.get('app_elements', '?')} app, "
                f"{summary.get('system_elements', '?')} system."
            ),
            (
                f"- Collection errors: {len(errors)}"
                + (f" â€” {_clean(json.dumps(errors, ensure_ascii=False))}" if errors else ".")
            ),
            "",
            "## CONTEXT COVERAGE",
            (
                f"- Included: {_included_count(len(actions), max_actions)} actions, "
                f"{_included_count(len(visible_text), max_text_items)} visible text "
                f"occurrences, and {_included_count(len(informative), max_elements)} "
                "other semantic elements."
            ),
            (
                "- Visual evidence: pass the referenced screenshot alongside this "
                "text when the LLM supports image input."
            ),
            (
                "- Full-fidelity fallback: appium-result.json retains every captured "
                "element, raw hierarchy, system detail, capability, hash, and artifact."
            ),
            (
                "- Omitted intentionally: raw XML, full Appium capabilities, "
                "non-semantic layout containers, hashes, and screenshot pixel "
                "statistics; these remain available in the canonical result."
            ),
        ]
    )
    if not include_capture_quality:
        quality_heading = lines.index("## CAPTURE QUALITY")
        coverage_heading = lines.index("## CONTEXT COVERAGE")
        del lines[max(0, quality_heading - 1) : coverage_heading]

    context = "\n".join(lines).strip() + "\n"
    if not max_chars or len(context) <= max_chars:
        return context
    marker = "\n\n[Context truncated to configured character limit.]\n"
    if max_chars <= len(marker):
        return marker[:max_chars]
    return context[: max(0, max_chars - len(marker))].rstrip() + marker


def context_inventory(document: JsonObject) -> JsonObject:
    """Return the semantic item counts used by the context projection."""
    _validate_document(document)
    elements = list(document.get("elements") or [])
    actions = _actionable_elements(elements)
    return {
        "actions": len(actions),
        "visible_text_occurrences": len(_visible_text(elements)),
        "semantic_elements": len(_informative_elements(elements, actions)),
    }


def load_llm_context(
    input_path: str | Path,
    **options: Any,
) -> str:
    path = Path(input_path).expanduser().resolve()
    document = json.loads(path.read_text(encoding="utf-8"))
    return build_llm_context(document, **options)


def estimate_tokens(context: str) -> int:
    """Return a dependency-free, conservative token estimate."""
    return (len(context) + 3) // 4


def _validate_document(document: JsonObject) -> None:
    if not isinstance(document, dict):
        raise ValueError("Appium result must be a JSON object.")
    if document.get("contract") != "appium.screen_capture":
        raise ValueError(
            "Expected an appium.screen_capture canonical result document."
        )
    for field in ("screen", "system"):
        if not isinstance(document.get(field), dict):
            raise ValueError(
                f"Appium result field '{field}' must be a JSON object."
            )
    elements = document.get("elements")
    if not isinstance(elements, list) or not all(
        isinstance(item, dict) for item in elements
    ):
        raise ValueError(
            "Appium result field 'elements' must be an array of objects."
        )


def _actionable_elements(elements: list[JsonObject]) -> list[JsonObject]:
    return [
        item
        for item in elements
        if item.get("displayed") is not False
        and (
            item.get("interaction")
            or item.get("clickable")
            or item.get("long_clickable")
            or item.get("editable")
            or item.get("scrollable")
        )
    ]


def _visible_text(elements: list[JsonObject]) -> list[JsonObject]:
    result: list[JsonObject] = []
    seen: set[tuple[str, str]] = set()
    for item in elements:
        if item.get("displayed") is False:
            continue
        for candidate in (item.get("text"), item.get("content_description")):
            cleaned = _clean(candidate)
            identity = (cleaned.casefold(), str(item.get("bounds_raw") or ""))
            if not cleaned or identity in seen:
                continue
            seen.add(identity)
            result.append({**item, "text": cleaned})
    return result


def _informative_elements(
    elements: list[JsonObject],
    actions: list[JsonObject],
) -> list[JsonObject]:
    action_ids = {item.get("id") for item in actions}
    result: list[JsonObject] = []
    generic_labels = {
        "action bar root",
        "content",
        "container",
        "main",
        "root",
    }
    for item in elements:
        if (
            item.get("id") in action_ids
            or item.get("displayed") is False
            or item.get("text")
        ):
            continue
        semantic_state = any(
            item.get(key)
            for key in ("checkable", "checked", "selected", "focused")
        )
        description = _clean(item.get("content_description"))
        resource_label = _resource_label(item.get("resource_id"))
        if description or semantic_state or (
            resource_label and resource_label.casefold() not in generic_labels
        ):
            result.append(item)
    return result


def _format_action(
    element: JsonObject,
    elements: list[JsonObject],
) -> str:
    label = _element_label(element, elements)
    states = [
        f"{key}={_yes_no(element.get(key))}"
        for key in (
            "displayed",
            "enabled",
            "clickable",
            "long_clickable",
            "editable",
            "scrollable",
            "checkable",
            "checked",
            "selected",
            "focused",
        )
    ]
    action = element.get("interaction") or (
        "type" if element.get("editable") else "scroll" if element.get("scrollable") else "tap"
    )
    details = [
        f"[{element.get('id', '?')}] {action}",
        f'"{label}"' if label else "unlabelled",
        _short_class(element.get("class")),
    ]
    if element.get("resource_id"):
        details.append(f"id={element['resource_id']}")
    if element.get("bounds_raw"):
        details.append(f"bounds={element['bounds_raw']}")
    if states:
        details.append("state=" + ",".join(states))
    return "- " + " | ".join(details)


def _format_element(element: JsonObject) -> str:
    label = (
        _clean(element.get("content_description"))
        or _resource_label(element.get("resource_id"))
        or "unlabelled"
    )
    return (
        f"- [{element.get('id', '?')}] {label} | "
        f"{_short_class(element.get('class'))} | "
        f"id={element.get('resource_id') or 'none'} | "
        f"bounds={element.get('bounds_raw') or 'none'}"
    )


def _element_label(
    element: JsonObject,
    elements: list[JsonObject],
) -> str:
    direct = _clean(element.get("text")) or _clean(
        element.get("content_description")
    )
    if direct:
        return direct
    xpath = str(element.get("xpath") or "")
    descendant_text: list[str] = []
    if xpath:
        prefix = xpath + "/"
        for candidate in elements:
            if str(candidate.get("xpath") or "").startswith(prefix):
                text = _clean(candidate.get("text")) or _clean(
                    candidate.get("content_description")
                )
                if text and text not in descendant_text:
                    descendant_text.append(text)
                if len(descendant_text) == 3:
                    break
    if descendant_text:
        return " / ".join(descendant_text)
    return _resource_label(element.get("resource_id"))


def _resource_label(value: Any) -> str:
    resource = _clean(value)
    if not resource:
        return ""
    name = resource.rsplit("/", 1)[-1]
    name = re.sub(r"^(btn|fl|iv|tv|txt|et|ll|rl|cl)_?", "", name)
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    return name.replace("_", " ").strip()


def _bar_summary(value: Any) -> str:
    if not isinstance(value, dict):
        return "unknown"
    visible = _yes_no(value.get("visible"))
    bounds = value.get("bounds") or []
    first = bounds[0] if bounds else None
    return f"visible={visible}, bounds={_bounds(first)}, source={value.get('source') or 'unknown'}"


def _bounds(value: Any) -> str:
    if not isinstance(value, dict):
        return "unknown"
    return (
        f"[{value.get('left')},{value.get('top')}]"
        f"[{value.get('right')},{value.get('bottom')}]"
    )


def _size(value: Any) -> str:
    if not isinstance(value, dict):
        return "unknown"
    return f"{value.get('width', '?')}x{value.get('height', '?')}"


def _short_class(value: Any) -> str:
    return _clean(value).rsplit(".", 1)[-1] or "unknown class"


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _yes_no(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def _limited(items: list[JsonObject], limit: int | None) -> list[JsonObject]:
    return items if not limit or limit < 1 else items[:limit]


def _included_count(total: int, limit: int | None) -> str:
    included = total if not limit or limit < 1 else min(total, limit)
    return f"{included}/{total}"


def _get(document: JsonObject, path: str) -> Any:
    current: Any = document
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _value(document: JsonObject, path: str) -> str:
    value = _get(document, path)
    if value is None or value == "":
        return "unknown"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return _clean(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create compact LLM context from appium-result.json."
    )
    parser.add_argument("input", type=Path, help="Path to appium-result.json")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Optional output path. Prints to stdout when omitted.",
    )
    parser.add_argument(
        "--max-actions",
        type=int,
        default=0,
        help="Optional action limit; 0 keeps all actions (default).",
    )
    parser.add_argument(
        "--max-text-items",
        type=int,
        default=0,
        help="Optional visible-text limit; 0 keeps all occurrences (default).",
    )
    parser.add_argument(
        "--max-elements",
        type=int,
        default=0,
        help="Optional semantic-element limit; 0 keeps all (default).",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=0,
        help="Optional character limit; 0 disables truncation (default).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    context = load_llm_context(
        args.input,
        max_actions=args.max_actions or None,
        max_text_items=args.max_text_items or None,
        max_elements=args.max_elements or None,
        max_chars=args.max_chars or None,
    )
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(context, encoding="utf-8")
    else:
        print(context, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

