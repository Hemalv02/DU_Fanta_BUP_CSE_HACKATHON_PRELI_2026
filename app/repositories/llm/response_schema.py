"""Strict response schema for constrained generation (model-side guardrail).

Passed as `response_format` so the provider's structured-output mode makes
the model emit ONLY this shape — an out-of-schema response is refused at
generation time, before our deterministic guardrails even run.
"""

_HOURS = {
    "type": "array",
    "items": {"type": "integer", "minimum": 0, "maximum": 23},
    "minItems": 1,
    "maxItems": 24,
}

_INTERPRETATION_ITEM = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "note_index",
        "applies",
        "directive_type",
        "structured_adjustment",
        "explanation",
    ],
    "properties": {
        "note_index": {"type": "integer", "minimum": 0, "maximum": 2},
        "applies": {"type": "boolean"},
        "directive_type": {
            "type": "string",
            "enum": [
                "solar_reduction",
                "minimum_battery_reserve",
                "no_charge_window",
                "no_discharge_window",
                "max_grid_window",
                "no_op",
            ],
        },
        "structured_adjustment": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["hours", "factor"],
                    "properties": {
                        "hours": _HOURS,
                        "factor": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["hours", "minimum_energy_kwh"],
                    "properties": {
                        "hours": _HOURS,
                        "minimum_energy_kwh": {"type": "number", "minimum": 0},
                    },
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["hours"],
                    "properties": {"hours": _HOURS},
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["hours", "max_grid_kwh"],
                    "properties": {
                        "hours": _HOURS,
                        "max_grid_kwh": {"type": "number", "minimum": 0},
                    },
                },
            ]
        },
        "explanation": {"type": "string", "maxLength": 300},
    },
}

#: Chat-completions `response_format` payload for strict structured output.
GRIDWISE_RESPONSE_FORMAT: dict[str, object] = {
    "type": "json_schema",
    "json_schema": {
        "name": "gridwise_interpretation",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["interpretations"],
            "properties": {
                "interpretations": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 3,
                    "items": _INTERPRETATION_ITEM,
                }
            },
        },
    },
}
