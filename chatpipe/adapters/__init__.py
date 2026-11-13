# -*- coding: utf-8 -*-
"""
adapters - "переводчики" из конкретных форматов экспорта в стандартные карточки.

------------------------------------------------------------------------------
ИДЕЯ
------------------------------------------------------------------------------
Каждый источник чатов (Gemini через Takeout, Claude, DeepSeek...) хранит данные
по-своему. Адаптер знает ОДИН такой формат и умеет превращать его в стандартные
карточки (см. schema.py). Всё остальное в конвейере форматов не знает.

Здесь живёт РЕЕСТР: словарь "идентификатор профиля -> модуль-адаптер" и фабрика
get_adapter(), которая выдаёт нужный адаптер по идентификатору. Это тот же приём
"реестр + фабрика", что в library_adapter.py (get_adapter).

------------------------------------------------------------------------------
КАК ДОБАВИТЬ НОВЫЙ ИСТОЧНИК
------------------------------------------------------------------------------
  1. Создать файл adapters/<имя>.py (за образец взять gemini_takeout_html.py).
  2. В нём должны быть четыре вещи (см. REQUIRED_ATTRIBUTES ниже):
       PROFILE_ID   идентификатор профиля, например "claude-export-json"
       DESCRIPTION  описание для меню и справки (одна строка)
       STAT_LABELS  словарь "ключ диагностики -> подпись для отчёта"
       iter_cards(path, stats, limit=0)
                    генератор, который отдаёт карточки по договору schema.py
  3. Подключить модуль в этом файле: import и одна запись в _ADAPTERS.
Остальной конвейер (маскировка секретов, сортировка, отчёт, стадия 2) менять не нужно.
"""

from . import gemini_takeout_html  # подключаем модуль адаптера Gemini Takeout

# Какие "детали" обязан иметь каждый адаптер (договор для адаптеров).
REQUIRED_ATTRIBUTES = ("PROFILE_ID", "DESCRIPTION", "STAT_LABELS", "iter_cards")

# РЕЕСТР. Чтобы подключить новый адаптер, добавьте сюда строку вида:
#     claude_export_json.PROFILE_ID: claude_export_json,
_ADAPTERS = {
    gemini_takeout_html.PROFILE_ID: gemini_takeout_html,
}


def _check_registry():
    """Проверяет при подключении пакета, что у каждого адаптера есть все нужные детали.

    Зачем: если автор нового адаптера что-то забыл, ошибка появится СРАЗУ и с
    понятным текстом, а не глубоко внутри работы программы.
    """
    for profile_id, module in _ADAPTERS.items():
        # getattr / hasattr проверяют наличие атрибута у объекта по его имени.
        missing = [name for name in REQUIRED_ATTRIBUTES if not hasattr(module, name)]
        if missing:
            raise RuntimeError(
                f"адаптер '{profile_id}' не готов: нет {', '.join(missing)}")
        if module.PROFILE_ID != profile_id:
            raise RuntimeError(
                f"адаптер зарегистрирован как '{profile_id}', "
                f"но его PROFILE_ID = '{module.PROFILE_ID}'")


_check_registry()  # выполняется один раз при первом подключении пакета adapters


def list_profiles():
    """Список доступных профилей: [(идентификатор, описание), ...].

    Нужен для меню выбора источника и для флага --list-profiles.
    """
    return [(profile_id, module.DESCRIPTION) for profile_id, module in _ADAPTERS.items()]


def get_adapter(profile_id):
    """Возвращает модуль-адаптер по идентификатору профиля.

    Бросает ValueError с перечнем доступных профилей, если такого нет.
    """
    if profile_id not in _ADAPTERS:
        raise ValueError(
            f"неизвестный профиль источника '{profile_id}'. "
            f"Доступные профили: {list(_ADAPTERS.keys())}")
    return _ADAPTERS[profile_id]
