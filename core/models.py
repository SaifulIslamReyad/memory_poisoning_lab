"""Model capability normalization and request validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


class RequestValidationError(ValueError):
    """Raised when a request is unsupported or outside model limits."""


@dataclass(frozen=True)
class ModelCapabilities:
    """Immutable representation of a model's capabilities."""
    model_id: str                          # Unique model identifier
    active: bool = True                    # Whether the model is active/available
    context_window: int | None = None     # Total context length in tokens
    max_completion_tokens: int | None = None  # Maximum output tokens the model supports
    input_modalities: tuple[str, ...] = ()    # Supported input modalities (e.g., "text", "image")
    output_modalities: tuple[str, ...] = ()   # Supported output modalities
    features: frozenset[str] = field(default_factory=frozenset)  # Supported features (e.g., "tools", "json_mode")
    sampling_parameters: frozenset[str] = field(default_factory=frozenset)  # Advertised sampling parameters

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ModelCapabilities":
        """Construct ModelCapabilities from a model record dictionary."""
        # Parse the "active" field, handling both boolean and string representations
        active_value = value.get("active", True)
        if isinstance(active_value, str):
            active = active_value.strip().lower() in {"1", "true", "yes", "on"}
        else:
            active = bool(active_value)
        return cls(
            model_id=str(value.get("id") or value.get("model_id") or ""),
            active=active,
            # Accept either "context_window" or "context_length" as the key
            context_window=_optional_int(value.get("context_window") or value.get("context_length")),
            # Accept either "max_completion_tokens" or "max_output_length"
            max_completion_tokens=_optional_int(
                value.get("max_completion_tokens") or value.get("max_output_length")
            ),
            input_modalities=tuple(_string_list(value.get("input_modalities"))),
            output_modalities=tuple(_string_list(value.get("output_modalities"))),
            # Accept either "features" or "supported_features" as the key
            features=frozenset(_string_list(value.get("features") or value.get("supported_features"))),
            # Accept either "sampling_parameters" or "supported_sampling_parameters"
            sampling_parameters=frozenset(
                _string_list(
                    value.get("sampling_parameters")
                    or value.get("supported_sampling_parameters")
                )
            ),
        )

    @property
    def is_text_chat(self) -> bool:
        """Return True if the model accepts and produces text modality."""
        return "text" in self.input_modalities and "text" in self.output_modalities

    def supports(self, feature: str) -> bool:
        """Check whether the model supports a given feature (case-insensitive)."""
        return feature.lower() in {item.lower() for item in self.features}


