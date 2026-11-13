# -*- coding: utf-8 -*-
"""
gemini_takeout_html.py - адаптер: Gemini через Google Takeout (файл MyActivity.html).

ЭТО ЕДИНСТВЕННЫЙ ФАЙЛ, КОТОРЫЙ ЗНАЕТ УСТРОЙСТВО TAKEOUT-HTML: названия классов
(outer-cell, content-cell), формат времени Google, блок "Служебный файл проекта".
Если Google изменит разметку, править нужно только его.

Что он делает: читает файл и отдаёт карточки по договору schema.py.
Чего он НЕ делает (это забота "пульта", stage1_ingest.py):
  * не маскирует секреты (redaction.py),
  * не сортирует карточки и не убирает дубликаты,
  * не пишет файлы и отчёты.

Структура карточки в Takeout (упрощённо):
    <div class="outer-cell ...">
      <div class="mdl-grid">
        <div class="header-cell ...">Gemini Apps</div>
        <div class="content-cell ...">Prompted&nbsp;ТЕКСТ ЗАПРОСА<br>
             Jul 18, 2026, 10:46:52 AM EEST<br>
             ...HTML ответа...</div>
        <div class="content-cell ... text-right"></div>
        <div class="content-cell ... caption">Products: ... Why is this here?</div>
      </div>
    </div>
"""

import re                      # регулярные выражения
from datetime import datetime, timedelta, timezone  # дата, разница времён, часовые пояса
from pathlib import Path       # удобная работа с путями

from ..schema import SCHEMA_VERSION                # версия договора
from ..text_tools import html_to_md, placeholder_for, plain_text, sha  # общие текстовые инструменты
# ".." значит "на уровень выше": из пакета adapters мы обращаемся к модулям пакета chatpipe.


# ==============================================================================
# ПАСПОРТ АДАПТЕРА (то, что требует реестр adapters/__init__.py)
# ==============================================================================

PROFILE_ID = "gemini-takeout-html"       # идентификатор профиля = модель + способ экспорта
MODEL = "gemini"
EXPORT_METHOD = "takeout-html"
DESCRIPTION = "Gemini: Google Takeout, файл MyActivity.html (Моя активность -> Gemini Apps)"

# Диагностика, которую адаптер собирает в словарь-счётчик stats. Здесь - подписи,
# под которыми эти счётчики попадут в отчёт. Отчёт (stage1_report.py) не знает, что
# означают ключи, он просто печатает подпись и число. Так адаптер сам решает, о чём
# сообщить пользователю, а отчёт остаётся общим для всех источников.
STAT_LABELS = {
    "failed_cards": "карточек не удалось разобрать",
    "no_ts": "карточек без распознанного времени",
    "tz_unknown": "время без известного пояса (принято за UTC)",
    "fallback_cell": "ячейка найдена запасным способом",
    "service_mentioned_not_parsed": "упоминаний «служебный файл» в тексте без блока (шум, оставлены в тексте)",
}


# ==============================================================================
# 1. ВРЕМЯ
# ==============================================================================
# Takeout хранит время текстом, например: "Jul 18, 2026, 10:46:52 AM EEST".
# Нам нужно превратить его в настоящий объект datetime: только с ним можно
# сортировать карточки и считать паузы между ними.

# Словарь "название месяца -> номер месяца".
# Как он строится: "Jan Feb ... Dec".split() режет строку по пробелам и даёт
# список из 12 названий. enumerate(список, 1) нумерует их, начиная с 1:
# (1, "Jan"), (2, "Feb") ... Конструкция {m: i for i, m in ...} называется
# "словарное включение" (dict comprehension): для каждой пары создаёт запись
# "название: номер".
MONTHS = {m: i for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}

# Часовые пояса, которые встречаются в Takeout: "название -> сдвиг в часах от UTC".
# EEST (летнее время Восточной Европы) = UTC+3 и так далее.
# Если в файле встретится пояс, которого здесь нет, программа не упадёт:
# она примет время за UTC и посчитает такие случаи в диагностике.
TZ_OFFSETS = {
    "UTC": 0, "GMT": 0, "WET": 0, "WEST": 1, "BST": 1, "CET": 1, "CEST": 2,
    "EET": 2, "EEST": 3, "MSK": 3,
}

