# -*- coding: utf-8 -*-
"""
@@HELP-BEGIN module=compat
compat.py - ПРОВЕРКА СОВМЕСТИМОСТИ: библиотеки и процессор.

ЧТО ДЕЛАЕТ
  В начале каждой стадии проверяет в три шага:
  1. НАЛИЧИЕ. Нужные библиотеки установлены и реально работают. Мало импортировать
     pymorphy3: нужно создать MorphAnalyzer() и разобрать пробное слово (ему нужны
     словари из пакета pymorphy3-dicts-ru).
  2. СОВМЕСТИМОСТЬ С ПРОЦЕССОРОМ. Каждая библиотека пробуется в ОТДЕЛЬНОМ процессе.
     Если процессор не понимает какую-то инструкцию, процесс падает с сигналом SIGILL
     ("недопустимая инструкция"). Внутри самой программы такое падение поймать нельзя
     (она упала бы вместе с библиотекой), поэтому проба идёт в дочернем процессе, а
     основная программа читает только его код завершения.
  3. СВЕДЕНИЯ О ПРОЦЕССОРЕ (только Linux). Модель и важные флаги из /proc/cpuinfo.
     Это справка для человека, а не запрет.

ЗАЧЕМ
  Раньше при отсутствии pymorphy3 программа молча переходила в "простой режим", и
  результат становился хуже без предупреждения. Теперь при проблеме программа
  ОСТАНАВЛИВАЕТСЯ и печатает понятную причину и инструкцию для Ubuntu.

ЧТО ЗДЕСЬ НЕ ДЕЛАЕТСЯ
  Библиотеки не заменяются собственным кодом. Собственная замена на "родных" командах
  пишется только когда конкретная библиотека реально не работает на конкретной машине.
  Для этого в списке LIBRARIES есть поле "fallback" (сейчас везде None).

КТО ВЫЗЫВАЕТ
  stage1_ingest.main и stage2_label.main - в начале работы стадии.

ГЛАВНАЯ ФУНКЦИЯ
  ensure_compatibility(use_lemmas, write) -> True (работаем) или False (остановиться).
@@HELP-END
"""

import os
import signal
import subprocess
import sys


# ==============================================================================
# СПИСОК БИБЛИОТЕК, КОТОРЫЕ ПРОВЕРЯЕМ
# ==============================================================================
# Каждая запись:
#   key / title    короткое имя и название для сообщений;
#   pip            что установить (строка для команды pip install);
#   smoke          код пробы: выполняется в ОТДЕЛЬНОМ процессе Python;
#   expect         что проба должна напечатать (или None - не проверять вывод);
#   needed_if_lemmas   проверять только если лемматизация включена в настройках;
#   fallback       заготовка для собственной замены (сейчас нет).
LIBRARIES = [
    {
        "key": "pymorphy3",
        "title": "pymorphy3 (начальные формы русских слов)",
        "pip": "pymorphy3 pymorphy3-dicts-ru",
        "smoke": ("import pymorphy3; "
                  "print(pymorphy3.MorphAnalyzer().parse('делали')[0].normal_form)"),
        "expect": "делать",
        "needed_if_lemmas": True,
        "fallback": None,
    },
]

STATUS_OK, STATUS_MISSING, STATUS_BROKEN, STATUS_CPU = "ok", "missing", "broken", "cpu"
STATUS_TEXT = {
    STATUS_OK: "работает",
    STATUS_MISSING: "не установлена",
    STATUS_BROKEN: "установлена, но не работает",
    STATUS_CPU: "не совместима с процессором",
}

# Флаги процессора, которые полезно показать человеку (от старых к новым).
CPU_FLAGS_OF_INTEREST = ("sse2", "ssse3", "sse4_1", "sse4_2", "popcnt", "avx", "avx2")


# ==============================================================================
# ШАГ 1-2. ПРОБА БИБЛИОТЕКИ В ОТДЕЛЬНОМ ПРОЦЕССЕ
# ==============================================================================

def _no_core_dumps():
    """Выполняется в дочернем процессе: запрещает файлы core dump (мусор при падении)."""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:
        pass


