# -*- coding: utf-8 -*-
"""
stage1_report.py - отчёт и служебные файлы этапа 1.

Модуль работает ТОЛЬКО со стандартными карточками (schema.py) и не знает, из
какого источника они пришли. Всё, что известно только адаптеру (диагностика
разбора), приходит сюда готовым: словарём счётчиков и словарём подписей.

Что здесь есть:
  build_report(...)                 собирает текст отчёта со статистикой
  collect_service_versions(cards)   находит уникальные версии "служебного блока"
  write_service_versions(path, v)   записывает эти версии в читаемый файл .md
"""

import os
import re                          # регулярные выражения
import statistics                  # медиана (серединное значение) для отчёта
from collections import Counter    # словарь-счётчик
from pathlib import Path           # удобная работа с путями

from . import __version__          # версия программы (для шапки отчёта)
from .schema import ts_to_datetime # время карточки (текст ISO) -> объект datetime
from .text_tools import marker_for, sha        # короткий отпечаток текста


# ==============================================================================
# ВЕРСИИ СЛУЖЕБНОГО БЛОКА
# ==============================================================================

def service_fingerprint(service_md):
    """Отпечаток служебного блока. Пробелы нормализуем: лишний пробел не делает версию "новой"."""
    return sha(re.sub(r"\s+", " ", service_md))


def collect_service_versions(cards):
    """Находит уникальные версии служебного блока в порядке первого появления.

    Вход:  cards - карточки (уже в хронологическом порядке).
    Выход: словарь "отпечаток -> {first: время_первого_появления, count: в_скольких_карточках, md: текст}".
    Обычный словарь Python помнит порядок добавления ключей, поэтому версии
    останутся в порядке появления.
    """
    versions = {}
    for card in cards:
        service_md = card.get("service_md")
        if not service_md:
            continue  # у карточки нет служебного блока
        fp = service_fingerprint(service_md)
        if fp not in versions:  # новая версия - запоминаем, когда она впервые встретилась
            versions[fp] = {"first": card["ts"], "count": 0, "md": service_md}
        versions[fp]["count"] += 1  # и считаем, в скольких карточках она есть
    return versions


def write_service_versions(path, versions):
    """Записывает версии служебного блока в файл Markdown (для чтения глазами)."""
    with Path(path).open("w", encoding="utf-8") as f:
        f.write("# Версии служебного файла проекта (в порядке появления)\n\n")
        for fp, v in versions.items():
            # <!-- ... --> - комментарий Markdown: на экране не виден, но несёт
            # сведения о версии.
            f.write(f"<!-- version {fp} | впервые: {v['first']} | карточек: {v['count']} -->\n\n")
            f.write(v["md"] + "\n\n---\n\n")


_SERVICE_TEXTS_HEADER = (
    "# Вырезанные служебные тексты (service-files)\n\n"
    "Здесь лежат все фрагменты, которые программа сочла служебным файлом проекта и вырезала\n"
    "из текста ответов. На их месте в карточке стоит метка-заменитель с тем же номером svc-...\n"
    "Файл создаётся заново при каждом запуске стадии 2.\n\n---\n\n")

# Запись файла: начало с меткой и временем, текст, конец с той же меткой.
_SERVICE_ENTRY_RE = re.compile(
    r"<!-- SERVICE-BEGIN (\S+) ts=(\S*) -->\n\n(.*?)\n\n<!-- SERVICE-END \1 -->", re.S)


def write_service_texts(path, cards):
    """Пишет вырезанные служебные тексты (поле service_md карточек) в один MD-файл.

    Возвращает число записей. Запись атомарная (временный файл, потом замена).
    """
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    count = 0
    with tmp.open("w", encoding="utf-8") as f:
        f.write(_SERVICE_TEXTS_HEADER)
        for card in cards:
            text = card.get("service_md")
            if not text:
                continue
            marker = marker_for(card["id"])
            f.write(f"<!-- SERVICE-BEGIN {marker} ts={card.get('ts') or '?'} -->\n\n")
            f.write(text.strip() + "\n\n")
            f.write(f"<!-- SERVICE-END {marker} -->\n\n")
            count += 1
    os.replace(tmp, path)
    return count


