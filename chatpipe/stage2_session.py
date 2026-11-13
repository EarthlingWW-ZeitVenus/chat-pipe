# -*- coding: utf-8 -*-
"""
stage2_session.py - ЯДРО ЭТАПА 2: данные сеанса и действия над ними (без меню и вопросов).

Класс Session держит всё, с чем работает этап 2:
    карточки (из stage1)  +  словарь понятий  +  аннотации  +  правила  +  индекс слов
и умеет делать то, что нужно и меню, и командной строке:
    очереди карточек для разметки, статистика, применение правил, сборка пачек, справка.

Здесь НЕТ вопросов пользователю (input) - они живут в stage2_menus.py. Благодаря этому
то же самое можно запускать автоматически (например, раз в два месяца по расписанию)
без участия человека.

Этап 2 читает карточки только через schema.read_cards и ничего не знает о Takeout,
Claude или DeepSeek.
"""

from collections import Counter
from pathlib import Path

from .annotations import STATUSES, AnnotationStore
from .bundles import compose_bundles, format_summary
from .dictionary import Dictionary, default_dictionary
from .keywords import CorpusIndex, Lemmatizer, load_stopwords
from .names import (ANNOTATIONS_FILE, BUNDLES_REPORT_FILE, CARDS_FILE, DICTIONARY_FILE,
                    RULES_FILE, help_file_name)
from .rules import RuleEngine, RuleSet
from .schema import read_cards


# ==============================================================================
# ПУТИ К ФАЙЛАМ
# ==============================================================================

class Paths:
    """Все пути, нужные этапу 2, в одном объекте."""

    def __init__(self, cards, dictionary, annotations, rules, stopwords, user_data, bundles_dir):
        self.cards = cards                # карточки от stage1 (читаем)
        self.dictionary = dictionary      # словарь понятий (ваши данные)
        self.annotations = annotations    # метки и статусы (ваши данные)
        self.rules = rules                # правила меток (ваши данные)
        self.stopwords = stopwords        # файл "пустых" слов или None
        self.user_data = user_data        # папка ваших данных (там же лежат тексты справки)
        self.bundles_dir = bundles_dir    # куда писать пачки для ИИ


def resolve_paths(settings, cards_override=None):
    """Определяет пути по настройкам. Пустые (null) значения заменяются файлами по умолчанию.

    Приоритет для файла карточек: параметр командной строки > настройки > папка output.
    """
    from .names import BUNDLES_DIR
    user_data = Path(settings["paths"]["user_data"])
    output = Path(settings["paths"]["output"])
    s2 = settings["stage2"]
    return Paths(
        cards=Path(cards_override or s2["cards_file"] or output / CARDS_FILE),
        dictionary=Path(s2["dictionary_file"] or user_data / DICTIONARY_FILE),
        annotations=Path(s2["annotations_file"] or user_data / ANNOTATIONS_FILE),
        rules=Path(settings["anchor_rules_file"] or user_data / RULES_FILE),
        stopwords=Path(s2["stopwords_file"]) if s2["stopwords_file"] else None,
        user_data=user_data,
        bundles_dir=output / BUNDLES_DIR,
    )


# ==============================================================================
# СЕАНС
# ==============================================================================

