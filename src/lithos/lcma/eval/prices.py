"""Price table (``evals/prices.toml``) → cost per 1,000 calls.

Prices are USD per million tokens as the provider lists them; the table
records where and when each row was read so a cost claim can be re-checked.
A model missing from the table yields ``None`` rather than a guess.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModelPrice:
    input_per_m: float
    output_per_m: float
    cached_input_per_m: float | None = None
    source: str = ""


def load_prices(path: Path) -> dict[str, ModelPrice]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    models = data.get("models", {})
    if not isinstance(models, dict):
        raise ValueError(f"{path}: [models] must be a table")
    prices: dict[str, ModelPrice] = {}
    for model, row in models.items():
        if not isinstance(row, dict):
            raise ValueError(f"{path}: models.{model} must be a table")
        try:
            prices[str(model)] = ModelPrice(
                input_per_m=float(row["input_per_m"]),
                output_per_m=float(row["output_per_m"]),
                cached_input_per_m=(
                    float(row["cached_input_per_m"]) if "cached_input_per_m" in row else None
                ),
                source=str(row.get("source", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path}: models.{model}: {exc}") from exc
    return prices


def cost_per_1k_calls(
    price: ModelPrice | None, *, prompt_tokens_mean: float, completion_tokens_mean: float
) -> float | None:
    """USD for 1,000 calls at the run's mean token split; ``None`` without a price."""
    if price is None:
        return None
    per_call = (
        prompt_tokens_mean * price.input_per_m + completion_tokens_mean * price.output_per_m
    ) / 1_000_000
    return per_call * 1000
