#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_stage2.py - запуск этапа 2 (разметка карточек метками и сборка пачек для ИИ).

Это короткая точка входа: весь код лежит в пакете chatpipe/. Запускайте из папки проекта:
    python3 run_stage2.py              # меню
    python3 run_stage2.py --stats      # статистика
    python3 run_stage2.py --help       # все возможности
"""

import sys  # sys.exit - завершение программы с кодом результата

from chatpipe.stage2_label import main  # главная функция этапа 2

if __name__ == "__main__":
    # main() возвращает код результата (0 - успех); sys.exit передаёт его операционной системе.
    sys.exit(main())