# Регулярное выражение (regex) для поиска времени в тексте. Разберём по частям:
#   ([A-Z][a-z]{2})      группа 1: месяц - заглавная буква + ровно 2 строчные ("Jul")
#   (\d{1,2})            группа 2: день - одна или две цифры
#   (\d{4})              группа 3: год - ровно 4 цифры
#   (\d{1,2}):(\d{2}):(\d{2})   группы 4-6: часы, минуты, секунды
#   \s                   один пробельный символ. ВАЖНО: перед AM/PM в Takeout стоит
#                        не обычный пробел, а "узкий неразрывный" (U+202F).
#                        Шаблон \s подходит к любому пробельному символу, поэтому
#                        ловит и его. Именно поэтому здесь не просто " ".
#   ([AP]M)              группа 7: "AM" или "PM"
#   (?:\s([A-Z]{2,5}))?  группа 8: название пояса ("EEST"). "(?:...)" - группа без
#                        номера, а "?" в конце значит "может отсутствовать".
# Скобки () "запоминают" найденный кусок, потом его можно достать по номеру.
# Строка начинается с r"..." (raw-строка): в ней обратный слэш \ не считается
# специальным символом Python, а передаётся в regex как есть. Для регулярных
# выражений почти всегда пишут именно так.
TS_RE = re.compile(
    r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4}), (\d{1,2}):(\d{2}):(\d{2})\s([AP]M)(?:\s([A-Z]{2,5}))?"
)


def parse_ts(match):
    """Превращает найденный regex-ом текст времени в объект datetime.

    Вход:  match - результат TS_RE.search(...): объект с запомненными группами.
    Выход: пара (datetime, пояс_известен) либо (None, False), если время
           разобрать не удалось.
    """
    # match.groups() возвращает все 8 запомненных кусков сразу. Строка ниже
    # "распаковывает" их по 8 переменным за один шаг.
    mon, day, year, hh, mm, ss, ampm, tzname = match.groups()

    # Защита: вдруг "месяц" оказался не месяцем (например "Foo"). Тогда сдаёмся.
    if mon not in MONTHS:
        return None, False

    # Перевод 12-часового формата в 24-часовой. Остаток от деления на 12 даёт:
    #   12 AM -> 12 % 12 = 0   (полночь = 0 часов)
    #   1 AM  -> 1             ...  11 AM -> 11
    #   12 PM -> 0 + 12 = 12   (полдень)
    #   1 PM  -> 1 + 12 = 13   ...  11 PM -> 23
    # "(12 if ampm == 'PM' else 0)" - условное выражение: 12 для PM, иначе 0.
    h = int(hh) % 12 + (12 if ampm == "PM" else 0)

    # Сдвиг пояса. TZ_OFFSETS.get(ключ) возвращает значение или None, если ключа
    # нет (в отличие от TZ_OFFSETS[ключ], которое при отсутствии ключа упало бы).
    # "if tzname else None" - если пояс вообще не был указан, сразу None.
    offset = TZ_OFFSETS.get(tzname) if tzname else None

    # Если сдвиг известен - делаем пояс "UTC+N"; иначе считаем время за UTC.
    tz = timezone(timedelta(hours=offset)) if offset is not None else timezone.utc

    # try/except: "попробуй; если случится ошибка ValueError - не падай, а сделай
    # вот это". datetime(...) бросает ValueError на невозможных датах
    # (например, 30 февраля).
    try:
        dt = datetime(int(year), MONTHS[mon], int(day), h, int(mm), int(ss), tzinfo=tz)
    except ValueError:
        return None, False

    # Второе значение - "пояс известен": True, если offset нашёлся в таблице.
    return dt, offset is not None


# ==============================================================================
# 2. УСТРОЙСТВО КАРТОЧЕК TAKEOUT (регулярные выражения)
# ==============================================================================

# Граница карточек. "(?=...)" - "позитивная проверка вперёд": шаблон совпадает с
# ПОЗИЦИЕЙ перед текстом '<div class="outer-cell', не "съедая" сам текст. Когда
# мы режем строку по такому шаблону (CARD_SPLIT.split), разрез происходит ровно
# перед каждой карточкой, а начало <div ...> остаётся в карточке.
CARD_SPLIT = re.compile(r'(?=<div class="outer-cell)')

