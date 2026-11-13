# -*- coding: utf-8 -*-
"""
bundles.py - СБОРКА ПАЧЕК: готовые карточки одной темы -> файлы XML для ИИ.

Что делает:
  1. Берёт карточки с нужным статусом (по умолчанию - только "ready", то есть те, где
     человек явно поставил флаг "готово к компоновке").
  2. Группирует их по ПОНЯТИЮ (теме): одна карточка с двумя метками попадёт в обе
     группы. Внутри группы карточки идут в хронологическом порядке: поздние решения
     оказываются после ранних, и модель при "сушке" видит ход событий.
  3. Режет группу на "пачки" не длиннее лимита (в символах). Карточку никогда не
     разрезает: если одна карточка длиннее лимита, она становится отдельной пачкой
     (в отчёте помечена как oversize).
  4. Каждую пачку собирает в XML (xml_render.py), проверяет разборщиком и пишет в файл:
         chatpipe_bundle_<метка>_<номер части>_stage2.xml

ВАЖНО: пачки - это ВЫХОД программы, а не данные. Они собираются заново по запросу.
Поэтому перед записью старые файлы пачек в папке вывода (только с таким именем)
удаляются: иначе после уменьшения числа частей остались бы устаревшие "хвосты".
Ваши настоящие данные (словарь, аннотации, правила) лежат в другом месте и тут не трогаются.
"""

from pathlib import Path

from . import PREFIX
from .annotations import now_iso
from .xml_render import check_wellformed, render_bundle_xml, render_card_xml


def bundle_file_name(label, part):
    """Имя файла пачки по схеме [префикс]_[смысл]_[стадия]: chatpipe_bundle_it_coding_python_001_stage2.xml."""
    return f"{PREFIX}_bundle_{label}_{part:03d}_stage2.xml"


def clear_old_bundles(out_dir):
    """Удаляет в папке старые файлы пачек (только по нашему шаблону имени). Возвращает сколько удалено."""
    removed = 0
    out = Path(out_dir)
    if out.exists():
        # glob("шаблон") перебирает файлы папки по маске; * - любая последовательность символов.
        for path in out.glob(f"{PREFIX}_bundle_*_stage2.xml"):
            path.unlink()
            removed += 1
    return removed


def split_into_parts(rendered, max_chars):
    """Делит список готовых карточек на части не длиннее max_chars символов.

    Вход:  rendered - список пар (xml_карточки, длина_в_символах);
           max_chars - лимит одной части.
    Выход: список частей; часть - список пар из rendered. Жадный алгоритм: карточки
           кладутся в текущую часть, пока помещаются; не поместилась - начинаем новую.
    Карточка, которая одна длиннее лимита, получает отдельную часть.
    """
    parts, current, size = [], [], 0
    for item in rendered:
        length = item[1]
        if current and size + length > max_chars:
            parts.append(current)   # текущая часть заполнена - закрываем её
            current, size = [], 0
        current.append(item)
        size += length
    if current:
        parts.append(current)
    return parts


def compose_bundles(cards, store, dictionary, out_dir, max_chars, statuses=("ready",)):
    """Собирает и записывает пачки. Возвращает список описаний записанных пачек.

    Вход:
      cards       карточки в хронологическом порядке (список или итератор);
      store       хранилище аннотаций (annotations.AnnotationStore);
      dictionary  словарь понятий;
      out_dir     папка для файлов пачек;
      max_chars   лимит размера одной пачки в символах;
      statuses    карточки каких статусов брать.
    Выход: список словарей {label, concept_id, part, parts, cards, chars, oversize, path}.
    """
    generated_at = now_iso()

    # --- Шаг 1. Группировка карточек по понятиям. {id понятия: [карточки]}
    groups = {}
    skipped_unknown = 0
    for card in cards:
        if store.status(card["id"]) not in statuses:
            continue
        for concept_id in store.concept_ids(card["id"]):
            if dictionary.get(concept_id) is None:
                skipped_unknown += 1   # метка ссылается на понятие, которого уже нет в словаре
                continue
            groups.setdefault(concept_id, []).append(card)

    # --- Шаг 2. Рендер и нарезка по лимиту.
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    clear_old_bundles(out)

    summaries = []
    # Сортируем группы по тексту метки: порядок файлов предсказуем.
    for concept_id in sorted(groups, key=dictionary.label):
        label = dictionary.label(concept_id)
        rendered = []
        for card in groups[concept_id]:
            xml = render_card_xml(card, store.concepts_of(card["id"]), dictionary)
            rendered.append((xml, len(xml)))
        parts = split_into_parts(rendered, max_chars)

        for number, part in enumerate(parts, 1):
            xml_text = render_bundle_xml(
                [xml for xml, _length in part], label, concept_id, number, len(parts), generated_at)
            check_wellformed(xml_text)   # самопроверка: бракованный XML не попадёт в файл
            path = out / bundle_file_name(label, number)
            path.write_text(xml_text, encoding="utf-8")
            chars = sum(length for _xml, length in part)
            summaries.append({
                "label": label, "concept_id": concept_id, "part": number, "parts": len(parts),
                "cards": len(part), "chars": chars,
                "oversize": len(part) == 1 and chars > max_chars,
                "path": str(path),
            })
    return summaries, skipped_unknown


def format_summary(summaries, skipped_unknown, max_chars):
    """Читаемая сводка по собранным пачкам (для экрана и файла отчёта)."""
    if not summaries:
        return "Пачек нет: не нашлось карточек с нужным статусом и метками."
    lines = [f"Собрано файлов: {len(summaries)} (лимит пачки: {max_chars:,} символов)", ""]
    for s in summaries:
        flag = "  <-- карточка длиннее лимита" if s["oversize"] else ""
        lines.append(f"  {s['label']}  часть {s['part']}/{s['parts']}: карточек {s['cards']}, "
                     f"{s['chars']:,} симв.{flag}")
    if skipped_unknown:
        lines.append("")
        lines.append(f"ВНИМАНИЕ: {skipped_unknown} меток ссылаются на понятия, которых нет в словаре (пропущены).")
    return "\n".join(lines)
