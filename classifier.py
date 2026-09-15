# -*- coding: utf-8 -*-
"""
Классификация типа кондиционера и расчёт падения КПД обогрева — без единого
вызова какого-либо платного API. Вся логика — правила по ключевым словам +
таблицы, оцифрованные вручную по графикам с klimresh.by (см. README.md).

Если по тексту клиента не удаётся уверенно определить тип (или, для типа
"зимний инвертор", мощность) — бот покажет кнопки на выбор, а не будет
гадать и не будет НИКУДА отправлять текст клиента.
"""

import re

CATEGORY_NAMES = {
    "type1": "неинверторный (On/Off)",
    "type2": "простой инвертор без зимнего комплекта",
    "type3": "инвертор с заводским зимним комплектом",
    "type4": "зимний инвертор",
    "type5": "суперинвертор (тепловой насос воздух-воздух)",
}

# --- Оцифрованные кривые: (уличная_температура, % от паспортной мощности) ---

CURVE_TYPE2 = [
    (-15, 50), (-10, 60), (-5, 70), (0, 80), (5, 93), (7, 100), (10, 108),
]

CURVE_TYPE5 = [
    (-30, 95), (-25, 110), (-20, 130), (-15, 150), (-10, 155), (-5, 160),
    (0, 160), (5, 170), (7, 175), (10, 210),
]

# Тип 4 — 4 разные кривые в зависимости от класса мощности.
CURVES_TYPE4 = {
    "09K": [
        (-25, 55), (-20, 70), (-15, 85), (-10, 100), (-7, 110), (-5, 118),
        (0, 125), (5, 130), (7, 130), (10, 125), (15, 115), (20, 100),
        (25, 85), (30, 75),
    ],
    "12K": [
        (-25, 52), (-20, 66), (-15, 80), (-10, 92), (-7, 100), (-5, 106),
        (0, 113), (5, 118), (7, 120), (10, 118), (15, 108), (20, 95),
        (25, 82), (30, 72),
    ],
    "18K": [
        (-25, 48), (-20, 60), (-15, 72), (-10, 82), (-7, 90), (-5, 95),
        (0, 102), (5, 108), (7, 112), (10, 115), (15, 108), (20, 98),
        (25, 88), (30, 78),
    ],
    "24K": [
        (-25, 45), (-20, 55), (-15, 65), (-10, 75), (-7, 82), (-5, 88),
        (0, 95), (5, 100), (7, 103), (10, 105), (15, 102), (20, 95),
        (25, 85), (30, 70),
    ],
}

# Тип 3 (инвертор с заводским зимним комплектом) = кривая типа 4 того же
# класса минус эта поправка (заводской комплект слабее полноценного
# инженерного решения зимнего инвертора).
TYPE3_PENALTY_PP = 12

CAPACITY_DISPLAY = {"09K": "9 тыс. BTU", "12K": "12 тыс. BTU", "18K": "18 тыс. BTU", "24K": "24 тыс. BTU"}


def interpolate(points: list[tuple[float, float]], x: float) -> tuple[float, bool]:
    """Линейная интерполяция по отсортированным точкам (temp, pct).

    Возвращает (значение, out_of_range) — out_of_range=True, если x лежит
    за пределами оцифрованного диапазона (тогда возвращается ближайшее
    крайнее значение, без экстраполяции "в никуда").
    """
    pts = sorted(points)
    xs = [p[0] for p in pts]

    if x <= xs[0]:
        return pts[0][1], x < xs[0]
    if x >= xs[-1]:
        return pts[-1][1], x > xs[-1]

    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            frac = (x - x0) / (x1 - x0)
            return y0 + frac * (y1 - y0), False

    return pts[-1][1], True  # сюда не должны попасть, но на всякий случай


# --- Ключевые слова для определения типа по свободному тексту клиента ---

_TYPE5_KEYWORDS = [
    "prestige", "ultra", "hyper", "flagship", "суперинвертор",
    "тепловой насос", "heat pump",
]
_TYPE4_KEYWORDS = [
    "nordic", "arctic", "lyra", "winter", "зимний инвертор", "cold climate",
    "h-inverter", "xtreme", "extreme", "морозоустойч", "экстрим",
]
_TYPE3_KEYWORDS = [
    "зимний комплект", "winter kit", "подогрев поддона", "подогрев картера",
]
_TYPE1_KEYWORDS = [
    "on/off", "он/офф", "неинвертор", "без инвертора", "on-off", "онoff",
]


def keyword_guess_type(text: str) -> str | None:
    """Пытается угадать тип кондиционера по ключевым словам в тексте клиента.

    Возвращает None, если ни один признак не найден — в этом случае бот
    должен спросить тип явно (кнопками), а не гадать.
    """
    t = text.lower()

    if any(k in t for k in _TYPE5_KEYWORDS):
        return "type5"
    if any(k in t for k in _TYPE4_KEYWORDS):
        return "type4"
    if any(k in t for k in _TYPE3_KEYWORDS):
        return "type3"
    if any(k in t for k in _TYPE1_KEYWORDS):
        return "type1"
    if "инвертор" in t or "invert" in t:
        return "type2"
    return None


