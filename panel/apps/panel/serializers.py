"""The one place snake_case meets camelCase.

Python is snake_case, JSON is camelCase, and the two spellings must never be
written out side by side by hand: a field renamed in one place and forgotten in
the other is a bug that shows up as `undefined` in the browser and nowhere else.
So every serializer in every app mixes in ``CamelCaseMixin`` and declares its
fields once, in Python spelling.

Only a serializer's own field names are renamed, and only at its own level. The
contents of a dict field are data, not field names - ``params`` is keyed by
"Jc", the live stats blob by public key - and walking into them would corrupt
both. Nested serializers rename themselves, because each of them mixes this in
too.

Validation errors are re-keyed the same way, so the UI can look a message up by
the field name it rendered.
"""

import re
from collections.abc import Mapping
from typing import Any

from rest_framework import serializers
from rest_framework.fields import empty
from rest_framework.settings import api_settings

__all__ = [
    "CamelCaseMixin",
    "CamelCaseModelSerializer",
    "CamelCaseSerializer",
    "camelize_keys",
    "to_camel_case",
    "to_snake_case",
]

_UNDERSCORES = re.compile(r"_+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])([A-Z])")


def to_camel_case(name: str) -> str:
    """public_key -> publicKey. A name with no underscore is returned unchanged."""
    parts = [part for part in _UNDERSCORES.split(name) if part]
    if not parts:
        return name
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])


def to_snake_case(name: str) -> str:
    """publicKey -> public_key. Already-snake names survive unchanged."""
    return _CAMEL_BOUNDARY.sub(r"_\1", name).lower()


def camelize_keys(data: Mapping[str, Any]) -> dict[str, Any]:
    """Rename one mapping's own keys. Values, including nested ones, are untouched."""
    return {
        (to_camel_case(key) if isinstance(key, str) else key): value for key, value in data.items()
    }


def rename_incoming(data: Any, aliases: Mapping[str, str]) -> Any:
    """Accept either spelling on input: camelCase keys are mapped to their field names.

    The snake_case spelling wins when a payload somehow carries both, because
    that is the name the serializer would have read anyway.
    """
    if not aliases or not isinstance(data, Mapping):
        return data

    # A QueryDict (form or multipart body) can hold several values per key, and
    # dict() would silently keep only the last one.
    if hasattr(data, "getlist") and hasattr(data, "setlist"):
        renamed = data.copy()
        for camel, snake in aliases.items():
            if camel in renamed and snake not in renamed:
                renamed.setlist(snake, renamed.getlist(camel))
                del renamed[camel]
        return renamed

    renamed = dict(data)
    for camel, snake in aliases.items():
        if camel in renamed and snake not in renamed:
            renamed[snake] = renamed.pop(camel)
    return renamed


class CamelCaseMixin:
    """Mix in before a DRF serializer class to speak camelCase over the wire."""

    def to_representation(self, instance: Any) -> Any:
        data = super().to_representation(instance)
        if isinstance(data, Mapping):
            return camelize_keys(data)
        return data

    def to_internal_value(self, data: Any) -> Any:
        return super().to_internal_value(rename_incoming(data, self._camel_aliases()))

    def run_validation(self, data: Any = empty) -> Any:
        try:
            return super().run_validation(data)
        except serializers.ValidationError as exc:
            raise serializers.ValidationError(_camelize_detail(exc.detail)) from exc

    def _camel_aliases(self) -> dict[str, str]:
        """camelCase spelling -> field name, for the fields whose spellings differ."""
        aliases: dict[str, str] = {}
        for name in self.fields:
            camel = to_camel_case(name)
            if camel != name:
                aliases[camel] = name
        return aliases


class CamelCaseSerializer(CamelCaseMixin, serializers.Serializer):
    """serializers.Serializer that reads and writes camelCase."""


class CamelCaseModelSerializer(CamelCaseMixin, serializers.ModelSerializer):
    """serializers.ModelSerializer that reads and writes camelCase."""


def _camelize_detail(detail: Any) -> Any:
    """Re-key a validation error so each message lands on the field the UI drew.

    The non-field key keeps its DRF spelling: it is not a field, and the
    exception handler turns it into part of the sentence rather than a target.
    """
    if not isinstance(detail, Mapping):
        return detail
    non_field = api_settings.NON_FIELD_ERRORS_KEY
    return {
        (to_camel_case(key) if isinstance(key, str) and key != non_field else key): value
        for key, value in detail.items()
    }
