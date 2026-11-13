# -*- coding: utf-8 -*-
"""
names.py - ВСЕ ИМЕНА ФАЙЛОВ ПРОГРАММЫ В ОДНОМ МЕСТЕ.

Схема именования файлов данных: [префикс]_[смысл]_[стадия].[расширение].
Если нужно переименовать какой-то файл, правится ровно одна строка здесь.
Модули не пишут имена файлов вручную, а берут их из этого модуля.
"""

from . import PREFIX

# --- Стадия 1: результаты (машинные данные, их можно пересоздать)
CARDS_FILE = f"{PREFIX}_cards_stage1.jsonl"                    # карточки по договору
REPORT_FILE = f"{PREFIX}_report_stage1.txt"                    # отчёт этапа 1
SERVICE_TEXTS_FILE = f"{PREFIX}_service-files_stage1.md"          # вырезанные служебные тексты
SERVICE_FILE = f"{PREFIX}_service-versions_stage1.md"          # версии служебного блока

# --- Стадия 2: ваши данные (результат ручной работы, делайте резервные копии!)
DICTIONARY_FILE = f"{PREFIX}_dictionary_stage2.txt"            # словарь понятий
ANNOTATIONS_FILE = f"{PREFIX}_annotations_stage2.jsonl"        # метки и статусы карточек
RULES_FILE = f"{PREFIX}_anchor-rules_stage2.txt"               # правила меток

# --- Стадия 2: результаты (машинные данные)
BUNDLES_DIR = "bundles"                                        # подпапка для пачек в папке output
BUNDLES_REPORT_FILE = f"{PREFIX}_bundles-report_stage2.txt"    # отчёт о сборке пачек


def help_file_name(topic):
    """Имя файла подробной справки: chatpipe_help_<тема>_stage2.txt (тексты задаёт пользователь)."""
    return f"{PREFIX}_help_{topic}_stage2.txt"
