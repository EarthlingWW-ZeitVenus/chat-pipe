# -*- coding: utf-8 -*-
"""
stage1_ingest.py - ПУЛЬТ ЭТАПА 1: файл экспорта -> стандартные карточки + отчёт.

Сам этот модуль почти ничего не умеет: он только связывает специалистов друг с
другом и задаёт порядок работы. Как "прораб" на стройке:

   1. выбирает профиль источника (флаг --profile, файл настроек или единственный
      доступный)                                            -> adapters/__init__.py
   2. адаптер читает файл и отдаёт "сырые" карточки          -> adapters/<профиль>.py
   3. секреты в карточках маскируются                        -> redaction.py
   4. карточки сортируются по времени, дубликаты убираются   -> schema.py
   5. карточки записываются в .jsonl С ПРОВЕРКОЙ ДОГОВОРА    -> schema.py
   6. пишутся отчёт и файл версий служебного блока           -> stage1_report.py

Результат лежит в папке вывода (три файла, имена по схеме [префикс]_[смысл]_[стадия]):
   chatpipe_cards_stage1.jsonl            карточки (то, что читает stage2)
   chatpipe_report_stage1.txt             отчёт со статистикой
   chatpipe_service-versions_stage1.md    версии служебного блока (для чтения глазами)

Запуск (из папки проекта, рядом с run_stage1.py):
   python3 run_stage1.py MyActivity2.html -o stage1_out
   python3 run_stage1.py MyActivity2.html --limit 50          # быстрая проверка
   python3 run_stage1.py --list-profiles                      # какие источники умеем читать
"""

import argparse            # разбор аргументов командной строки
import sys                 # sys.stderr - канал для сообщений о ходе работы
from collections import Counter  # словарь-счётчик
from pathlib import Path   # удобная работа с путями

from . import __version__
from .compat import ensure_compatibility               # проверка библиотек и процессора
from .adapters import get_adapter, list_profiles       # реестр адаптеров
from .redaction import redact_card                     # маскирование секретов
from .schema import card_sort_key, write_cards         # договор между стадиями
from .settings import (SETTINGS_FILE_DEFAULT, SettingsError,
                       load_settings, settings_problems)  # файл настроек
from .stage1_report import (build_report, collect_service_versions,
                            write_service_texts, write_service_versions)    # отчёт

# Имена файлов результата берём из names.py, где собраны ВСЕ имена файлов программы
# (схема [префикс]_[смысл]_[стадия].[расширение]); сами имена здесь не пишем.
from .names import CARDS_FILE, REPORT_FILE, SERVICE_FILE, SERVICE_TEXTS_FILE

# Папка вывода, если её не указали ни флагом, ни в настройках.
OUTPUT_DIR_FALLBACK = "stage1_out"


def log(message):
    """Печатает сообщение о ходе работы в stderr (канал ошибок).

    Почему stderr, а не обычный print: так сообщения о ходе работы не смешиваются
    с основным выводом программы (отчётом), и их можно отдельно перенаправить.
    """
    print(message, file=sys.stderr)


def choose_profile(cli_profile, settings):
    """Выбирает профиль источника. Приоритет: флаг > файл настроек > единственный доступный.

    Вход:  cli_profile - значение флага --profile (или None);
           settings - словарь настроек (или None, если файла нет).
    Выход: пара (идентификатор_профиля, пояснение_откуда_взят).
    Бросает SystemExit с понятным текстом, если выбрать нечего.
    """
    # 1) Явный флаг командной строки главнее всего.
    if cli_profile:
        return cli_profile, "из флага --profile"

    # 2) Файл настроек (если он есть и в нём что-то указано).
    if settings and settings["source"]["profile_id"]:
        return settings["source"]["profile_id"], "из файла настроек"

    # 3) Если профиль в программе один - выбор очевиден. Но говорим об этом вслух:
    # молчаливые догадки - плохая привычка.
    profiles = list_profiles()
    if len(profiles) == 1:
        return profiles[0][0], "единственный доступный профиль (не задан ни флагом, ни в настройках)"

    names = ", ".join(pid for pid, _ in profiles)
    raise SystemExit(f"Профиль источника не задан. Укажите --profile. Доступные: {names}")