class Session:
    """Данные сеанса этапа 2 и действия над ними."""

    def __init__(self, settings, paths, say=print):
        self.settings = settings
        self.paths = paths
        self.say = say                 # функция для сообщений о ходе работы
        self.cards = []                # карточки в хронологическом порядке
        self.cards_by_id = {}          # {id: карточка}
        self.card_ids = []             # идентификаторы в хронологическом порядке
        self.dictionary = None
        self.store = None
        self.rules = None
        self.warnings = []             # предупреждения, найденные при загрузке (показывает меню)
        # Эти три объекта строятся ЛЕНИВО (только когда реально нужны): построение индекса слов
        # занимает время, а для статистики или сборки пачек оно не требуется.
        self._lemmatizer = None
        self._index = None
        self._engine = None

    # ------------------------------------------------------------------ загрузка

    def load(self):
        """Читает карточки, словарь, аннотации и правила."""
        if not self.paths.cards.exists():
            raise FileNotFoundError(
                f"файл карточек не найден: {self.paths.cards}. Сначала запустите этап 1 "
                f"(python3 run_stage1.py ...) или укажите путь флагом --cards.")
        self.cards = list(read_cards(self.paths.cards))   # заодно проверка договора
        self.cards_by_id = {c["id"]: c for c in self.cards}
        self.card_ids = [c["id"] for c in self.cards]

        if self.paths.dictionary.exists():
            self.dictionary = Dictionary.load(self.paths.dictionary)
        else:
            # Первый запуск: создаём стартовый словарь-пример, чтобы было с чего начать.
            self.dictionary = default_dictionary()
            self.save_dictionary()
            self.warnings.append(
                f"Словарь не найден, создан стартовый набор-пример: {self.paths.dictionary}")

        self.store = AnnotationStore.load(self.paths.annotations)
        # Записи, которые адаптер счёл непонятными (meta.skip_reason), сразу получают skipped.
        auto_skipped = 0
        for card in self.cards:
            reason = card["meta"].get("skip_reason")
            if reason and self.store.auto_skip(card["id"], str(reason)):
                auto_skipped += 1
        if auto_skipped:
            self.save_annotations()
            self.warnings.append(
                f"{auto_skipped} непонятных записей автоматически помечены skipped "
                f"(причина записана в аннотациях).")
        self.rules = RuleSet.load(self.paths.rules)
        for rule in self.rules.rules:
            self.warnings += rule.check_against(self.dictionary)

        # Аннотации к карточкам, которых больше нет в файле карточек (например, после новой выгрузки).
        orphans = [cid for cid in self.store.records if cid not in self.cards_by_id]
        if orphans:
            self.warnings.append(
                f"{len(orphans)} аннотаций относятся к карточкам, которых нет в файле карточек "
                f"(они сохранены и ничему не мешают).")

    # ------------------------------------------------------------------ ленивые объекты

    @property
    def lemmatizer(self):
        """Лемматизатор (создаётся при первом обращении)."""
        if self._lemmatizer is None:
            self._lemmatizer = Lemmatizer(use_pymorphy=self.settings["stage2"]["use_lemmas"])
        return self._lemmatizer

    @property
    def index(self):
        """Индекс слов по всем карточкам (строится при первом обращении; это самое долгое действие)."""
        if self._index is None:
            self.say(f"Анализ слов в {len(self.cards)} карточках "
                     f"(режим лемматизации: {self.lemmatizer.mode})...")
            s2 = self.settings["stage2"]
            self._index = CorpusIndex.build(
                self.cards, self.lemmatizer, load_stopwords(self.paths.stopwords),
                prompt_weight=s2["prompt_weight"],
                progress=lambda done, total: self.say(f"  ...{done}/{total}"))
        return self._index

    @property
    def engine(self):
        """Движок правил (создаётся при первом обращении)."""
        if self._engine is None:
            self._engine = RuleEngine(self.index, self.dictionary, self.lemmatizer)
        return self._engine

    def dictionary_changed(self):
        """Сообщает сеансу, что словарь изменился (пересобирает распознаватель понятий)."""
        if self._engine is not None:
            self._engine.refresh()

    # ------------------------------------------------------------------ сохранение

    @staticmethod
    def _ensure_parent(path):
        """Создаёт папку для файла, если её ещё нет."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    def save_annotations(self):
        self._ensure_parent(self.paths.annotations)
        self.store.save(self.paths.annotations)

    def save_dictionary(self):
        self._ensure_parent(self.paths.dictionary)
        self.dictionary.save(self.paths.dictionary)

    def save_rules(self, path=None):
        target = Path(path) if path else self.paths.rules
        self._ensure_parent(target)
        self.rules.save(target)

    def status_counts(self):
        """Сколько карточек в каждом статусе (Counter)."""
        return self.store.status_counts(self.card_ids)

    # ------------------------------------------------------------------ очереди карточек

    def queue(self, kind, word=None):
        """Список идентификаторов карточек для работы (в хронологическом порядке).

        Виды очередей:
          unfinished   new + proposed: всё, что ещё не размечено человеком
          new          карточки, которых никто не трогал
          proposed     есть предложения правил, ждут вашего решения
          tagged       метки есть, флага "готово" нет
          review_auto  есть метки, поставленные правилами (проверить автоматику)
          word         необработанные карточки, где встречается слово (нужен параметр word)
        """
        status = self.store.status
        if kind == "unfinished":
            return [c for c in self.card_ids if status(c) in ("new", "proposed")]
        if kind == "new":
            return [c for c in self.card_ids if status(c) == "new"]
        if kind == "proposed":
            return [c for c in self.card_ids if status(c) == "proposed"]
        if kind == "tagged":
            return [c for c in self.card_ids if status(c) == "tagged"]
        if kind == "review_auto":
            machine = set(self.store.cards_with_machine_labels())
            return [c for c in self.card_ids if c in machine]
        if kind == "word":
            lemma = self.lemmatizer.lemma(word)
            feats = self.index.features
            return [c for c in self.card_ids
                    if status(c) in ("new", "proposed") and feats[c].counts.get(lemma, 0) > 0]
        raise ValueError(f"неизвестный вид очереди: {kind}")

    # ------------------------------------------------------------------ статистика

    def stats_text(self, with_words=False):
        """Текст статистики: статусы, использование понятий, правила. with_words добавляет частые слова необработанных."""
        counts = self.store.status_counts(self.card_ids)
        total = len(self.card_ids) or 1
        lines = [f"Карточек всего: {len(self.card_ids)}", "", "Статусы:"]
        names = {"new": "новые (без меток)", "proposed": "предложено правилами",
                 "tagged": "размечены, но не 'готово'", "ready": "готовы к компоновке",
                 "skipped": "пропущены"}
        for s in STATUSES:
            lines.append(f"  {s:<9} {counts[s]:>6}  ({100 * counts[s] / total:4.1f}%)  {names[s]}")

        usage = Counter()   # {id понятия: на скольких карточках стоит}
        for rec in self.store.records.values():
            for c in rec["concepts"]:
                usage[c["id"]] += 1
        lines += ["", f"Понятий в словаре: {len(self.dictionary.concepts)}, "
                      f"правил: {len(self.rules.rules)}"]
        if usage:
            lines.append("Метки чаще всего:")
            for cid, n in usage.most_common(10):
                label = self.dictionary.label(cid) if self.dictionary.get(cid) else f"{cid} (нет в словаре!)"
                lines.append(f"  {n:>5}  {label}")

        machine = len(self.store.cards_with_machine_labels())
        if machine:
            lines.append("")
            lines.append(f"Карточек с метками от правил: {machine} (очередь 'проверить автоматику')")

        if with_words:
            words = self.index.corpus_top_terms(self.queue("new"), n=15)
            if words:
                lines += ["", "Частые слова среди новых карточек:"]
                lines.append("  " + ", ".join(f"{w}({n})" for w, n in words))
        return "\n".join(lines)

    # ------------------------------------------------------------------ правила

    def apply_rules(self, rule_ids=None):
        """Применяет правила (все или перечисленные) к карточкам и сохраняет аннотации.

        Возвращает {id_правила: {"matched": N, "eligible": N, "changed": N}}.
        """
        results = {}
        for rule in self.rules.rules:
            if rule_ids is not None and rule.id not in rule_ids:
                continue
            results[rule.id] = self.engine.apply(rule, self.card_ids, self.store)
        self.save_annotations()
        return results

    # ------------------------------------------------------------------ пачки

    def compose(self, statuses=("ready",), max_chars=None):
        """Собирает пачки для ИИ и пишет отчёт. Возвращает текст сводки."""
        if max_chars is None:
            max_chars = self.settings["limits"]["bundle_max_chars"]
        summaries, skipped = compose_bundles(
            self.cards, self.store, self.dictionary, self.paths.bundles_dir, max_chars, statuses)
        text = format_summary(summaries, skipped, max_chars)
        self.paths.bundles_dir.mkdir(parents=True, exist_ok=True)
        (self.paths.bundles_dir / BUNDLES_REPORT_FILE).write_text(text + "\n", encoding="utf-8")
        return text

    # ------------------------------------------------------------------ справка

    def help_text(self, topic, default_text):
        """Текст справки по теме. Подробный текст пользователь кладёт в файл (см. names.help_file_name).

        Если файла нет, показывается краткая встроенная справка с подсказкой, куда положить свою.
        """
        path = self.paths.user_data / help_file_name(topic)
        if path.exists():
            return path.read_text(encoding="utf-8")
        return (default_text.rstrip() + "\n\n(Подробная справка не задана. Чтобы добавить свою, "
                f"положите текст в файл {path})")
