from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from appium_tool.errors import UnsupportedScreenContractError
from appium_tool.exploration.llm_context import (
    build_llm_context,
    context_inventory,
    estimate_tokens,
)
from appium_tool.exploration.models import JsonObject


IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
INTEGER_OPTIONS = {
    "max_actions": 10_000,
    "max_text_items": 10_000,
    "max_elements": 10_000,
    "max_characters": 1_000_000,
}
BOOLEAN_OPTIONS = {
    "include_screenshot_reference",
    "include_device_context",
    "include_system_context",
    "include_capture_quality",
}
DEFAULT_OPTIONS: JsonObject = {
    "format": "markdown",
    "max_actions": 0,
    "max_text_items": 0,
    "max_elements": 0,
    "max_characters": 0,
    "include_screenshot_reference": True,
    "include_device_context": True,
    "include_system_context": True,
    "include_capture_quality": True,
}


class ContextExportError(RuntimeError):
    pass


class ContextValidationError(ContextExportError):
    pass


class ContextScreenNotFoundError(ContextExportError):
    pass


class ScreenContextExporter:
    """Resolve one canonical screen and project it into LLM-ready context."""

    def __init__(self, output_root: str | Path) -> None:
        self.output_root = Path(output_root).expanduser().resolve()

    def export(self, payload: JsonObject) -> JsonObject:
        if not isinstance(payload, dict):
            raise ContextValidationError("Request body must be a JSON object.")
        unexpected = sorted(set(payload) - {"screen_ref", "screen", "options"})
        if unexpected:
            raise ContextValidationError(
                f"Unsupported context fields: {', '.join(unexpected)}."
            )

        has_ref = "screen_ref" in payload
        has_screen = "screen" in payload
        if has_ref == has_screen:
            raise ContextValidationError(
                "Provide exactly one of 'screen_ref' or 'screen'."
            )

        source: JsonObject
        if has_ref:
            document, source = self._load_stored(payload["screen_ref"])
        else:
            document, source = self._load_inline(payload["screen"])

        options = self._options(payload.get("options", {}))
        self._validate_contract(document)
        inventory = context_inventory(document)
        builder_options = {
            "max_actions": self._limit(options["max_actions"]),
            "max_text_items": self._limit(options["max_text_items"]),
            "max_elements": self._limit(options["max_elements"]),
            "include_screenshot_reference": options[
                "include_screenshot_reference"
            ],
            "include_device_context": options["include_device_context"],
            "include_system_context": options["include_system_context"],
            "include_capture_quality": options["include_capture_quality"],
        }
        complete_text = build_llm_context(document, **builder_options)
        text = build_llm_context(
            document,
            **builder_options,
            max_chars=self._limit(options["max_characters"]),
        )
        character_truncated = text != complete_text
        coverage, warnings = self._coverage(
            inventory,
            options,
            character_truncated=character_truncated,
        )
        screenshot = (
            document.get("artifacts", {}).get("screenshots", {}).get("full")
            if options["include_screenshot_reference"]
            else None
        )
        return {
            "contract": "appium.llm_screen_context",
            "schema_version": 1,
            "format": "text/markdown",
            "text": text,
            "characters": len(text),
            "estimated_tokens": estimate_tokens(text),
            "source": {
                **source,
                "screen_id": document.get("screen_id"),
                "screen_contract": document.get("contract"),
                "screen_schema_version": document.get("schema_version"),
            },
            "coverage": {
                **coverage,
                "truncated": character_truncated
                or any(
                    not item["complete"]
                    for key, item in coverage.items()
                    if key != "truncated"
                ),
            },
            "visual_evidence": {
                "screenshot": screenshot,
                "should_accompany_context": bool(screenshot),
            },
            "warnings": warnings,
        }

    def _load_stored(self, value: Any) -> tuple[JsonObject, JsonObject]:
        if not isinstance(value, dict):
            raise ContextValidationError("'screen_ref' must be a JSON object.")
        if set(value) != {"session_id", "screen_id"}:
            raise ContextValidationError(
                "'screen_ref' must contain exactly 'session_id' and 'screen_id'."
            )
        session_id = value.get("session_id")
        screen_id = value.get("screen_id")
        if not self._identifier(session_id) or not self._identifier(screen_id):
            raise ContextValidationError(
                "'session_id' and 'screen_id' must be valid captured identifiers."
            )
        path = (
            self.output_root
            / str(session_id)
            / "screens"
            / str(screen_id)
            / "appium-result.json"
        )
        if not path.is_file():
            raise ContextScreenNotFoundError(
                f"Screen '{screen_id}' does not exist in session '{session_id}'."
            )
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise UnsupportedScreenContractError(
                "The stored canonical screen could not be read."
            ) from error
        return document, {"type": "stored", "session_id": session_id}

    @staticmethod
    def _load_inline(value: Any) -> tuple[JsonObject, JsonObject]:
        if not isinstance(value, dict):
            raise ContextValidationError("'screen' must be a JSON object.")
        return value, {"type": "inline", "session_id": None}

    @staticmethod
    def _options(value: Any) -> JsonObject:
        if not isinstance(value, dict):
            raise ContextValidationError("'options' must be a JSON object.")
        allowed = {"format", *INTEGER_OPTIONS, *BOOLEAN_OPTIONS}
        unexpected = sorted(set(value) - allowed)
        if unexpected:
            raise ContextValidationError(
                f"Unsupported context options: {', '.join(unexpected)}."
            )
        options = {**DEFAULT_OPTIONS, **value}
        if options["format"] != "markdown":
            raise ContextValidationError(
                "'options.format' currently supports only 'markdown'."
            )
        for name, maximum in INTEGER_OPTIONS.items():
            item = options[name]
            if isinstance(item, bool) or not isinstance(item, int):
                raise ContextValidationError(f"'options.{name}' must be an integer.")
            if item < 0 or item > maximum:
                raise ContextValidationError(
                    f"'options.{name}' must be between 0 and {maximum}."
                )
        for name in BOOLEAN_OPTIONS:
            if not isinstance(options[name], bool):
                raise ContextValidationError(
                    f"'options.{name}' must be a boolean."
                )
        return options

    @staticmethod
    def _validate_contract(document: JsonObject) -> None:
        if not isinstance(document, dict) or (
            document.get("contract") != "appium.screen_capture"
            or document.get("schema_version") != 1
        ):
            raise UnsupportedScreenContractError(
                "Expected appium.screen_capture schema version 1."
            )
        try:
            context_inventory(document)
        except ValueError as error:
            raise UnsupportedScreenContractError(str(error)) from error

    @staticmethod
    def _coverage(
        inventory: JsonObject,
        options: JsonObject,
        *,
        character_truncated: bool,
    ) -> tuple[JsonObject, list[str]]:
        limits = {
            "actions": options["max_actions"],
            "visible_text_occurrences": options["max_text_items"],
            "semantic_elements": options["max_elements"],
        }
        coverage: JsonObject = {}
        warnings: list[str] = []
        for name, available in inventory.items():
            limit = limits[name]
            included = available if limit == 0 else min(available, limit)
            complete = included == available and not character_truncated
            coverage[name] = {
                "included": included,
                "available": available,
                "complete": complete,
            }
            if included < available:
                warnings.append(
                    f"{name} limited to {included} of {available} items."
                )
        if character_truncated:
            warnings.append(
                "Context text was truncated by options.max_characters."
            )
        return coverage, warnings

    @staticmethod
    def _limit(value: int) -> int | None:
        return value or None

    @staticmethod
    def _identifier(value: Any) -> bool:
        return isinstance(value, str) and bool(IDENTIFIER_PATTERN.fullmatch(value))