def check_library(lib, timeout=120):
    """Пробует библиотеку в отдельном процессе. Возвращает пару (статус, пояснение).

    Статусы: "ok", "missing" (нет библиотеки), "broken" (есть, но не работает),
    "cpu" (процесс упал с SIGILL: процессор не понимает инструкцию).
    """
    kwargs = {}
    if os.name == "posix":
        kwargs["preexec_fn"] = _no_core_dumps
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        done = subprocess.run(
            [sys.executable, "-c", lib["smoke"]], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=env, **kwargs)
    except subprocess.TimeoutExpired:
        return STATUS_BROKEN, f"проба не закончилась за {timeout} с"

    sigill = getattr(signal, "SIGILL", None)
    if sigill is not None and done.returncode == -int(sigill):
        return STATUS_CPU, "процесс упал с сигналом SIGILL (процессор не понимает инструкцию)"
    if done.returncode != 0:
        lines = done.stderr.strip().splitlines()
        last = lines[-1] if lines else "(без текста ошибки)"
        if "ModuleNotFoundError" in done.stderr or "ImportError" in done.stderr:
            return STATUS_MISSING, last
        return STATUS_BROKEN, last
    printed = done.stdout.strip()
    if lib.get("expect") and printed != lib["expect"]:
        return STATUS_BROKEN, f"ожидался результат {lib['expect']!r}, получено {printed!r}"
    return STATUS_OK, "работает"


# ==============================================================================
# ШАГ 3. СВЕДЕНИЯ О ПРОЦЕССОРЕ
# ==============================================================================

def read_cpu_info(path="/proc/cpuinfo"):
    """Читает модель и флаги процессора. Возвращает {"model", "flags"} или None (не Linux)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    model, flags = "", set()
    for line in text.splitlines():
        key, _sep, value = line.partition(":")
        key = key.strip()
        if key == "model name" and not model:
            model = value.strip()
        elif key == "flags" and not flags:
            flags = set(value.split())
    return {"model": model or "неизвестно", "flags": flags}


def describe_cpu(info):
    """Строки для печати: модель и наличие важных инструкций."""
    if info is None:
        return ["  Процессор: сведения недоступны (нет /proc/cpuinfo)"]
    marks = ", ".join(f"{name} {'да' if name in info['flags'] else 'нет'}"
                      for name in CPU_FLAGS_OF_INTEREST)
    return [f"  Процессор: {info['model']}", f"  Инструкции: {marks}"]


# ==============================================================================
# СООБЩЕНИЯ И ГЛАВНАЯ ФУНКЦИЯ
# ==============================================================================

def explain_problem(lib, status, detail):
    """Текст остановки: причина и что делать."""
    lines = ["", "=" * 70,
             f"ОСТАНОВКА: проблема с библиотекой «{lib['title']}».",
             f"Состояние: {STATUS_TEXT[status]}", f"Подробности: {detail}", ""]
    if status == STATUS_CPU:
        lines += [
            "Процессор не понимает инструкцию, которую использует библиотека.",
            "Что можно сделать:",
            "  1) поставить более старую версию библиотеки, собранную без этой инструкции;",
            "  2) отключить функцию: в файле настроек, раздел stage2, \"use_lemmas\": false;",
            "  3) сообщить об этом: для такой машины нужна собственная замена библиотеки.",
        ]
    else:
        lines += [
            f"Сейчас программа запущена этим Python: {sys.executable}",
            "",
            "Как установить на Ubuntu (через виртуальное окружение):",
            "  1) sudo apt update && sudo apt install -y python3-venv python3-pip",
            "  2) python3 -m venv ~/venv-chatpipe     (или используйте свой venv)",
            "  3) source ~/venv-chatpipe/bin/activate",
            f"  4) pip install {lib['pip']}",
            "  5) запускайте программу из этого окружения (в строке виден его значок)",
            "",
            "Если пакеты уже стоят, а ошибка осталась - запущен другой Python.",
            f"Проверьте так:  {sys.executable} -m pip list",
            "",
            "Осознанно запустить без лемматизации: в файле настроек, раздел stage2,",
            "поставьте  \"use_lemmas\": false",
        ]
    lines.append("=" * 70)
    return "\n".join(lines)


def ensure_compatibility(use_lemmas=True, write=None):
    """Проверяет библиотеки и процессор. True - можно работать; False - остановиться.

    use_lemmas - включена ли лемматизация в настройках (если нет, pymorphy3 не проверяем).
    write      - функция для сообщений (по умолчанию печать в stderr).
    """
    write = write or (lambda text: print(text, file=sys.stderr))
    write(f"Проверка совместимости. Python: {sys.executable}")
    for line in describe_cpu(read_cpu_info()):
        write(line)

    problems = []
    for lib in LIBRARIES:
        if lib.get("needed_if_lemmas") and not use_lemmas:
            write(f"  {lib['title']}: пропущено (лемматизация отключена в настройках)")
            continue
        status, detail = check_library(lib)
        if status == STATUS_OK:
            write(f"  {lib['title']}: ОК")
        else:
            # Здесь место для собственной замены: если у библиотеки есть lib["fallback"],
            # можно подключить её вместо остановки. Сейчас замен нет.
            problems.append((lib, status, detail))

    for lib, status, detail in problems:
        write(explain_problem(lib, status, detail))
    return not problems