# Ячейка с содержимым карточки: от "<div class="content-cell ...">" до начала
# СЛЕДУЮЩЕЙ ячейки (правая, пустая, с классом "text-right").
#   (.*?)   группа 1 - всё содержимое. "*?" - "ленивое" повторение: берёт как
#           можно МЕНЬШЕ символов, поэтому остановится на первом подходящем конце,
#           а не пробежит до самого конца файла.
#   re.S    флаг: точка "." подходит и к переводу строки (по умолчанию нет).
CELL_RE = re.compile(
    r'<div class="content-cell[^"]*">(.*?)</div><div class="content-cell[^"]*text-right">', re.S)

# Запасной вариант на случай, если правой ячейки нет (например, образец обрезан
# посреди карточки): берём всё после открытия первой ячейки.
CELL_FALLBACK_RE = re.compile(r'<div class="content-cell[^"]*">(.*)', re.S)

# Начало служебной ячейки Google ("Products / Why is this here?"). Всё, что после
# неё, - мусор; в запасном варианте по этой строке обрезаем.
CAPTION_CUT = '<div class="content-cell mdl-cell mdl-cell--12-col mdl-typography--caption"'

# Блок "Служебный файл проекта ...": от заголовка <h2> со словами
# "Служебный файл" (или "Service file") до следующего <hr> либо до конца ответа.
# Это особенность ВАШЕГО Gemini: он сам добавляет такой блок в ответы.
#   (?:<hr\s*/?>\s*)?   необязательная горизонтальная линия перед заголовком
#   <h2[^>]*>[^<]*(?:Служебн\w+\s+файл|Service\s+file)[^<]*</h2>   сам заголовок
#   .*?                 всё содержимое блока (лениво)
#   (?=<hr\s*/?>|\Z)    остановиться ПЕРЕД следующим <hr> или в конце текста
#                       (\Z - конец строки)
#   re.S | re.I         точка ловит переводы строк; регистр букв не важен
#   "Служебн\w+" - \w+ значит "одна или больше букв", так ловятся все окончания:
#   Служебный, Служебного ...
SERVICE_RE = re.compile(
    r"(?:<hr\s*/?>\s*)?<h2[^>]*>[^<]*(?:Служебн\w+\s+файл|Service\s+file)[^<]*</h2>.*?(?=<hr\s*/?>|\Z)",
    re.S | re.I)


# ==============================================================================
# 3. РАЗБОР ОДНОЙ КАРТОЧКИ
# ==============================================================================

def _normalize_kind(head):
    """Название типа записи без хвоста.

    "Created Gemini Canvas titled Vagus nerve" -> "Created Gemini Canvas",
    "Gave feedback: Good response"             -> "Gave feedback".
    Нужно, чтобы отчёт считал однотипные записи вместе, а не по названию документа.
    """
    first_line = (head.strip().splitlines() or [""])[0]
    kind = re.split(r":| titled", first_line)[0].strip()
    return kind[:40] or "unknown"


