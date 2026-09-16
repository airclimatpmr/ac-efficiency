# -*- coding: utf-8 -*-
"""
Распознавание фото акта выполненных работ через Claude (Anthropic API).

Это ЕДИНСТВЕННОЕ место во всём боте, где используется платный ИИ — все
остальные функции (классификация типа кондиционера, расчёт мощности, расчёт
акта, склад) работают без единого ИИ-вызова. Осознанное решение: OCR/чтение
рукописного текста с фото требует зрения модели, бесплатной альтернативы с
сопоставимым качеством нет.

Настройка:
    export ANTHROPIC_API_KEY="sk-ant-..."
    (необязательно) export ACT_OCR_MODEL="claude-sonnet-5"
"""

import os
import base64
import json
import logging

from anthropic import Anthropic

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
OCR_MODEL = os.environ.get("ACT_OCR_MODEL", "claude-sonnet-5")

_client: Anthropic | None = None


def is_configured() -> bool:
    return bool(ANTHROPIC_API_KEY)


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError(
                "ANTHROPIC_API_KEY не задан — распознавание фото актов недоступно."
            )
        _client = Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


OCR_PROMPT = """
Это фото бумажного акта выполненных монтажных работ по установке
кондиционера (компания AirClimat.pmr, Приднестровье). На акте могут быть
как печатные, так и рукописные заполненные поля (в том числе подпись
клиента).

Внимательно рассмотри документ и извлеки следующие данные:
- client_name: ФИО заказчика/клиента
- phone: номер телефона клиента (в любом формате, как написано)
- ac_model: модель установленного кондиционера (марка + модель, как указано)
- act_date: дата акта — верни строго в формате ГГГГ-ММ-ДД. Если год на
  документе не указан явно, но написан день/месяц — предположи ближайший
  подходящий год из текущей даты (сегодня — {today}) и отметь это в notes.
- address: адрес объекта, где производился монтаж

Если какое-то поле неразборчиво или отсутствует на фото — верни для него
null. НЕ ПРИДУМЫВАЙ данные, которых не видно на фото.

Ответь СТРОГО в виде JSON без каких-либо пояснений до или после, вот такой
схемы:

{{
  "client_name": "...", 
  "phone": "...",
  "ac_model": "...",
  "act_date": "ГГГГ-ММ-ДД",
  "address": "...",
  "confidence": "high" | "medium" | "low",
  "unclear_fields": ["список полей, в распознавании которых не уверен"],
  "notes": "короткая заметка, если что-то важное для человека, который потом проверит запись"
}}
""".strip()


def extract_act_data(image_bytes: bytes, media_type: str = "image/jpeg") -> dict:
    """Отправляет фото в Claude и возвращает разобранный JSON с данными акта.

    В случае ошибки парсинга или сетевой ошибки возвращает
    {"error": "..."} — вызывающий код должен эту ситуацию обработать (не
    падать, а сообщить пользователю, что распознать не получилось).
    """
    from datetime import date as _date

    client = _get_client()
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    prompt = OCR_PROMPT.format(today=_date.today().isoformat())

    try:
        message = client.messages.create(
            model=OCR_MODEL,
            max_tokens=600,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
    except Exception as e:
        logger.exception("Ошибка запроса к Claude при распознавании акта")
        return {"error": f"api_error: {e}"}

    raw_text = message.content[0].text.strip()
    raw_text = raw_text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        logger.error("Не удалось распарсить ответ распознавания акта: %s", raw_text)
        return {"error": "parse_failed", "raw": raw_text}

    return parsed
