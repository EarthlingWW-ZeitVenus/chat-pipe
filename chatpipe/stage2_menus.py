# -*- coding: utf-8 -*-
"""
stage2_menus.py - ТЕРМИНАЛЬНЫЕ МЕНЮ ЭТАПА 2.

Главное меню:
  1. Разметка карточек вручную  - просмотр карточки, выборка предложений со словом, метки, флаг "готово"
  2. Правила меток              - выражения, пробный прогон, применение, откат, Save/Load
  3. Словарь понятий            - дерево, новые понятия и синонимы, смена предпочтительного слова
  4. Собрать пачки для ИИ (XML)
  5. Статистика
  ?  Справка (подробные тексты вы кладёте в файлы, см. names.help_file_name)

Здесь только "разговор с пользователем". Всё, что делается с данными, лежит в
stage2_session.py (Session), stage2-логика меток - в annotations.py, rules.py и т.д.
Каждое изменение СОХРАНЯЕТСЯ СРАЗУ: можно закрыть программу в любой момент.
"""

from .annotations import AnnotationError
from .console import QuitSession, short
from .dictionary import LEVELS, PARENT_LEVEL, DictionaryError, normalize_word
from .keywords import sentences_with
from .rules import (MODES, Rule, RuleSet, RuleSyntaxError, explain, expand_numbered,
                    parse_expression)

# ==============================================================================
# КРАТКИЕ ВСТРОЕННЫЕ СПРАВКИ (подробные тексты пользователь кладёт в файлы)
# ==============================================================================

HELP_MAIN = """\
Этап 2: разметка карточек метками-якорями и сборка пачек для ИИ.
Порядок работы: 1) пополните словарь (3), 2) создайте правила (2) или размечайте вручную (1),
3) ставьте флаг "готово" карточкам, 4) соберите пачки (4).
Метка-якорь - это путь понятий словаря, например it_coding_python.
Все изменения сохраняются сразу."""

HELP_LABELING = """\
Команды в режиме разметки (вводятся после знака >):
  Enter, n      следующая карточка        p      предыдущая        g N   перейти к карточке N
  w N | слово   предложения со словом (N - номер слова из списка ключевых слов)
  v             показать карточку целиком (по страницам)
  l текст       поставить метку: слово, id или часть названия; несколько - через запятую
  lf текст      поставить метку и флаг "готово", перейти дальше
  x [N]         снять метку (N - номер метки из строки "Метки")
  c             подтвердить предложения правил (метки становятся вашими)
  f             флаг "готово к компоновке" (нужна хотя бы одна метка)
  u             снять флаг "готово"
  s             пропустить карточку (не нужна)       z   стереть всю разметку карточки
  q             выйти в главное меню
Если нужного понятия нет в словаре, программа предложит его создать."""

HELP_RULES = """\
Правило = выражение + понятия для метки + режим.
Выражение: слова и @понятия, связки and / or / not, скобки, сравнения, например:
  (docker or докер) and not kubernetes        @top.python >= 3
В меню показан список ключевых слов с номерами - можно писать #1, #2: (#1 or #2) and #3.
Режимы: suggest - только предлагать (статус proposed); apply - сама ставить метку;
        apply-ready - метка и флаг "готово". Автоматика не трогает карточки, с которыми вы уже работали.
Перед сохранением программа показывает, сколько карточек подходит. Правило можно откатить."""

HELP_DICTIONARY = """\
Словарь: понятия трёх уровней (направление, поднаправление, тема), у каждого неизменный id
и список слов. Первое слово - предпочтительное, оно идёт в метку; остальные - синонимы,
по ним понятие находят в тексте. Предпочтительное слово: латиница, цифры, дефис (cpp вместо c++).
Файл словаря можно править и вручную (формат описан в его шапке)."""