def _parse_card(card_html, stats):
    """Разбирает ОДНУ карточку Takeout и возвращает её в формате договора (schema.py).

    Вход:  card_html - HTML одной карточки; stats - Counter для диагностики.
    Выход: словарь-карточка либо None, если разобрать не удалось.

    ВАЖНО: тексты здесь "сырые" (секреты не замаскированы): маскировку делает пульт.
    Идентификатор считается от момента времени в UTC и текста запроса, поэтому он
    не зависит ни от названия часового пояса (EEST/EET), ни от правил очистки.
    """

    # --- Шаг 1. Находим ячейку с содержимым.
    m = CELL_RE.search(card_html)
    if m:
        cell = m.group(1)
    else:
        m = CELL_FALLBACK_RE.search(card_html)
        if not m:
            return None
        stats["fallback_cell"] += 1
        cell = m.group(1).split(CAPTION_CUT)[0]

    # --- Шаг 2. Время делит ячейку на "запрос" (до) и "ответ" (после).
    tm = TS_RE.search(cell)
    tz_assumed = False
    if tm:
        dt, tz_known = parse_ts(tm)
        before, after, ts_raw = cell[:tm.start()], cell[tm.end():], tm.group(0)
        if dt and not tz_known:
            stats["tz_unknown"] += 1
            tz_assumed = True
    else:
        dt, before, after, ts_raw = None, "", cell, ""
        stats["no_ts"] += 1

    before = re.sub(r"(<br\s*/?>\s*)+$", "", before)
    after = re.sub(r"^(\s*<br\s*/?>)+", "", after)

    # --- Шаг 3. Тип записи и текст запроса.
    head = plain_text(before)
    if head.startswith("Prompted"):
        kind, prompt = "Prompted", head[len("Prompted"):].strip()
    else:
        kind, prompt = _normalize_kind(head), head

    # --- Шаг 4. Идентификатор: момент времени в UTC + текст запроса.
    # astimezone(utc) приводит время к единому виду, поэтому один и тот же момент даёт
    # один и тот же id при любом названии пояса и любом сезоне.
    if dt is not None:
        cid = sha(dt.astimezone(timezone.utc).isoformat() + "\n" + prompt)
    else:
        cid = sha("no-ts\n" + prompt)

    # --- Шаг 5. Служебный блок вырезаем из ответа, а на его месте оставляем метку-заменитель.
    service_html = ""
    sm = SERVICE_RE.search(after)
    if sm:
        service_html = sm.group(0)
        after = after[:sm.start()] + "<p>" + placeholder_for(cid) + "</p>" + after[sm.end():]
    elif re.search(r"Служебн\w+\s+файл", after, re.I):
        # Слова "Служебный файл" в ответе есть, а блока нет: обычное упоминание (шум).
        stats["service_mentioned_not_parsed"] += 1

    # --- Шаг 6. HTML -> Markdown (ответ и служебный блок отдельно).
    answer_md = html_to_md(after)
    service_md = html_to_md(service_html) if service_html else ""
    service_md = re.sub(r"^-{3,}\s*", "", service_md)

    # --- Шаг 7. Всё, что специфично для Takeout, кладём в meta.
    meta = {"ts_raw": ts_raw, "kind": kind}
    if tz_assumed:
        meta["tz_assumed_utc"] = True
    if kind != "Prompted":
        # Всё непонятное помечается skipped (это делает стадия 3 при загрузке карточек).
        meta["skip_reason"] = f"тип записи не «Prompted»: {kind}"

    return {
        "schema_version": SCHEMA_VERSION,
        "id": cid,
        "source": PROFILE_ID,
        "conversation_id": None,      # Takeout не говорит, к какому чату относится запись
        "conversation_title": None,
        "ts": dt.isoformat() if dt else None,
        "prompt": prompt,
        "answer_md": answer_md,
        "service_md": service_md or None,
        "meta": meta,
    }


# ==============================================================================
# 4. ГЛАВНАЯ ФУНКЦИЯ АДАПТЕРА
# ==============================================================================

def iter_cards(path, stats, limit=0):
    """Читает файл Takeout и по одной отдаёт карточки в формате договора.

    Это ГЕНЕРАТОР (функция с yield): карточки отдаются по одной, по мере запроса.

    Вход:  path - путь к MyActivity.html;
           stats - Counter для диагностики (см. STAT_LABELS);
           limit - если больше нуля, обработать только первые limit карточек.
    Порядок карточек - как в файле (от новых к старым); сортировку выполняет пульт.
    """
    # Читаем файл целиком в память. errors="replace": битые байты заменяются
    # знаком "?", а не останавливают чтение.
    raw = Path(path).read_text(encoding="utf-8", errors="replace")

    # Режем на карточки и оставляем только настоящие. Первый кусок - это шапка
    # файла (CSS и т.п.), он не начинается с outer-cell и отбрасывается условием
    # "if p.startswith(...)" в конце включения списка.
    pieces = [p for p in CARD_SPLIT.split(raw) if p.startswith('<div class="outer-cell')]
    if limit:
        pieces = pieces[:limit]  # режим быстрой проверки: только первые N

    # Сколько карточек найдено - пульт покажет это пользователю.
    stats["cards_found"] = len(pieces)

    for piece in pieces:
        card = _parse_card(piece, stats)
        if card is None:
            stats["failed_cards"] += 1
            continue  # плохую карточку пропускаем и идём дальше
        yield card    # отдаём карточку вызывающему коду и "засыпаем" до следующего запроса