_CAPACITY_PATTERNS = [
    (r"9\s*000\s*(btu|бту)\b|\b09?\s*(k|к)\b(?!\d)|9\s*тыс", "09K"),
    (r"12\s*000\s*(btu|бту)\b|\b12\s*(k|к)\b(?!\d)|12\s*тыс", "12K"),
    (r"18\s*000\s*(btu|бту)\b|\b18\s*(k|к)\b(?!\d)|18\s*тыс", "18K"),
    (r"24\s*000\s*(btu|бту)\b|\b24\s*(k|к)\b(?!\d)|24\s*тыс", "24K"),
]
_CAPACITY_KW_PATTERNS = [
    (r"2[.,][5-7]\s*(квт|kw)", "09K"),
    (r"3[.,][4-6]\s*(квт|kw)", "12K"),
    (r"5[.,][0-3]\s*(квт|kw)", "18K"),
    (r"[67][.,][0-1]\s*(квт|kw)", "24K"),
]


def guess_capacity_class(text: str) -> str | None:
    """Пытается угадать класс мощности (09K/12K/18K/24K) по тексту клиента.

    Осторожно: требует явного признака единицы измерения (BTU/K/тыс/кВт)
    рядом с числом — просто совпадение цифр внутри артикула модели (например
    "XYZ-9000") НЕ считается указанием мощности, чтобы не давать ложных
    срабатываний.
    """
    t = text.lower()
    for pattern, cls in _CAPACITY_PATTERNS + _CAPACITY_KW_PATTERNS:
        if re.search(pattern, t):
            return cls
    return None


def compute_percent(category: str, outdoor_temp: float, capacity_class: str | None = None):
    """Считает % от паспортной мощности для заданного типа/температуры.

    Возвращает (percent: float | None, out_of_range: bool). percent=None для
    типа 1 (там своя текстовая логика, не числовая кривая).
    """
    if category == "type2":
        return interpolate(CURVE_TYPE2, outdoor_temp)
    if category == "type5":
        return interpolate(CURVE_TYPE5, outdoor_temp)
    if category == "type4":
        cls = capacity_class or "12K"
        return interpolate(CURVES_TYPE4[cls], outdoor_temp)
    if category == "type3":
        cls = capacity_class or "12K"
        pct, out_of_range = interpolate(CURVES_TYPE4[cls], outdoor_temp)
        return max(pct - TYPE3_PENALTY_PP, 0), out_of_range
    if category == "type1":
        return None, False
    raise ValueError(f"Неизвестная категория: {category}")


def build_client_message(
    model_text: str,
    category: str,
    outdoor_temp: float,
    capacity_class: str | None = None,
    capacity_was_guessed: bool = False,
) -> str:
    """Собирает финальный текст ответа клиенту на русском."""
    name = CATEGORY_NAMES[category]

    if category == "type1":
        if outdoor_temp >= 3:
            body = (
                f"Сейчас на улице {outdoor_temp:.1f}°C — для типа «{name}» это ещё "
                f"приемлемо, обогрев должен работать, хотя эффективность уже заметно "
                f"ниже паспортной."
            )
        else:
            body = (
                f"Сейчас на улице {outdoor_temp:.1f}°C — при такой погоде "
                f"неинверторный кондиционер («{name}») скорее всего либо не запустится "
                f"на обогрев вообще, либо будет работать крайне неэффективно и "
                f"обмерзать. Как основной источник тепла в такую погоду его "
                f"использовать не стоит."
            )
        tail = (
            " Если дома при этом всё равно прохладно — возможно, дело не только в "
            "погоде, стоит проверить фильтр и общее состояние системы."
        )
        return body + tail

    pct, out_of_range = compute_percent(category, outdoor_temp, capacity_class)
    # Кривые графиков местами реально превышают 100% (тепловой насос при
    # определённой погоде физически может выдавать больше паспортной
    # мощности) — но клиенту показываем не больше 100%, чтобы не путать:
    # "эффективность 130%" звучит как ошибка, даже если технически так и есть.
    pct = min(pct, 100.0)
    pct_int = round(pct)

    cap_part = ""
    if capacity_class:
        cap_display = CAPACITY_DISPLAY.get(capacity_class, capacity_class)
        cap_part = f", мощность {cap_display}"
        if capacity_was_guessed:
            cap_part += " (определил по вашему тексту, поправьте, если не так)"

    range_note = ""
    if out_of_range:
        range_note = " (за пределами оцифрованного диапазона графика, оценка приблизительная)"

    body = (
        f"Модель: {model_text}\n"
        f"Тип: «{name}»{cap_part}.\n"
        f"Сейчас на улице {outdoor_temp:.1f}°C.\n\n"
        f"Ориентировочно кондиционер сейчас работает на ~{pct_int}% от "
        f"паспортной мощности обогрева{range_note}."
    )

    if pct_int < 70:
        tail = (
            "\n\nЕсли дома при этом всё равно прохладно — возможно, дело не только "
            "в погоде: стоит проверить фильтр, трассу и уровень фреона."
        )
    else:
        tail = "\n\nПока эффективность в порядке, но по мере похолодания она продолжит снижаться."

    return body + tail
