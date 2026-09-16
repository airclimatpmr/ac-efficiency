# -*- coding: utf-8 -*-
"""
Подбор рекомендуемой мощности кондиционера по площади помещения.

Базовое правило (общепринятое, используется всеми продавцами климатической
техники как отправная точка, не инженерный точный расчёт): ~100 Вт
холодопроизводительности на 1 м² при стандартном потолке (2.5-2.7 м) и
типовой инсоляции. Дальше поправки (каждая — независимый множитель +5%):

- Последний этаж / мансарда — дополнительный нагрев через крышу летом.
- Окна на солнечную сторону (юг/запад) — дополнительная тепловая нагрузка.
- Высокие потолки (выше 2.7 м) — больше объём воздуха для охлаждения.
- Тепловыделяющая техника в помещении (компьютеры, холодильники, серверы,
  кухонное/торговое оборудование) — дополнительная тепловая нагрузка.

Это ОРИЕНТИРОВОЧНЫЙ расчёт для быстрой подсказки клиенту и квалификации
лида — не заменяет очный замер и консультацию монтажника перед покупкой.
Итоговое решение всегда обязательно уточняется у клиента менеджером.
"""

CAPACITY_CLASSES_W = [
    ("09K", 2600),
    ("12K", 3500),
    ("18K", 5300),
    ("24K", 7000),
]

# Стороны света, где решает, попадает ли прямое солнце на окна (юг/запад —
# самая сильная тепловая нагрузка во второй половине дня).
SUNNY_DIRECTIONS = {"south", "west"}
DIRECTION_DISPLAY = {
    "north": "Север", "south": "Юг", "east": "Восток", "west": "Запад", "none": "Окон нет",
}


def recommend_capacity(
    area_m2: float,
    top_floor: bool,
    sunny: bool,
    high_ceiling: bool = False,
    heat_equipment: bool = False,
) -> dict:
    """Считает рекомендуемую мощность и подбирает ближайший класс.

    Возвращает dict: {
        "required_watts": float,          # расчётная потребность, Вт
        "capacity_class": str | None,     # ближайший подходящий класс сверху, либо None если выше 24K
        "capacity_watts": int | None,
        "needs_custom_solution": bool,    # True, если требуется больше 24K / индивидуальный расчёт
    }
    """
    watts = area_m2 * 100
    if top_floor:
        watts *= 1.05
    if sunny:
        watts *= 1.05
    if high_ceiling:
        watts *= 1.05
    if heat_equipment:
        watts *= 1.05

    for cls, cls_watts in CAPACITY_CLASSES_W:
        if cls_watts >= watts:
            return {
                "required_watts": watts,
                "capacity_class": cls,
                "capacity_watts": cls_watts,
                "needs_custom_solution": False,
            }

    return {
        "required_watts": watts,
        "capacity_class": None,
        "capacity_watts": None,
        "needs_custom_solution": True,
    }


CAPACITY_DISPLAY = {"09K": "9 тыс. BTU", "12K": "12 тыс. BTU", "18K": "18 тыс. BTU", "24K": "24 тыс. BTU"}


def build_recommendation_message(
    area_m2: float,
    top_floor: bool,
    sunny: bool,
    high_ceiling: bool = False,
    heat_equipment: bool = False,
) -> tuple[str, dict]:
    result = recommend_capacity(area_m2, top_floor, sunny, high_ceiling, heat_equipment)

    if result["needs_custom_solution"]:
        text = (
            f"Для помещения {area_m2:.0f} м² с учётом условий одна бытовая сплит-система "
            f"уже, скорее всего, не справится оптимально — нужен индивидуальный расчёт "
            f"(например, две зоны или полупромышленная система). Оставьте контакт, и мы "
            f"посчитаем точно."
        )
    else:
        cls_display = CAPACITY_DISPLAY[result["capacity_class"]]
        text = (
            f"Для помещения {area_m2:.0f} м² ориентировочно подойдёт кондиционер класса "
            f"~{cls_display}. Это прикидка «на глаз» — точный подбор модели и цену "
            f"уточнит менеджер, если оставите контакт."
        )

    return text, result
