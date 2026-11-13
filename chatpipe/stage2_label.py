# -*- coding: utf-8 -*-
"""
stage2_label.py - ТОЧКА ВХОДА ЭТАПА 2 (разметка карточек метками и сборка пачек).

Что делает: читает аргументы командной строки, находит файл настроек, собирает сеанс
(stage2_session.Session) и запускает либо меню (по умолчанию), либо одну из команд без
вопросов - для автоматического запуска по расписанию.

Запуск (из папки проекта, рядом с run_stage2.py):
    python3 run_stage2.py                       # интерактивное меню
    python3 run_stage2.py --stats               # только статистика
    python3 run_stage2.py --apply-rules         # применить все правила (без вопросов)
    python3 run_stage2.py --compose             # собрать пачки из карточек со статусом ready
    python3 run_stage2.py --cards путь/к/cards.jsonl     # другой файл карточек

Меню требует живого терминала. Если программу запустить без терминала (например, из cron)
и без команды, она сообщит об ошибке, а не будет вечно ждать ответа, которого не будет.
"""

import argparse
import sys
from pathlib import Path

from . import __version__
from .annotations import AnnotationError
from .console import Console, QuitSession
from .compat import ensure_compatibility
from .dictionary import DictionaryError
from .rules import RuleSyntaxError
from .schema import CardValidationError
from .settings import (SETTINGS_FILE_DEFAULT, SettingsError, default_settings,
                       load_settings, settings_problems)
from .stage2_menus import Menus
from .stage2_session import Session, resolve_paths


def build_parser():
    """Описывает аргументы командной строки."""
    ap = argparse.ArgumentParser(
        description=f"chatpipe {__version__}, этап 2: разметка карточек метками и сборка пачек для ИИ")
    ap.add_argument("--cards", default=None, help="файл карточек (по умолчанию из настроек)")
    ap.add_argument("--settings", default=SETTINGS_FILE_DEFAULT,
                    help=f"файл настроек (по умолчанию {SETTINGS_FILE_DEFAULT}, если он существует)")
    # Взаимоисключающие команды: можно указать не больше одной.
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--stats", action="store_true", help="показать статистику и выйти")
    group.add_argument("--apply-rules", action="store_true",
                       help="применить все правила (каждое по своему режиму) и выйти")
    group.add_argument("--compose", action="store_true",
                       help="собрать пачки для ИИ и выйти")
    ap.add_argument("--include-tagged", action="store_true",
                    help="с --compose: брать и карточки со статусом tagged, не только ready")
    ap.add_argument("--max-chars", type=int, default=None,
                    help="с --compose: лимит размера пачки в символах (по умолчанию из настроек)")
    return ap


def load_settings_or_default(path, console):
    """Читает файл настроек, если он есть; иначе берёт настройки по умолчанию."""
    p = Path(path)
    if not p.exists():
        return default_settings()
    settings = load_settings(p)
    for problem in settings_problems(settings):
        console.say(f"ВНИМАНИЕ, в настройках: {problem}")
    return settings


def main(argv=None, console=None):
    """Запуск этапа 2. Возвращает код результата (0 - успех). console можно подставить для проверок."""
    args = build_parser().parse_args(argv)
    real_console = console is None          # True, если работаем с настоящей клавиатурой
    console = console or Console()
    interactive = not (args.stats or args.apply_rules or args.compose)

    # Меню без терминала запускать нельзя (см. шапку файла): оно вечно ждало бы ответов.
    # Для проверок подставляется заготовленная консоль, там эта проверка не нужна.
    if interactive and real_console and not sys.stdin.isatty():
        print("Меню требует терминала. Для запуска без терминала укажите команду: "
              "--stats, --apply-rules или --compose.", file=sys.stderr)
        return 2

    try:
        settings = load_settings_or_default(args.settings, console)
        if not ensure_compatibility(settings["stage2"]["use_lemmas"], write=console.say):
            return 3
        session = Session(settings, resolve_paths(settings, args.cards), say=console.say)
        session.load()

        if args.stats:
            console.say(session.stats_text(with_words=False))
            return 0

        if args.apply_rules:
            if not session.rules.rules:
                console.say("Правил нет, применять нечего.")
                return 0
            for rule_id, res in session.apply_rules().items():
                console.say(f"{rule_id}: подходит {res['matched']}, доступно автоматике {res['eligible']}, "
                            f"изменено карточек {res['changed']}")
            return 0

        if args.compose:
            statuses = ("ready", "tagged") if args.include_tagged else ("ready",)
            console.say(session.compose(statuses=statuses, max_chars=args.max_chars))
            return 0

        Menus(session, console).run()
        return 0

    except QuitSession:
        console.say("\nЗавершено. Все изменения уже сохранены.")
        return 0
    except KeyboardInterrupt:
        console.say("\nПрервано (Ctrl+C). Все изменения уже сохранены.")
        return 130
    except FileNotFoundError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    except (CardValidationError, SettingsError, DictionaryError, RuleSyntaxError, AnnotationError) as e:
        print(f"Ошибка в данных:\n{e}", file=sys.stderr)
        return 1
