"""Token usage of agent runs, recorded on each grocery analysis run (and later costed, P1-9)."""

from dataclasses import dataclass

from pydantic_ai.usage import RunUsage


@dataclass
class ModelUsage:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @classmethod
    def of(cls, usage: RunUsage) -> ModelUsage:
        return cls(usage.requests, usage.input_tokens, usage.output_tokens)
