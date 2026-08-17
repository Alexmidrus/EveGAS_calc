"""Форматирование чисел для шаблонов.

Правило проекта: любое число, попадающее в шаблон, проходит через эти
функции. Никаких «2751.0000000004» на экране.
"""


def fmt_number(value: float | int, decimals: int | None = None) -> str:
    """«1 234 567» или «2 751.19»: разряды через пробел.

    decimals=None — автоматический режим: целые значения без дробной части,
    дробные — до двух знаков без хвостовых нулей (0.5, а не 0.50).
    """
    if decimals is not None:
        text = f"{value:,.{decimals}f}"
    else:
        rounded = round(float(value), 2)
        if rounded == int(rounded):
            text = f"{int(rounded):,d}"
        else:
            text = f"{rounded:,.2f}".rstrip("0")
    return text.replace(",", " ")


def fmt_compact(value: float | int) -> str:
    """Компактная запись крупных сумм: «137.6M», «2.5B». Мелкие — как обычно."""
    magnitude = abs(value)
    if magnitude >= 1e9:
        return f"{value / 1e9:.1f}B"
    if magnitude >= 1e6:
        return f"{value / 1e6:.1f}M"
    return fmt_number(value)


def fmt_percent(value: float | int, decimals: int = 0) -> str:
    """Процент с явной точностью: «89%», «9.0%»."""
    return fmt_number(value, decimals) + "%"
