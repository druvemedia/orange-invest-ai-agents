"""Model prices for cost estimates. A model without an entry has no estimate: cost is reported as unknown.

Prices are USD per million tokens at the standard rate (no cached-input or batch discounts), so estimates err high.
Check the provider's page before relying on them; prices change.
"""

from app.schemas.common import StrictModel


class ModelPrice(StrictModel):
    input_per_million: float
    output_per_million: float
    source: str
    checked: str  # date the price was read from the source


PRICES: dict[str, ModelPrice] = {
    "gpt-4o-mini": ModelPrice(input_per_million=0.15, output_per_million=0.60,
                              source="https://developers.openai.com/api/docs/models/gpt-4o-mini", checked="2026-10-27"),
}


def estimate_cost(model: str | None, input_tokens: int | None, output_tokens: int | None) -> float | None:
    """Estimated USD cost of measured usage, or None if the model has no price or usage was not reported."""
    price = PRICES.get((model or "").strip())
    if price is None or input_tokens is None:
        return None
    return round(input_tokens * price.input_per_million / 1e6 + (output_tokens or 0) * price.output_per_million / 1e6, 6)
