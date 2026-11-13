# -*- coding: utf-8 -*-
"""
console.py - ТЕКСТОВЫЙ ИНТЕРФЕЙС: вопросы пользователю, выбор из списка, постраничный показ.

Все "окошки" программы (меню, вопросы, подтверждения) строятся из нескольких простых
действий, и они собраны здесь. Остальные модули не вызывают input() и print() напрямую,
а пользуются объектом Console. Зачем так:
  * интерфейс можно потом заменить (например, на цветной или на curses), не трогая
    логику программы;
  * программу можно ПРОВЕРЯТЬ автоматически: вместо клавиатуры подставляется заранее
    заготовленный список ответов (см. Console(input_fn=...) и tests/).

Ctrl+D (конец ввода) и Ctrl+C завершают сеанс без ошибки: все изменения программа
сохраняет сразу, после каждого действия, поэтому ничего не теряется.
"""


class QuitSession(Exception):
    """Сигнал "пользователь закончил работу" (Ctrl+D). Ловится на самом верхнем уровне программы."""


def short(text, limit=160):
    """Первая непустая строка текста, обрезанная до limit символов (для показа в одной строке)."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line if len(line) <= limit else line[:limit].rstrip() + "…"
    return ""


class Console:
    """Вопросы и ответы через терминал.

    input_fn и print_fn по умолчанию настоящие input и print; для проверок сюда подставляют
    функции-заглушки.
    """

    WIDTH = 78  # ширина линий-разделителей

    def __init__(self, input_fn=input, print_fn=print):
        self._input = input_fn
        self._print = print_fn

    # ------------------------------------------------------------------ вывод

    def say(self, text=""):
        """Печатает строку (или несколько строк)."""
        self._print(text)

    def rule(self, char="─"):
        """Печатает горизонтальную линию-разделитель."""
        self._print(char * self.WIDTH)

    # ------------------------------------------------------------------ ввод

    def ask(self, prompt, default=None):
        """Задаёт вопрос и возвращает ответ без лишних пробелов.

        Пустой ответ заменяется на default (если он задан). При Ctrl+D бросает QuitSession.
        """
        try:
            raw = self._input(prompt)
        except EOFError:
            raise QuitSession()
        raw = raw.strip()
        if raw == "" and default is not None:
            return default
        return raw

    def confirm(self, question, default=False):
        """Вопрос "да/нет". Возвращает True или False. Пустой ответ = default."""
        hint = "[Д/н]" if default else "[д/Н]"
        while True:
            answer = self.ask(f"{question} {hint} ").lower()
            if answer == "":
                return default
            if answer in ("д", "да", "y", "yes"):
                return True
            if answer in ("н", "нет", "n", "no"):
                return False
            self.say("Ответьте «д» или «н».")

    def choose(self, title, options, cancel_text="отмена"):
        """Показывает пронумерованный список и просит выбрать номер.

        Возвращает индекс выбранного варианта (с нуля) или None, если пользователь
        выбрал отмену (пустой ввод или 0).
        """
        self.say(title)
        for i, option in enumerate(options, 1):
            self.say(f"  {i}. {option}")
        self.say(f"  0. {cancel_text}")
        while True:
            answer = self.ask("Номер: ")
            if answer in ("", "0"):
                return None
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return int(answer) - 1
            self.say(f"Введите число от 0 до {len(options)}.")

    def page(self, text, lines=28):
        """Показывает длинный текст порциями по lines строк; между порциями ждёт Enter.

        Ответ "q" прерывает показ.
        """
        rows = text.splitlines()
        for start in range(0, len(rows), lines):
            for row in rows[start:start + lines]:
                self.say(row)
            if start + lines < len(rows):
                if self.ask("── Enter: дальше, q: прервать ── ").lower() == "q":
                    return