def read_service_texts(path):
    """Читает файл служебных текстов. Возвращает словарь {метка: текст}."""
    text = Path(path).read_text(encoding="utf-8")
    return {m.group(1): m.group(3) for m in _SERVICE_ENTRY_RE.finditer(text)}


# ==============================================================================
# ОТЧЁТ
# ==============================================================================

def gap_buckets(gaps_min):
    """Раскладывает паузы (в минутах) по корзинам: "< 2 мин", "2-5 мин" и т.д.

    Вход:  gaps_min - список пауз между соседними карточками, в минутах.
    Выход: список пар (название_корзины, сколько_пауз_попало).
    """
    # Верхние границы корзин. float("inf") - "бесконечность": последняя корзина
    # принимает всё, что больше предыдущих границ.
    edges = [(2, "< 2 мин"), (5, "2-5 мин"), (15, "5-15 мин"), (30, "15-30 мин"),
             (60, "30-60 мин"), (180, "1-3 ч"), (720, "3-12 ч"), (float("inf"), "> 12 ч")]
    cnt = Counter()
    for g in gaps_min:
        # Ищем первую корзину, чья верхняя граница больше паузы.
        # break - выйти из внутреннего цикла, как только корзина найдена.
        for lim, label in edges:
            if g < lim:
                cnt[label] += 1
                break
    # Возвращаем корзины в исходном порядке (cnt[label] даёт 0 для пустых).
    # "_" - имя переменной, которая нам не нужна (граница), по договорённости.
    return [(label, cnt[label]) for _, label in edges]