def run_stage1(input_path, out_dir, profile_id, limit=0):
    """Выполняет весь этап 1 и возвращает текст отчёта.

    Эта функция - сердце модуля. Командная строка (main) лишь собирает для неё
    параметры. Благодаря такому разделению этап 1 можно вызвать и из другой
    программы, например из будущего общего конвейера, без всякой командной строки.
    """
    adapter = get_adapter(profile_id)   # модуль-адаптер, умеющий читать этот формат
    adapter_stats = Counter()           # диагностика от адаптера
    secrets = Counter()                 # счётчик замаскированных секретов

    # --- Шаг 1. Чтение через адаптер + маскировка секретов.
    # adapter.iter_cards - генератор: карточки приходят по одной. enumerate(..., 1)
    # нумерует их с единицы, чтобы показывать прогресс.
    cards = []
    n = 0
    for n, card in enumerate(adapter.iter_cards(input_path, adapter_stats, limit), 1):
        if n == 1:
            # К моменту первой карточки адаптер уже знает, сколько их всего.
            log(f"Найдено карточек: {adapter_stats['cards_found']}")
        cards.append(redact_card(card, secrets))
        if n % 500 == 0:  # % - остаток от деления: условие верно на каждой 500-й карточке
            log(f"  ...{n}/{adapter_stats['cards_found']}")
    if n == 0:
        log("Карточек не найдено. Проверьте файл и выбранный профиль источника.")

    # --- Шаг 2. Хронологический порядок (по договору: от старых к новым).
    # В Takeout карточки идут от НОВЫХ к СТАРЫМ, нам нужен обратный порядок.
    # sort(key=...) сортирует "на месте" по значению, которое вернула функция.
    # Карточки без времени card_sort_key отправляет в конец.
    cards.sort(key=card_sort_key)

    # --- Шаг 3. Убираем дубликаты: по договору id уникален внутри файла.
    # seen - множество уже встреченных id. Оставляем первую карточку с таким id.
    unique_cards, seen, removed_duplicates = [], set(), 0
    for card in cards:
        if card["id"] in seen:
            removed_duplicates += 1
            continue
        seen.add(card["id"])
        unique_cards.append(card)

    # --- Шаг 4. Папка результата. parents=True создаёт и промежуточные папки,
    # exist_ok=True - не считает ошибкой, если папка уже есть.
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # --- Шаг 5. Запись карточек. write_cards проверяет каждую карточку по договору;
    # если адаптер ошибся, мы получим понятное сообщение, а не битый файл.
    write_cards(out / CARDS_FILE, unique_cards)

    # --- Шаг 6. Версии служебного блока и отчёт.
    write_service_versions(out / SERVICE_FILE, collect_service_versions(unique_cards))
    n_service = write_service_texts(out / SERVICE_TEXTS_FILE, unique_cards)
    log(f"Вырезано служебных текстов: {n_service} (файл {SERVICE_TEXTS_FILE})")

    source_bytes = Path(input_path).stat().st_size  # размер исходного файла в байтах
    report = build_report(
        unique_cards, source_bytes, profile_id, adapter_stats,
        adapter.STAT_LABELS, secrets, removed_duplicates)
    (out / REPORT_FILE).write_text(report + "\n", encoding="utf-8")
    return report


def main(argv=None):
    """Точка входа командной строки: читает аргументы, готовит параметры, запускает этап 1."""

    # --- Аргументы командной строки.
    # argparse сам обрабатывает то, что вы пишете после имени скрипта, и даже
    # генерирует справку: python3 run_stage1.py --help
    ap = argparse.ArgumentParser(
        description=f"chatpipe {__version__}, этап 1: файл экспорта -> стандартные карточки + отчёт")
    # nargs="?" - аргумент необязателен (иначе --list-profiles пришлось бы
    # сопровождать лишним именем файла).
    ap.add_argument("input", nargs="?", help="файл экспорта (например MyActivity.html)")
    # "-o" - короткая форма, "--out" - длинная; обе значат одно и то же.
    ap.add_argument("-o", "--out", default=None,
                    help=f"папка результата (по умолчанию из настроек или {OUTPUT_DIR_FALLBACK})")
    ap.add_argument("--profile", default=None,
                    help="профиль источника (по умолчанию из настроек); список: --list-profiles")
    ap.add_argument("--settings", default=SETTINGS_FILE_DEFAULT,
                    help=f"файл настроек (по умолчанию {SETTINGS_FILE_DEFAULT}, если он существует)")
    ap.add_argument("--limit", type=int, default=0, help="обработать только первые N карточек")
    # action="store_true": флаг без значения; есть в команде - True, нет - False.
    ap.add_argument("--list-profiles", action="store_true", help="показать доступные профили и выйти")
    args = ap.parse_args(argv)  # разбор; значения берём как args.input, args.out ...

    # --- Режим "просто покажи профили".
    if args.list_profiles:
        for profile_id, description in list_profiles():
            print(f"{profile_id}\n    {description}")
        return 0  # код 0 = "всё хорошо" (принятое в Linux соглашение)

    if not args.input:
        ap.error("укажите файл экспорта (или --list-profiles)")  # печатает ошибку и завершает

    # --- Файл настроек: читаем, только если он существует. Его отсутствие - не ошибка.
    settings = None
    settings_path = Path(args.settings)
    if settings_path.exists():
        try:
            settings = load_settings(settings_path)
        except SettingsError as e:
            raise SystemExit(f"Файл настроек {settings_path}: {e}")
        for problem in settings_problems(settings):
            log(f"ВНИМАНИЕ, в настройках: {problem}")

    # --- Проверка совместимости: при проблеме останавливаемся с инструкцией.
    use_lemmas = settings["stage2"]["use_lemmas"] if settings else True
    if not ensure_compatibility(use_lemmas, write=log):
        return 3

    # --- Профиль и папка вывода.
    profile_id, why = choose_profile(args.profile, settings)
    log(f"Профиль источника: {profile_id} ({why})")

    if args.out:
        out_dir = args.out
    elif settings:
        out_dir = settings["paths"]["output"]
    else:
        out_dir = OUTPUT_DIR_FALLBACK

    # --- Запуск. Известные нам ошибки превращаем в понятное сообщение вместо
    # длинной "простыни" технической трассировки.
    try:
        report = run_stage1(args.input, out_dir, profile_id, limit=args.limit)
    except FileNotFoundError:
        raise SystemExit(f"Файл не найден: {args.input}")
    except ValueError as e:
        # Сюда попадают и неизвестный профиль, и нарушения договора
        # (CardValidationError - подвид ValueError).
        raise SystemExit(f"Ошибка: {e}")

    print(report)
    log(f"\nГотово. Файлы в папке: {Path(out_dir).resolve()}")
    return 0