def _optional_int(value: Any) -> int | None:
    """Convert a value to int, returning None for empty/None values or non-convertible values."""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _string_list(value: Any) -> list[str]:
    """Normalize various input types into a list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item) for item in value]
    return [str(value)]


def capabilities_from_model(value: Mapping[str, Any] | ModelCapabilities) -> ModelCapabilities:
    """Convert a dict or ModelCapabilities to a ModelCapabilities instance."""
    if isinstance(value, ModelCapabilities):
        return value
    return ModelCapabilities.from_dict(value)



def estimate_token_count(value: Any) -> int:
    """Conservatively estimate the token count of a message for context-window validation."""
    # For strings: approximate 4 characters per token, plus 1 per newline, minimum 1
    if isinstance(value, str):
        return max(1, len(value) // 4 + value.count("\n") + 1)
    # For mappings (dicts): sum token counts of all keys and values
    if isinstance(value, Mapping):
        return sum(estimate_token_count(key) + estimate_token_count(item) for key, item in value.items())
    # For sequences (lists/tuples): sum token counts of all elements
    if isinstance(value, (list, tuple)):
        return sum(estimate_token_count(item) for item in value)
    # For other types: count whitespace-separated tokens
    return len(str(value).split())


def validate_request(
    model: Mapping[str, Any] | ModelCapabilities,
    parameters: Mapping[str, Any] | None = None,
    *,
    prompt_token_count: int | None = None,
) -> dict[str, Any]:
    """Validate and return a provider-safe request parameter dictionary.

    Only advertised sampling parameters and capabilities are forwarded. The
    function rejects unsupported modes instead of silently changing behavior.
    """
    capabilities = capabilities_from_model(model)
    # Ensure the model has a valid ID
    if not capabilities.model_id:
        raise RequestValidationError("model id is required")
    # Ensure the model is active
    if not capabilities.active:
        raise RequestValidationError(f"model {capabilities.model_id} is inactive")
    params = dict(parameters or {})
    # Check for any parameters not in the known set of valid request parameters
    unsupported = sorted(set(params) - {"temperature", "top_p", "stop", "seed", "max_tokens", "tools", "tool_choice", "response_format", "json_mode", "reasoning_effort", "reasoning"})
    if unsupported:
        raise RequestValidationError(f"unsupported request parameters: {', '.join(unsupported)}")

    validated: dict[str, Any] = {}
    # Validate temperature (0.0 to 2.0)
    if "temperature" in params:
        _require_parameter("temperature", capabilities, "temperature")
        temperature = _finite_float(params["temperature"], "temperature")
        if not 0.0 <= temperature <= 2.0:
            raise RequestValidationError("temperature must be between 0 and 2")
        validated["temperature"] = temperature
    # Validate top_p (0.0 to 1.0)
    if "top_p" in params:
        _require_parameter("top_p", capabilities, "top_p")
        top_p = _finite_float(params["top_p"], "top_p")
        if not 0.0 <= top_p <= 1.0:
            raise RequestValidationError("top_p must be between 0 and 1")
        validated["top_p"] = top_p
    # Validate stop sequences (string or list of up to 4 strings)
    if "stop" in params:
        _require_parameter("stop", capabilities, "stop")
        stop = params["stop"]
        if isinstance(stop, str):
            stop = [stop]
        if not isinstance(stop, list) or len(stop) > 4 or any(not isinstance(item, str) for item in stop):
            raise RequestValidationError("stop must be a string or a list of at most four strings")
        validated["stop"] = stop
    # Validate seed (must be an integer, not a boolean)
    if "seed" in params:
        _require_parameter("seed", capabilities, "seed")
        seed = params["seed"]
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise RequestValidationError("seed must be an integer")
        validated["seed"] = seed
    # Validate max_tokens (positive integer, must not exceed model limit)
    if "max_tokens" in params:
        _require_parameter("max_tokens", capabilities, "max_tokens")
        max_tokens = params["max_tokens"]
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
            raise RequestValidationError("max_tokens must be a positive integer")
        if capabilities.max_completion_tokens and max_tokens > capabilities.max_completion_tokens:
            raise RequestValidationError(
                f"max_tokens exceeds model limit of {capabilities.max_completion_tokens}"
            )
        validated["max_tokens"] = max_tokens
    # Validate tools (requires "tools" feature)
    if "tools" in params:
        if not capabilities.supports("tools"):
            raise RequestValidationError("model does not support tools")
        tools = params["tools"]
        if not isinstance(tools, list) or not tools:
            raise RequestValidationError("tools must be a non-empty list when enabled")
        _validate_tools(tools)
        validated["tools"] = tools
    # Validate tool_choice (requires "tools" feature)
    if "tool_choice" in params:
        if not capabilities.supports("tools"):
            raise RequestValidationError("model does not support tool_choice")
        tool_choice = params["tool_choice"]
        if isinstance(tool_choice, str):
            if tool_choice not in {"auto", "none", "required"}:
                raise RequestValidationError("tool_choice string must be auto, none, or required")
        elif not isinstance(tool_choice, dict) or not tool_choice.get("type"):
            raise RequestValidationError("tool_choice must be a string or object")
        validated["tool_choice"] = tool_choice
    # Validate json_mode (mutually exclusive with response_format)
    if "json_mode" in params:
        if not capabilities.supports("json_mode"):
            raise RequestValidationError("model does not support json_mode")
        if params["json_mode"] not in (True, False):
            raise RequestValidationError("json_mode must be boolean")
        if params["json_mode"]:
            if params.get("response_format") is not None:
                raise RequestValidationError("json_mode and response_format cannot both be active")
            # json_mode maps to response_format={"type": "json_object"}
            validated["response_format"] = {"type": "json_object"}
    # Validate response_format (structured outputs / json_schema)
    if "response_format" in params and "json_mode" not in params:
        if not capabilities.supports("structured_outputs") and not capabilities.supports("json_mode"):
            raise RequestValidationError("model does not support structured outputs")
        response_format = params["response_format"]
        if not isinstance(response_format, dict) or not response_format.get("type"):
            raise RequestValidationError("response_format must be an object with a type")
        _validate_response_format(response_format)
        validated["response_format"] = response_format
    # Validate reasoning controls (low/medium/high effort)
    if "reasoning_effort" in params or "reasoning" in params:
        if not capabilities.supports("reasoning"):
            raise RequestValidationError("model does not support reasoning controls")
        if "reasoning_effort" in params:
            effort = str(params["reasoning_effort"]).lower()
            if effort not in {"low", "medium", "high"}:
                raise RequestValidationError("reasoning_effort must be low, medium, or high")
            validated["reasoning_effort"] = effort
        if "reasoning" in params and params["reasoning"] not in (True, False):
            raise RequestValidationError("reasoning must be boolean")
    # Validate total token count doesn't exceed context window
    if prompt_token_count is not None and capabilities.context_window:
        requested_tokens = int(prompt_token_count) + int(validated.get("max_tokens", 0))
        if requested_tokens > capabilities.context_window:
            raise RequestValidationError(
                f"prompt and max_tokens exceed context window of {capabilities.context_window}"
            )
    return validated


def _validate_tools(tools: Any) -> None:
    """Validate the structure of tool definitions passed in a request."""
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict):
            raise RequestValidationError(f"tool[{index}] must be an object")
        function = tool.get("function")
        if not isinstance(function, dict):
            raise RequestValidationError(f"tool[{index}] must contain a function object")
        if not isinstance(function.get("name"), str) or not function["name"].strip():
            raise RequestValidationError(f"tool[{index}].function.name must be a non-empty string")
        schema = function.get("parameters")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise RequestValidationError(f"tool[{index}].function.parameters must be an object schema")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise RequestValidationError(f"tool[{index}].function.parameters.properties must be an object")


def _validate_response_format(response_format: Any) -> None:
    """Validate the structure of a response_format parameter."""
    response_type = response_format.get("type")
    # Only allow known response format types
    if response_type not in {"json_object", "json_schema", "text"}:
        raise RequestValidationError("response_format.type must be json_object, json_schema, or text")
    # json_schema type requires a json_schema wrapper with schema and name
    if response_type != "json_schema":
        return
    wrapper = response_format.get("json_schema")
    if not isinstance(wrapper, dict):
        raise RequestValidationError("json_schema response_format requires json_schema")
    if not isinstance(wrapper.get("schema"), dict):
        raise RequestValidationError("json_schema.schema must be an object")
    if not isinstance(wrapper.get("name"), str) or not wrapper["name"].strip():
        raise RequestValidationError("json_schema.name must be a non-empty string")
    if "strict" in wrapper and not isinstance(wrapper["strict"], bool):
        raise RequestValidationError("json_schema.strict must be boolean")
    # Recursively validate the schema structure (with cycle detection)
    _validate_json_schema(wrapper["schema"], set())


def _validate_json_schema(schema: Any, path: set[str]) -> None:
    """Recursively validate a JSON schema, with cycle detection by object identity."""
    if not isinstance(schema, dict):
        raise RequestValidationError("JSON schema must be an object")
    schema_id = id(schema)
    # Prevent infinite recursion on circular references
    if schema_id in path:
        return
    path.add(schema_id)
    # Validate the "type" field if present
    schema_type = schema.get("type")
    if schema_type is not None and schema_type not in {
        "object", "array", "string", "number", "integer", "boolean", "null"
    }:
        raise RequestValidationError("JSON schema contains an unsupported type")
    # Recursively validate "properties" entries
    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            raise RequestValidationError("JSON schema properties must be an object")
        for child in properties.values():
            _validate_json_schema(child, path)
    # Recursively validate "items" for arrays
    items = schema.get("items")
    if items is not None:
        _validate_json_schema(items, path)
    path.remove(schema_id)


def _require_parameter(parameter: str, capabilities: ModelCapabilities, advertised_name: str) -> None:
    """Raise an error if a parameter is not in the model's advertised sampling_parameters."""
    if advertised_name not in capabilities.sampling_parameters:
        raise RequestValidationError(
            f"parameter {parameter} is not advertised by model {capabilities.model_id}"
        )


def _finite_float(value: Any, name: str) -> float:
    """Convert a value to a finite float, rejecting booleans, NaN, and infinity."""
    if isinstance(value, bool):
        raise RequestValidationError(f"{name} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RequestValidationError(f"{name} must be a number") from exc
    # Reject NaN, +inf, -inf
    if result != result or result in (float("inf"), float("-inf")):
        raise RequestValidationError(f"{name} must be finite")
    return result


def build_request(
    model: Mapping[str, Any] | ModelCapabilities,
    messages: list[dict[str, Any]],
    parameters: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build and validate a complete provider request dict."""
    capabilities = capabilities_from_model(model)
    validated = validate_request(capabilities, parameters)
    return {"model": capabilities.model_id, "messages": messages, **validated}


def model_supports(
    model: Mapping[str, Any] | ModelCapabilities,
    feature: str,
) -> bool:
    """Check whether a model supports a given feature."""
    return capabilities_from_model(model).supports(feature)


__all__ = [
    "ModelCapabilities",
    "RequestValidationError",
    "build_request",
    "capabilities_from_model",
    "estimate_token_count",
    "model_supports",
    "validate_request",
]