def build_report(cards, source_bytes, profile_id, adapter_stats, stat_labels,
                 secrets, removed_duplicates):
    """Собирает текст отчёта со статистикой по карточкам.

    Вход:
      cards              итоговые карточки (по договору, в хронологическом порядке);
      source_bytes       размер исходного файла в байтах;
      profile_id         какой профиль источника использовался;
      adapter_stats      Counter с диагностикой от адаптера;
      stat_labels        словарь "ключ диагностики -> подпись" (от того же адаптера);
      secrets            Counter "тип секрета -> сколько замаскировано";
      removed_duplicates сколько дубликатов карточек убрано.
    Выход: готовый текст отчёта.
    """
    L = []          # список строк отчёта
    add = L.append  # короткое имя для L.append: вместо L.append("...") пишем add("...")
    n = len(cards)

    add(f"ОТЧЁТ ЭТАПА 1 (chatpipe {__version__})")
    add("=" * 60)  # строку можно "умножить": "=" * 60 даёт 60 знаков "="
    add(f"Профиль источника: {profile_id}")
    # Форматирование в f-строках: {x:.1f} - число с одним знаком после запятой.
    add(f"Размер исходного файла: {source_bytes / 1048576:.1f} МиБ")
    add(f"Карточек найдено адаптером: {adapter_stats['cards_found']}")
    add(f"Карточек в результате: {n}")
    add(f"Дубликатов убрано (одинаковый id): {removed_duplicates}")

    # Диагностика адаптера: он сам решил, о чём сообщить. Печатаем подпись и число.
    # adapter_stats[ключ] для отсутствующего ключа даёт 0 (так устроен Counter).
    add("")
    add("Диагностика адаптера:")
    for key, label in stat_labels.items():
        add(f"  {label}: {adapter_stats[key]}")

    # Период и распределение по месяцам. Список "... for c in cards if c['ts']"
    # - "включение списка": берём время только у карточек, где оно есть.
    dts = [ts_to_datetime(c["ts"]) for c in cards if c["ts"]]
    if dts:
        add("")
        add(f"Период: {min(dts).date()} .. {max(dts).date()}")
        # strftime("%Y-%m") форматирует дату как "2026-07"
        months = Counter(d.strftime("%Y-%m") for d in dts)
        add("Карточек по месяцам: " + ", ".join(f"{k}: {v}" for k, v in sorted(months.items())))

    # Типы записей (если адаптер их указал в meta). .most_common(10) - десять самых частых.
    kinds = Counter(c["meta"].get("kind") for c in cards if c["meta"].get("kind"))
    if kinds:
        add("")
        add("Типы записей:")
        for k, v in kinds.most_common(10):
            add(f"  {v:6d}  {k}")  # {v:6d} - целое число, выровненное в 6 знаков

    # Объём текста. sum(... for c in cards) складывает длины по всем карточкам.
    # "or 1" в конце защищает от деления на ноль, если текста нет вообще.
    add("")
    chars_prompt = sum(len(c["prompt"]) for c in cards)
    chars_answer = sum(len(c["answer_md"]) for c in cards)
    chars_service = sum(len(c["service_md"] or "") for c in cards)
    total = chars_prompt + chars_answer + chars_service or 1
    add("Объём текста (символы):")
    # {x:>12,} - выровнять вправо в 12 знаков и разделить тысячи запятыми
    add(f"  запросы:         {chars_prompt:>12,}  ({100 * chars_prompt / total:.0f}%)")
    add(f"  ответы:          {chars_answer:>12,}  ({100 * chars_answer / total:.0f}%)")
    add(f"  служебные блоки: {chars_service:>12,}  ({100 * chars_service / total:.0f}%)")

    # Служебные блоки: сколько карточек с блоком и сколько РАЗНЫХ версий.
    # {...} без пар "ключ: значение" - это множество (set), хранящее каждое
    # значение один раз; поэтому len(uniq) = число уникальных версий.
    with_service = [c for c in cards if c["service_md"]]
    uniq = {service_fingerprint(c["service_md"]) for c in with_service}
    add("")
    add("Служебные файлы проекта:")
    add(f"  карточек с блоком: {len(with_service)} из {n}")
    add(f"  уникальных версий: {len(uniq)}")

    # Паузы между соседними карточками (карточки уже отсортированы по времени).
    gaps = []
    prev = None  # время предыдущей карточки; None - "предыдущей ещё нет"
    for c in cards:
        if not c["ts"]:
            continue  # continue - пропустить карточку и перейти к следующей
        dt = ts_to_datetime(c["ts"])
        if prev is not None:
            # Разность двух datetime - это timedelta; .total_seconds() / 60 - минуты.
            gaps.append((dt - prev).total_seconds() / 60)
        prev = dt
    if gaps:
        add("")
        add(f"Паузы между соседними карточками (медиана {statistics.median(gaps):.1f} мин):")
        for label, v in gap_buckets(gaps):
            add(f"  {label:>10}: {v}")

    # Пять самых больших карточек. sorted(..., key=функция, reverse=True) сортирует
    # по значению, которое возвращает функция (здесь - длина ответа), по убыванию.
    # [:5] - первые пять. {...!r} показывает значение в кавычках (repr).
    # chr(10) - это символ перевода строки: внутри f-строки в старых версиях
    # Python нельзя писать обратный слэш, поэтому "\n" заменён на chr(10).
    add("")
    add("Самые большие карточки (символы ответа):")
    for c in sorted(cards, key=lambda c: len(c["answer_md"]), reverse=True)[:5]:
        add(f"  {len(c['answer_md']):>9,}  {c['ts'] or '?'}  {c['prompt'][:60].replace(chr(10), ' ')!r}")

    # Секреты: выводим только счётчики по типам, значения нигде не показываются.
    add("")
    if secrets:
        add("ЗАМАСКИРОВАННЫЕ СЕКРЕТЫ (только счётчики, значения нигде не хранятся):")
        for k, v in secrets.most_common():
            add(f"  {v:6d}  {k}")
    else:
        add("Секретов по шаблонам не найдено.")

    return "\n".join(L)  # склеиваем строки отчёта через перевод строки
