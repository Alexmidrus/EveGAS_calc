"""Форматирование чисел для шаблонов.

Правило проекта: любое число, попадающее в шаблон, проходит через эти
функции. Никаких «2751.0000000004» на экране.
"""

from collections.abc import Sequence


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
    """Компактная запись крупных сумм: «137.6M», «2.5B». Мелкие — как обычно.

    От тысячи и выше дробная часть не показывается. Компактная запись по своей
    сути приблизительная, и «9 123.86 юнитов в сутки» — это не точность,
    а сотые доли юнита в среднем за неделю. Точные значения идут через
    ``fmt_number`` и на экране остаются: компактной записью показываются
    оборот, глубина стакана и итоги — величины, которые читают порядком.
    """
    magnitude = abs(value)
    if magnitude >= 1e9:
        return f"{value / 1e9:.1f}B"
    if magnitude >= 1e6:
        return f"{value / 1e6:.1f}M"
    if magnitude >= 1e3:
        return fmt_number(value, 0)
    return fmt_number(value)


def fmt_percent(value: float | int, decimals: int = 0) -> str:
    """Процент с явной точностью: «89%», «9.0%»."""
    return fmt_number(value, decimals) + "%"


def share_percents(parts: Sequence[float], total: float) -> list[float]:
    """Доли частей в процентах, сумма которых равна ровно 100.

    Полоска «из чего сложилось» рисуется этими числами, и три независимо
    округлённых доли дали бы 99.9 % или 100.1 % — щель или срез на конце
    полоски. Поэтому последняя доля не считается, а добирается остатком.
    Нулевой знаменатель — нули: делить не на что, а врать нельзя.
    """
    if total <= 0 or not parts:
        return [0.0] * len(parts)
    shares = [round(part / total * 100, 1) for part in parts[:-1]]
    return shares + [round(100.0 - sum(shares), 1)]


def bar_width(value: float, floor: float = 0.0, ceiling: float = 100.0) -> str:
    """Ширина полоски в процентах строкой для CSS-свойства.

    Обрезается по краям: отрицательная ширина невалидна, а полоска шире
    контейнера вылезает за рамку. floor держит видимый огрызок там, где
    величина мала, но строка на экране есть.
    """
    return f"{min(max(value, floor), ceiling):.0f}%"


def fmt_share(percent: float) -> str:
    """Доля в подписи к полоске: целые проценты, но не «0 %» вместо ненулевой доли.

    Обеспечение в полпроцента от стоимости газа — это миллионы ISK, и округлять
    их до нуля значит стереть с экрана строку расходов. Полоску такой остаток
    рисует всё равно, а подпись обязана с ней сходиться.
    """
    if 0 < percent < 1:
        return "<1"
    return fmt_number(percent, 0)


# Размер спарклайна «7 дней» в строке результата — из макета (SPEC §5.2).
# Внутри viewBox, а не в пикселях: SVG растягивается по ячейке.
SPARK_WIDTH = 58.0
SPARK_HEIGHT = 16.0
# Отступ сверху и снизу, чтобы линия толщиной 1.2 не срезалась краем viewBox.
SPARK_PADDING = 1.0


def sparkline_points(
    values: Sequence[float],
    width: float = SPARK_WIDTH,
    height: float = SPARK_HEIGHT,
    padding: float = SPARK_PADDING,
) -> str:
    """Точки для <polyline> спарклайна: «0.0,15.0 9.7,4.2 …».

    Линия растягивается на всю ширину и нормируется по своему же размаху:
    спарклайн показывает форму движения за неделю, а не абсолютный уровень —
    уровень стоит числом в соседней колонке «Сделки».

    Меньше двух точек — пустая строка: одна точка это не линия, и рисовать
    по ней горизонталь значит показать «неделю без изменений» там, где у нас
    просто один день данных.

    Ровная серия кладётся по середине, а не по нижнему краю: линия у самого
    низа читается как «упало в пол», хотя цена не двигалась вовсе.
    """
    if len(values) < 2:
        return ""

    low, high = min(values), max(values)
    span = high - low
    step = width / (len(values) - 1)
    # Сверху и снизу остаётся padding, остальное — под размах
    usable = height - padding * 2

    points = []
    for index, value in enumerate(values):
        share = (value - low) / span if span > 0 else 0.5
        x = index * step
        # Ось Y в SVG растёт вниз: дорогой день должен оказаться выше
        y = height - padding - share * usable
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def sparkline_change_pct(values: Sequence[float]) -> float | None:
    """На сколько процентов последний день окна отличается от первого.

    None — считать не от чего: нет двух точек или первый день нулевой.
    """
    if len(values) < 2 or values[0] <= 0:
        return None
    return (values[-1] / values[0] - 1) * 100