class Menus:
    """Все меню этапа 2. Работает с Session (данные) и Console (ввод-вывод)."""

    def __init__(self, session, console):
        self.s = session
        self.c = console
        self.shown_terms = []   # слова, показанные на экране карточки (для команды "w N")
        self.rule_words = []    # слова, показанные в меню правил (для ссылок #N)

    # ==========================================================================
    # ГЛАВНОЕ МЕНЮ
    # ==========================================================================

    def run(self):
        """Главный цикл. Завершается выбором выхода (всё к этому моменту уже сохранено)."""
        c = self.c
        for warning in self.s.warnings:
            c.say(f"ВНИМАНИЕ: {warning}")
        actions = {"1": self.menu_labeling, "2": self.menu_rules, "3": self.menu_dictionary,
                   "4": self.menu_bundles, "5": self.menu_stats, "?": self.menu_help}
        while True:
            self.show_header()
            answer = c.ask("Выбор: ").lower()
            if answer in ("0", "q", "в", "выход"):
                c.say("Все изменения сохранены. До встречи!")
                return
            action = actions.get(answer)
            if action:
                action()
            else:
                c.say("Не понял. Введите номер пункта или ? для справки.")

    def show_header(self):
        c = self.c
        counts = self.s.status_counts()
        c.say()
        c.rule("═")
        c.say("chatpipe, этап 2: разметка карточек")
        c.say(f"Карточек: {len(self.s.card_ids)} | new: {counts['new']} | proposed: {counts['proposed']} | "
              f"tagged: {counts['tagged']} | ready: {counts['ready']} | skipped: {counts['skipped']}")
        c.rule("═")
        c.say(" 1. Разметка карточек вручную")
        c.say(" 2. Правила меток (автоматика)")
        c.say(" 3. Словарь понятий")
        c.say(" 4. Собрать пачки для ИИ (XML)")
        c.say(" 5. Статистика")
        c.say(" ?. Справка")
        c.say(" 0. Выход")

    def menu_help(self):
        self.c.page(self.s.help_text("main", HELP_MAIN))

    def menu_stats(self):
        self.c.say(self.s.stats_text(with_words=True))

    # ==========================================================================
    # ПОДБОР ПОНЯТИЙ (общие помощники)
    # ==========================================================================

    def describe_concept(self, concept):
        """Одна строка о понятии: id -> метка [слова]."""
        return f"{concept.id} -> {self.s.dictionary.label(concept.id)}  [{'; '.join(concept.words)}]"

    def resolve_concept(self, query):
        """Превращает ввод пользователя в идентификатор понятия (или None).

        Ищет в словаре по id, слову, метке и части слова. Если вариантов несколько, просит
        выбрать; если ничего нет, предлагает создать новое понятие.
        """
        d, c = self.s.dictionary, self.c
        query = query.strip()
        if not query:
            return None
        found = d.search(query)
        if not found:
            if c.confirm(f"Понятия «{query}» нет в словаре. Создать?", default=False):
                return self.create_concept_flow(prefill=query)
            return None
        # Точное совпадение (id, слово или целая метка) выбираем сразу, без вопросов.
        norm = normalize_word(query)
        exact = [x for x in found if norm == x.id or norm in x.words or norm == d.label(x.id)]
        if len(exact) == 1:
            return exact[0].id
        if len(found) == 1:
            return found[0].id
        idx = c.choose(f"Найдено несколько понятий для «{query}»:",
                       [self.describe_concept(x) for x in found])
        return found[idx].id if idx is not None else None

    def create_concept_flow(self, prefill=None):
        """Пошаговое создание понятия. Возвращает идентификатор созданного понятия или None."""
        d, c = self.s.dictionary, self.c
        level_idx = c.choose("Уровень нового понятия:", [
            "direction (направление: it, chemistry...)",
            "subdirection (поднаправление: coding, system-administration...)",
            "topic (тема: python, docker...)"])
        if level_idx is None:
            return None
        level = LEVELS[level_idx]
        parent = None
        need = PARENT_LEVEL[level]
        if need:
            parents = d.at_level(need)
            if not parents:
                c.say(f"Сначала создайте понятие уровня {need}: у нового понятия должен быть родитель.")
                return None
            p_idx = c.choose(f"Родитель (уровень {need}):",
                             [f"{p.id} -> {d.label(p.id)}" for p in parents])
            if p_idx is None:
                return None
            parent = parents[p_idx].id
        default_words = prefill.lower() if prefill and not prefill.isspace() else None
        raw = c.ask("Слова через ';' (первое - предпочтительное, латиница/цифры/дефис): ",
                    default=default_words)
        if not raw:
            return None
        words = [w for w in raw.split(";")]
        prefix = {"direction": "dom", "subdirection": "sub", "topic": "top"}[level]
        suggested_id = f"{prefix}.{normalize_word(words[0])}"
        cid = c.ask(f"Идентификатор (неизменный!) [{suggested_id}]: ", default=suggested_id)
        try:
            concept = d.add_concept(cid, level, parent, words)
        except DictionaryError as e:
            c.say(f"Не получилось: {e}")
            return None
        self.s.save_dictionary()
        self.s.dictionary_changed()
        c.say(f"Создано: {self.describe_concept(concept)}")
        return concept.id

    def pick_concept(self, prompt="Понятие (слово, id или часть названия): "):
        """Находит понятие по вводу, БЕЗ создания нового. Возвращает Concept или None."""
        c, d = self.c, self.s.dictionary
        query = c.ask(prompt)
        if not query:
            return None
        found = d.search(query)
        if not found:
            c.say("Ничего не найдено.")
            return None
        if len(found) == 1:
            return found[0]
        idx = c.choose("Найдено несколько:", [self.describe_concept(x) for x in found])
        return found[idx] if idx is not None else None

    # ==========================================================================
    # 1. РАЗМЕТКА КАРТОЧЕК ВРУЧНУЮ
    # ==========================================================================

    def menu_labeling(self):
        """Выбор очереди карточек и запуск режима разметки."""
        c, s = self.c, self.s
        n = {k: len(s.queue(k)) for k in ("unfinished", "proposed", "new", "tagged", "review_auto")}
        idx = c.choose("Какие карточки показывать?", [
            f"Все необработанные (new + proposed): {n['unfinished']}",
            f"Только предложенные правилами (proposed): {n['proposed']}",
            f"Только новые (new): {n['new']}",
            f"С метками, но без флага 'готово' (tagged): {n['tagged']}",
            f"Проверить метки, поставленные правилами: {n['review_auto']}",
            "Необработанные со словом…"], cancel_text="назад")
        if idx is None:
            return
        if idx == 5:
            word = c.ask("Слово (любая форма): ")
            if not word:
                return
            queue = s.queue("word", word=word)
        else:
            queue = s.queue(["unfinished", "proposed", "new", "tagged", "review_auto"][idx])
        if not queue:
            c.say("В этой очереди карточек нет.")
            return
        c.say(f"В очереди карточек: {len(queue)}. Команды: ? - справка, Enter - следующая, q - выход.")
        self.label_loop(queue)

    def label_loop(self, queue):
        """Показывает карточки очереди по одной и выполняет команды пользователя."""
        c = self.c
        pos = 0
        while 0 <= pos < len(queue):
            card_id = queue[pos]
            self.show_card(card_id, pos + 1, len(queue))
            raw = c.ask("> ")
            # partition делит строку на три части по первому пробелу: команда, пробел, остаток.
            verb, _space, arg = raw.partition(" ")
            try:
                pos = self.handle_card_command(card_id, verb.lower(), arg.strip(), pos, len(queue))
            except (AnnotationError, DictionaryError, ValueError) as e:
                c.say(f"Ошибка: {e}")
            if pos is None:
                return
        c.say("Очередь закончилась.")

    def handle_card_command(self, card_id, verb, arg, pos, total):
        """Выполняет одну команду над карточкой. Возвращает новую позицию в очереди или None (выйти)."""
        s, c = self.s, self.c
        if verb in ("", "n"):
            return pos + 1
        if verb == "p":
            return max(pos - 1, 0)
        if verb == "q":
            return None
        if verb == "g":
            if arg.isdigit() and 1 <= int(arg) <= total:
                return int(arg) - 1
            c.say(f"Укажите номер карточки от 1 до {total}: g 12")
            return pos
        if verb in ("?", "h"):
            c.page(s.help_text("labeling", HELP_LABELING))
            return pos
        if verb == "v":
            self.show_full(card_id)
            return pos
        if verb == "w":
            self.show_sentences(card_id, arg)
            return pos
        if verb in ("l", "lf"):
            added = self.add_labels(card_id, arg)
            if added and verb == "lf":
                self.mark_ready_and_report(card_id)
                return pos + 1
            return pos
        if verb == "x":
            self.remove_label(card_id, arg)
            return pos
        if verb == "c":
            s.store.confirm(card_id)
            s.save_annotations()
            c.say("  предложения подтверждены, метки теперь ваши")
            return pos
        if verb == "f":
            if not s.store.concept_ids(card_id):
                c.say("Сначала поставьте метку (команда l).")
                return pos
            self.mark_ready_and_report(card_id)
            return pos + 1
        if verb == "u":
            s.store.unmark_ready(card_id)
            s.save_annotations()
            c.say("  флаг 'готово' снят")
            return pos
        if verb == "s":
            s.store.skip(card_id)
            s.save_annotations()
            c.say("  карточка пропущена")
            return pos + 1
        if verb == "z":
            if c.confirm("Стереть всю разметку этой карточки?", default=False):
                s.store.reset(card_id)
                s.save_annotations()
                c.say("  разметка стёрта")
            return pos
        c.say("Не понял команду. ? - справка.")
        return pos

    def mark_ready_and_report(self, card_id):
        """Ставит флаг "готово", сохраняет и сообщает, сколько машинных меток при этом подтверждено."""
        confirmed = self.s.store.mark_ready(card_id)
        self.s.save_annotations()
        note = f" (подтверждено меток от правил: {confirmed})" if confirmed else ""
        self.c.say("  флаг 'готово' поставлен" + note)

    # ------------------------------------------------------------------ показ карточки

    def describe_labels(self, card_id):
        """Строка "1) it_coding_python [вы]  2) ..." с метками карточки."""
        d = self.s.dictionary
        parts = []
        for i, concept in enumerate(self.s.store.concepts_of(card_id), 1):
            label = d.label(concept["id"]) if d.get(concept["id"]) else f"{concept['id']} (нет в словаре!)"
            by = {"user": "вы", "ai": "ИИ"}.get(concept["by"], f"правило {concept.get('rule')}")
            parts.append(f"{i}) {label} [{by}]")
        return "  ".join(parts)

    def show_card(self, card_id, number, total):
        """Печатает сводку карточки: время, запрос, метки, подсказки словаря и ключевые слова."""
        s, c = self.s, self.c
        card = s.cards_by_id[card_id]
        c.say()
        c.rule("═")
        c.say(f"Карточка {number} из {total}   id: {card_id}   статус: {s.store.status(card_id)}")
        when = card["ts"][:16].replace("T", " ") if card["ts"] else "время неизвестно"
        c.say(f"Время: {when}   ответ: {len(card['answer_md']):,} симв.")
        c.say("Запрос: " + short(card["prompt"], 200))
        c.say("Метки: " + (self.describe_labels(card_id) or "(нет)"))

        hits = s.engine.concept_hits(card_id).most_common(5)
        if hits:
            c.say("Подсказки словаря: " + ", ".join(
                f"{s.dictionary.label(cid)}({n})" for cid, n in hits))

        terms = s.index.top_terms(card_id, n=s.settings["stage2"]["keywords_shown"])
        self.shown_terms = [word for word, _count, _score in terms]
        if terms:
            c.say("Ключевые слова: " + "  ".join(
                f"{i}:{word}({count})" for i, (word, count, _score) in enumerate(terms, 1)))
        c.say("[Enter]далее [w N]предложения [v]целиком [l]метка [lf]метка+готово [f]готово [s]пропуск [?]помощь [q]выход")

    def show_full(self, card_id):
        """Показывает запрос и ответ карточки целиком, по страницам."""
        card = self.s.cards_by_id[card_id]
        text = "ЗАПРОС:\n" + card["prompt"] + "\n\nОТВЕТ:\n" + card["answer_md"]
        if card.get("service_md"):
            text += "\n\nСЛУЖЕБНЫЙ БЛОК:\n" + card["service_md"]
        self.c.page(text)

    def show_sentences(self, card_id, arg):
        """Показывает предложения карточки со словом (с соседними). arg - номер слова из списка или само слово."""
        c, s = self.c, self.s
        if not arg:
            arg = c.ask("Номер слова из списка или само слово: ")
        if not arg:
            return
        if arg.isdigit():
            n = int(arg)
            if not 1 <= n <= len(self.shown_terms):
                c.say(f"В списке нет слова №{n}.")
                return
            word = self.shown_terms[n - 1]
        else:
            word = arg
        result = sentences_with(s.cards_by_id[card_id], word, s.lemmatizer,
                                window=s.settings["stage2"]["sentence_window"])
        if not result["excerpts"]:
            c.say(f"Слово «{word}» в тексте (вне блоков кода) не найдено.")
            return
        c.say(f"Слово «{word}»: предложений {result['total']}; выдержки с контекстом:")
        for i, excerpt in enumerate(result["excerpts"], 1):
            c.say(f"  [{i}] {excerpt}")

    # ------------------------------------------------------------------ метки

    def add_labels(self, card_id, arg):
        """Ставит метки (можно несколько через запятую). Возвращает True, если что-то добавлено."""
        s, c = self.s, self.c
        if not arg:
            arg = c.ask("Метка (слово, id или часть названия; несколько - через запятую): ")
        if not arg:
            return False
        added = 0
        for part in arg.split(","):
            concept_id = self.resolve_concept(part)
            if concept_id:
                s.store.user_add(card_id, concept_id)
                c.say(f"  + {s.dictionary.label(concept_id)}")
                added += 1
        if added:
            s.save_annotations()
        return added > 0

    def remove_label(self, card_id, arg):
        """Снимает метку: по номеру из строки "Метки" или выбором из списка."""
        s, c = self.s, self.c
        labels = s.store.concepts_of(card_id)
        if not labels:
            c.say("У карточки нет меток.")
            return
        if arg.isdigit() and 1 <= int(arg) <= len(labels):
            target = labels[int(arg) - 1]["id"]
        elif len(labels) == 1 and not arg:
            target = labels[0]["id"]
        else:
            idx = c.choose("Какую метку снять?", [
                s.dictionary.label(x["id"]) if s.dictionary.get(x["id"]) else x["id"] for x in labels])
            if idx is None:
                return
            target = labels[idx]["id"]
        s.store.user_remove(card_id, target)
        s.save_annotations()
        c.say("  метка снята")

    # ==========================================================================
    # 2. ПРАВИЛА МЕТОК
    # ==========================================================================

    def menu_rules(self):
        c = self.c
        actions = {"1": self.rules_list, "2": self.rules_add, "3": self.rules_test,
                   "4": self.rules_apply, "5": self.rules_rollback, "6": self.rules_delete,
                   "7": self.rules_save, "8": self.rules_load}
        while True:
            c.say()
            c.rule()
            c.say(f"Правила меток (правил: {len(self.s.rules.rules)})")
            c.say(" 1. Список правил")
            c.say(" 2. Добавить правило")
            c.say(" 3. Пробный прогон правила (ничего не меняет)")
            c.say(" 4. Применить правило / все правила")
            c.say(" 5. Откатить правило (убрать его неподтверждённые метки)")
            c.say(" 6. Удалить правило")
            c.say(" 7. Сохранить правила в файл (Save)")
            c.say(" 8. Загрузить правила из файла (Load)")
            c.say(" ?. Справка")
            c.say(" 0. Назад")
            answer = c.ask("Выбор: ").lower()
            if answer in ("0", "q", ""):
                return
            if answer == "?":
                c.page(self.s.help_text("rules", HELP_RULES))
                continue
            action = actions.get(answer)
            if action:
                action()
            else:
                c.say("Не понял.")

    def pick_rule(self, title="Какое правило?"):
        rules = self.s.rules.rules
        if not rules:
            self.c.say("Правил пока нет.")
            return None
        idx = self.c.choose(title, [f"{r.id} [{r.mode}] {r.expression}" for r in rules], cancel_text="назад")
        return rules[idx] if idx is not None else None

    def example_lines(self, card_ids, n=5):
        """Строки-примеры "время  «первая строка запроса»" для первых n карточек."""
        lines = []
        for cid in card_ids[:n]:
            card = self.s.cards_by_id[cid]
            when = card["ts"][:16].replace("T", " ") if card["ts"] else "?"
            lines.append(f"    {when}  «{short(card['prompt'], 90)}»")
        return lines

    def rules_list(self):
        s, c = self.s, self.c
        if not s.rules.rules:
            c.say("Правил пока нет.")
            return
        for r in s.rules.rules:
            labels = ", ".join(s.dictionary.label(x) if s.dictionary.get(x) else f"{x}?" for x in r.concept_ids)
            matched = len(s.engine.matching(r, s.card_ids))
            c.say(f"{r.id} [{r.mode}] -> {labels}")
            c.say(f"    {r.expression}")
            c.say(f"    подходит карточек: {matched}")

    def print_numbered_words(self, pairs):
        """Печатает слова с номерами в три колонки: " 1 docker(140)   2 compose(98) ..."."""
        cells = [f"{i:>2} {word}({count})" for i, (word, count) in enumerate(pairs, 1)]
        for start in range(0, len(cells), 3):
            self.c.say("  " + "".join(cell.ljust(26) for cell in cells[start:start + 3]))

    def rules_add(self):
        """Пошаговое создание правила: выражение, понятия, режим, пробный прогон, сохранение."""
        s, c = self.s, self.c
        pairs = s.index.corpus_top_terms(s.queue("unfinished"), n=40)
        self.rule_words = [word for word, _count in pairs]
        if pairs:
            c.say("Ключевые слова необработанных карточек (в скобках - в скольких карточках встречается):")
            self.print_numbered_words(pairs)
        c.say("Выражение: слова и @понятия, and/or/not, скобки, сравнения (docker >= 3). Номера слов: #1, #2.")
        while True:
            text = c.ask("Выражение (пусто - отмена): ")
            if not text:
                return
            try:
                text = expand_numbered(text, self.rule_words)
                tree = parse_expression(text)
            except RuleSyntaxError as e:
                c.say(f"Ошибка: {e}")
                continue
            c.say("Понял так: " + explain(tree))
            break

        concept_ids = []
        while not concept_ids:
            raw = c.ask("Какие понятия поставить (слова или id через запятую; пусто - отмена): ")
            if not raw:
                return
            concept_ids = [x for x in (self.resolve_concept(p) for p in raw.split(",")) if x]

        mode_idx = c.choose("Режим правила:", [
            "suggest: только предлагать (карточки получат статус proposed, решаете вы)",
            "apply: само ставить метку (статус tagged)",
            "apply-ready: ставить метку и флаг 'готово' (только для правил, которым вы доверяете)"])
        if mode_idx is None:
            return

        rule = Rule(s.rules.next_id(), MODES[mode_idx], concept_ids, text)
        preview = s.engine.preview(rule, s.card_ids, s.store)
        c.say(f"Найдено карточек: {len(preview['matched'])}, из них доступно автоматике: "
              f"{len(preview['eligible'])}")
        for line in self.example_lines(preview["matched"]):
            c.say(line)
        if c.confirm(f"Сохранить правило {rule.id}?", default=True):
            s.rules.add(rule)
            s.save_rules()
            c.say(f"Правило {rule.id} сохранено. Чтобы применить его, выберите пункт 4.")

    def rules_test(self):
        s, c = self.s, self.c
        rule = self.pick_rule("Для какого правила сделать пробный прогон?")
        if not rule:
            return
        preview = s.engine.preview(rule, s.card_ids, s.store)
        c.say(f"{rule.id}: подходит {len(preview['matched'])}, автоматике доступно "
              f"{len(preview['eligible'])} (остальные уже тронуты вами или пропущены).")
        for line in self.example_lines(preview["matched"], n=8):
            c.say(line)

    def rules_apply(self):
        s, c = self.s, self.c
        if not s.rules.rules:
            c.say("Правил пока нет.")
            return
        idx = c.choose("Что применить?", ["Все правила"] + [f"{r.id} [{r.mode}] {r.expression}"
                                                            for r in s.rules.rules], cancel_text="назад")
        if idx is None:
            return
        chosen = s.rules.rules if idx == 0 else [s.rules.rules[idx - 1]]
        for r in chosen:
            pv = s.engine.preview(r, s.card_ids, s.store)
            c.say(f"  {r.id} [{r.mode}]: подходит {len(pv['matched'])}, будет затронуто {len(pv['eligible'])}")
        if not c.confirm("Применить?", default=False):
            return
        results = s.apply_rules([r.id for r in chosen])
        for rule_id, res in results.items():
            c.say(f"  {rule_id}: изменено карточек {res['changed']}")
        c.say("Готово. Метки сохранены. Откатить действие правила можно пунктом 5.")

    def rules_rollback(self):
        s, c = self.s, self.c
        rule = self.pick_rule("Правило, метки которого убрать?")
        if not rule:
            return
        count = sum(1 for rec in s.store.records.values() for x in rec["concepts"]
                    if x["by"] == "rule" and x.get("rule") == rule.id)
        if not count:
            c.say("Неподтверждённых меток этого правила нет.")
            return
        if c.confirm(f"Убрать {count} меток правила {rule.id}? (подтверждённые вами останутся)", default=False):
            removed = s.store.remove_by_rule(rule.id)
            s.save_annotations()
            c.say(f"Убрано меток: {removed}")

    def rules_delete(self):
        s, c = self.s, self.c
        rule = self.pick_rule("Какое правило удалить?")
        if rule and c.confirm(f"Удалить правило {rule.id}? (уже поставленные им метки останутся)", default=False):
            s.rules.remove(rule.id)
            s.save_rules()
            c.say("Удалено.")

    def rules_save(self):
        s, c = self.s, self.c
        path = c.ask(f"Куда сохранить [Enter = {s.paths.rules}]: ", default=str(s.paths.rules))
        s.save_rules(path)
        c.say(f"Сохранено: {path}")

    def rules_load(self):
        s, c = self.s, self.c
        path = c.ask(f"Откуда загрузить [Enter = {s.paths.rules}]: ", default=str(s.paths.rules))
        from pathlib import Path
        if not Path(path).exists():
            c.say("Такого файла нет.")
            return
        try:
            loaded = RuleSet.load(path)
        except RuleSyntaxError as e:
            c.say(f"Файл правил содержит ошибки:\n{e}")
            return
        problems = [p for r in loaded.rules for p in r.check_against(s.dictionary)]
        for p in problems:
            c.say(f"  ВНИМАНИЕ: {p}")
        if c.confirm(f"Заменить текущие правила ({len(s.rules.rules)}) загруженными ({len(loaded.rules)})?",
                     default=False):
            s.rules = loaded
            s.save_rules()
            c.say("Загружено.")

    # ==========================================================================
    # 3. СЛОВАРЬ
    # ==========================================================================

    def menu_dictionary(self):
        c = self.c
        while True:
            c.say()
            c.rule()
            c.say(f"Словарь понятий (понятий: {len(self.s.dictionary.concepts)})")
            c.say(" 1. Показать словарь")
            c.say(" 2. Добавить понятие")
            c.say(" 3. Добавить синоним понятию")
            c.say(" 4. Сменить предпочтительное слово")
            c.say(" 5. Проверить словарь")
            c.say(" ?. Справка")
            c.say(" 0. Назад")
            answer = c.ask("Выбор: ").lower()
            if answer in ("0", "q", ""):
                return
            if answer == "1":
                self.dictionary_show()
            elif answer == "2":
                self.create_concept_flow()
            elif answer == "3":
                self.dictionary_add_synonym()
            elif answer == "4":
                self.dictionary_set_preferred()
            elif answer == "5":
                problems = self.s.dictionary.problems()
                c.say("Словарь в порядке." if not problems else "Проблемы:\n  " + "\n  ".join(problems))
            elif answer == "?":
                c.page(self.s.help_text("dictionary", HELP_DICTIONARY))
            else:
                c.say("Не понял.")

    def dictionary_show(self):
        """Печатает словарь деревом: отступ показывает уровень понятия."""
        d = self.s.dictionary
        lines = []

        def walk(parent_id, depth):
            for concept in d.children(parent_id):
                lines.append("  " * depth + f"{concept.id}  ->  {d.label(concept.id)}  [{'; '.join(concept.words)}]")
                walk(concept.id, depth + 1)   # рекурсия: сразу после понятия - его потомки

        walk(None, 0)
        self.c.page("\n".join(lines) if lines else "Словарь пуст.")

    def dictionary_add_synonym(self):
        concept = self.pick_concept()
        if not concept:
            return
        word = self.c.ask(f"Новое слово для {concept.id} ({'; '.join(concept.words)}): ")
        if not word:
            return
        try:
            self.s.dictionary.add_synonym(concept.id, word)
        except DictionaryError as e:
            self.c.say(f"Не получилось: {e}")
            return
        self.s.save_dictionary()
        self.s.dictionary_changed()
        self.c.say("Добавлено.")

    def dictionary_set_preferred(self):
        concept = self.pick_concept()
        if not concept:
            return
        idx = self.c.choose(f"Какое слово сделать предпочтительным для {concept.id}?", concept.words)
        if idx is None:
            return
        try:
            self.s.dictionary.set_preferred(concept.id, concept.words[idx])
        except DictionaryError as e:
            self.c.say(f"Не получилось: {e}")
            return
        self.s.save_dictionary()
        self.s.dictionary_changed()
        self.c.say(f"Теперь метка: {self.s.dictionary.label(concept.id)} (изменилась на всех карточках сразу)")

    # ==========================================================================
    # 4. ПАЧКИ ДЛЯ ИИ
    # ==========================================================================

    def menu_bundles(self):
        s, c = self.s, self.c
        counts = s.status_counts()
        idx = c.choose("Из каких карточек собирать пачки?", [
            f"Только готовые (ready): {counts['ready']}",
            f"Готовые и размеченные (ready + tagged): {counts['ready'] + counts['tagged']}"],
            cancel_text="назад")
        if idx is None:
            return
        statuses = [("ready",), ("ready", "tagged")][idx]
        default_limit = s.settings["limits"]["bundle_max_chars"]
        raw = c.ask(f"Максимальный размер пачки в символах [{default_limit}]: ", default=str(default_limit))
        if not raw.isdigit() or int(raw) < 1000:
            c.say("Нужно целое число не меньше 1000.")
            return
        c.say(f"Пачки будут записаны в {s.paths.bundles_dir}. Старые файлы пачек там будут заменены.")
        if not c.confirm("Собрать?", default=True):
            return
        c.say(s.compose(statuses=statuses, max_chars=int(raw)))
