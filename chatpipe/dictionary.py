# -*- coding: utf-8 -*-
"""
dictionary.py - СЛОВАРЬ ПОНЯТИЙ (контролируемый словарь).

------------------------------------------------------------------------------
ЗАЧЕМ ОН НУЖЕН
------------------------------------------------------------------------------
Метка-якорь вроде it_coding_python собирается ТОЛЬКО из слов этого словаря. Так
метки не расходятся из-за опечаток ("administation") и споров об именовании
("coding" или "programming"?).

Главная идея: у каждого ПОНЯТИЯ есть неизменный идентификатор (например top.python)
и список слов-синонимов. Первое слово в списке - ПРЕДПОЧТИТЕЛЬНОЕ: именно оно
попадает в текст метки. Остальные синонимы нужны, чтобы РАСПОЗНАВАТЬ понятие в
тексте: нашли "программирование" - значит, речь о понятии "coding".

Скрипты ВСЕГДА работают с идентификаторами, а текст метки собирают из словаря в
момент показа. Поэтому предпочтительное слово можно переименовать (исправить
опечатку), и метки на всех карточках исправятся сами.

------------------------------------------------------------------------------
УРОВНИ
------------------------------------------------------------------------------
Понятия образуют дерево из трёх уровней (как в вашей схеме меток):
    direction     направление          (it, chemistry, biology ...)
    subdirection  поднаправление       (coding, system-administration ...)
    topic         конкретная тема      (python, docker, server-ubuntu ...)
У понятия есть родитель на уровень выше; метка - это путь от корня до понятия:
    top.python -> sub.coding -> dom.it     даёт метку   it_coding_python

------------------------------------------------------------------------------
ФОРМАТ ФАЙЛА (обычный текст, по строке на понятие; правится в Vim или через меню)
------------------------------------------------------------------------------
    id | уровень | родитель | слова через ";" (первое - предпочтительное)
    top.python | topic | sub.coding | python; питон

Поля разделяет символ "|", слова внутри четвёртого поля - символ ";". Строки,
начинающиеся с "#", - комментарии; пустые строки пропускаются.

------------------------------------------------------------------------------
ПРАВИЛА (программа проверяет их сама)
------------------------------------------------------------------------------
  * идентификатор уникален, состоит из латиницы в нижнем регистре, цифр, точки, дефиса;
  * предпочтительное слово "безопасно для тегов": латиница в нижнем регистре, цифры,
    дефис (без подчёркивания - оно разделяет части метки, и без символов вроде "+");
  * синонимы - любой текст (можно по-русски, можно с пробелами), но без "|" и ";";
  * одно слово не может принадлежать двум понятиям ОДНОГО уровня (иначе непонятно,
    какое имелось в виду);
  * родитель должен существовать и стоять ровно на уровень выше.
"""

import re                       # регулярные выражения
import os                       # os.replace - атомарная замена файла
from collections import Counter # словарь-счётчик
from pathlib import Path        # удобная работа с путями


# ==============================================================================
# ОБЩИЕ ПРАВИЛА
# ==============================================================================

# Три уровня понятий по порядку от общего к частному.
LEVELS = ("direction", "subdirection", "topic")

# Для каждого уровня - уровень, на котором обязан стоять родитель.
# У самого верхнего уровня (direction) родителя нет (None).
PARENT_LEVEL = {"direction": None, "subdirection": "direction", "topic": "subdirection"}

# Шаблон идентификатора: начинается с буквы/цифры, дальше буквы, цифры, точки, дефисы.
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]*$")

# Шаблон "безопасного для тегов" слова: куски из латиницы/цифр, соединённые одиночными
# дефисами. Примеры: python, server-ubuntu, cpp. Не подходят: c++, my_word, Python.
_TAG_WORD_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Шаблон "одного слова-токена": буквы/цифры, внутри допускаются дефисы. Именно такие
# синонимы ищутся в тексте по леммам. Всё остальное (с пробелами, "c++") ищется как
# фраза подстрокой.
_SINGLE_TOKEN_RE = re.compile(r"^[^\W_][\w\-]*$", re.UNICODE)


class DictionaryError(ValueError):
    """Ошибка словаря. В тексте перечислены ВСЕ найденные проблемы (с номерами строк)."""


def normalize_word(word):
    """Приводит слово к единому виду для сравнения: нижний регистр, "ё" -> "е", без лишних пробелов.

    Зачем: "Ёлка" и "елка", "Python" и "python" должны считаться одним словом.
    Эту же функцию используют и модули анализа текста, чтобы сравнивать "яблоки с яблоками".
    """
    return " ".join(word.strip().lower().replace("ё", "е").split())
    # .split() без аргументов режет по любым пробелам и выбрасывает пустые куски;
    # " ".join(...) склеивает обратно одинарными пробелами - так лишние пробелы исчезают.


# ==============================================================================
# ПОНЯТИЕ
# ==============================================================================

class Concept:
    """Одно понятие словаря: идентификатор, уровень, родитель и список слов."""

    def __init__(self, cid, level, parent, words):
        self.id = cid          # неизменный идентификатор, например "top.python"
        self.level = level     # "direction" / "subdirection" / "topic"
        self.parent = parent   # идентификатор родителя или None у верхнего уровня
        self.words = words     # список слов; words[0] - предпочтительное

    @property
    def preferred(self):
        """Предпочтительное слово (то, что попадает в метку).

        @property превращает метод в "вычисляемое поле": пишем concept.preferred
        без скобок, как обычный атрибут.
        """
        return self.words[0]

    @property
    def synonyms(self):
        """Остальные слова (всё, кроме предпочтительного)."""
        return self.words[1:]


# ==============================================================================
# РАСПОЗНАВАТЕЛЬ ПОНЯТИЙ В ТЕКСТЕ
# ==============================================================================

class ConceptMatcher:
    """Считает, сколько раз слова каждого понятия встретились в тексте карточки.

    Строится один раз на весь словарь (Dictionary.build_matcher), потом применяется
    к каждой карточке. Слова-синонимы из одного слова сравниваются по ЛЕММАМ
    (начальным формам): "докеры" и "докер" - одно слово. Синонимы из нескольких слов
    или со знаками ("c++", "информационные технологии") ищутся как фраза.
    """

    def __init__(self, token_map, phrases):
        self.token_map = token_map  # {лемма: [идентификаторы понятий]}
        self.phrases = phrases      # [(фраза_в_нижнем_регистре, идентификатор_понятия)]

    def count(self, lemma_counts, lower_text):
        """Возвращает Counter {идентификатор_понятия: число_попаданий} для одной карточки.

        lemma_counts - Counter лемм карточки; lower_text - её текст в нижнем регистре
        (для поиска фраз).
        """
        hits = Counter()
        # Перебираем слова словаря (их немного) и смотрим, сколько раз каждое
        # встретилось в карточке. Так быстрее, чем перебирать все слова карточки.
        for lemma, concept_ids in self.token_map.items():
            n = lemma_counts.get(lemma, 0)
            if n:
                for cid in concept_ids:
                    hits[cid] += n
        for phrase, cid in self.phrases:
            n = lower_text.count(phrase)  # str.count - сколько раз подстрока встретилась
            if n:
                hits[cid] += n
        return hits


# ==============================================================================
# СЛОВАРЬ
# ==============================================================================

class Dictionary:
    """Весь словарь понятий: хранение, поиск, правка, проверка, чтение и запись файла."""

    def __init__(self):
        # Обычный словарь Python помнит порядок добавления - это порядок файла.
        self.concepts = {}  # {идентификатор: Concept}

    # ------------------------------------------------------------------ чтение

    def get(self, cid):
        """Понятие по идентификатору или None."""
        return self.concepts.get(cid)

    def path(self, cid):
        """Идентификаторы от корня до понятия: ["dom.it", "sub.coding", "top.python"]."""
        chain = []
        current = self.concepts.get(cid)
        while current is not None:           # поднимаемся по родителям до корня
            chain.append(current.id)
            current = self.concepts.get(current.parent) if current.parent else None
        chain.reverse()                      # разворачиваем: от корня к понятию
        return chain

    def label(self, cid):
        """Метка-якорь для понятия: предпочтительные слова пути через "_" (it_coding_python)."""
        return "_".join(self.concepts[c].preferred for c in self.path(cid))

    def children(self, parent_id):
        """Список прямых потомков понятия (в порядке файла)."""
        return [c for c in self.concepts.values() if c.parent == parent_id]

    def at_level(self, level):
        """Все понятия заданного уровня."""
        return [c for c in self.concepts.values() if c.level == level]

    def find_by_word(self, word, level=None):
        """Понятия, у которых есть ТОЧНО такое слово (любой синоним). level - ограничить уровнем."""
        w = normalize_word(word)
        return [c for c in self.concepts.values()
                if (level is None or c.level == level) and w in c.words]

    def search(self, query):
        """Поиск понятий по запросу пользователя. Порядок: точные совпадения раньше неточных.

        Что считается совпадением: идентификатор, слово-синоним, метка целиком или
        начало/часть слова. Результаты без повторов.
        """
        q = normalize_word(query)
        if not q:
            return []
        exact, starts, inside = [], [], []
        for c in self.concepts.values():
            if q == c.id or q in c.words or q == self.label(c.id):
                exact.append(c)
            elif c.id.startswith(q) or any(w.startswith(q) for w in c.words):
                starts.append(c)
            elif q in c.id or any(q in w for w in c.words):
                inside.append(c)
        found, seen = [], set()
        for c in exact + starts + inside:
            if c.id not in seen:     # убираем повторы, сохраняя порядок
                seen.add(c.id)
                found.append(c)
        return found

    # ------------------------------------------------------------------ правка

    def _check_words(self, cid, level, words, ignore_id=None):
        """Проверяет список слов понятия. Возвращает список проблем (пустой = всё хорошо).

        ignore_id - идентификатор понятия, которое сейчас правим (его собственные слова
        не считаются конфликтом самим с собой).
        """
        problems = []
        if not words:
            return ["не указано ни одного слова"]
        if not _TAG_WORD_RE.match(words[0]):
            problems.append(
                f"предпочтительное слово '{words[0]}' должно состоять из латинских букв "
                f"в нижнем регистре, цифр и дефисов (например cpp вместо c++)")
        seen = set()
        for w in words:
            if not w:
                problems.append("пустое слово в списке")
            elif "|" in w or ";" in w:
                problems.append(f"слово '{w}': символы | и ; запрещены")
            if w in seen:
                problems.append(f"слово '{w}' повторяется")
            seen.add(w)
        # Слово не должно принадлежать другому понятию ТОГО ЖЕ уровня.
        for other in self.at_level(level):
            if other.id == ignore_id or other.id == cid:
                continue
            clash = set(words) & set(other.words)
            if clash:
                problems.append(
                    f"слово(а) {sorted(clash)} уже есть у понятия {other.id} того же уровня")
        return problems

    def add_concept(self, cid, level, parent, words):
        """Добавляет понятие. При нарушении правил бросает DictionaryError с описанием."""
        words = [normalize_word(w) for w in words]
        problems = []
        if not _ID_RE.match(cid or ""):
            problems.append(f"идентификатор '{cid}' некорректен (латиница в нижнем регистре, цифры, . и -)")
        if cid in self.concepts:
            problems.append(f"идентификатор '{cid}' уже занят")
        if level not in LEVELS:
            problems.append(f"уровень '{level}' неизвестен (допустимы: {', '.join(LEVELS)})")
        else:
            need = PARENT_LEVEL[level]
            if need is None:
                if parent not in (None, "-", ""):
                    problems.append("у понятия верхнего уровня (direction) не должно быть родителя")
                parent = None
            else:
                p = self.concepts.get(parent)
                if p is None:
                    problems.append(f"родитель '{parent}' не найден")
                elif p.level != need:
                    problems.append(f"родитель '{parent}' имеет уровень {p.level}, а нужен {need}")
            problems += self._check_words(cid, level, words)
        if problems:
            raise DictionaryError(f"понятие {cid}: " + "; ".join(problems))
        concept = Concept(cid, level, parent, words)
        self.concepts[cid] = concept
        return concept

    def add_synonym(self, cid, word):
        """Добавляет слово-синоним к понятию (в конец списка)."""
        concept = self.concepts[cid]
        new_words = concept.words + [normalize_word(word)]
        problems = self._check_words(cid, concept.level, new_words, ignore_id=cid)
        if problems:
            raise DictionaryError(f"понятие {cid}: " + "; ".join(problems))
        concept.words = new_words

    def set_preferred(self, cid, word):
        """Делает существующее слово предпочтительным (ставит его на первое место).

        Метка на всех карточках с этим понятием изменится сама: она собирается из
        словаря, а не хранится. Так исправляются опечатки и переименования.
        """
        concept = self.concepts[cid]
        w = normalize_word(word)
        if w not in concept.words:
            raise DictionaryError(f"слова '{w}' нет у понятия {cid}; сначала добавьте его как синоним")
        new_words = [w] + [x for x in concept.words if x != w]
        problems = self._check_words(cid, concept.level, new_words, ignore_id=cid)
        if problems:
            raise DictionaryError(f"понятие {cid}: " + "; ".join(problems))
        concept.words = new_words

    def problems(self):
        """Полная перепроверка словаря. Возвращает список проблем (пустой = словарь в порядке)."""
        result = []
        for c in self.concepts.values():
            if c.level in LEVELS:
                need = PARENT_LEVEL[c.level]
                p = self.concepts.get(c.parent) if c.parent else None
                if need is None and c.parent is not None:
                    result.append(f"{c.id}: у верхнего уровня не должно быть родителя")
                if need is not None and (p is None or p.level != need):
                    result.append(f"{c.id}: некорректный родитель {c.parent!r}")
            else:
                result.append(f"{c.id}: неизвестный уровень {c.level!r}")
            result += [f"{c.id}: {p}" for p in self._check_words(c.id, c.level, c.words, ignore_id=c.id)]
        return result

    # ------------------------------------------------------------------ распознавание

    def build_matcher(self, lemma):
        """Строит ConceptMatcher. lemma - функция "слово -> лемма" (см. keywords.Lemmatizer.lemma)."""
        token_map = {}   # {лемма: [идентификаторы понятий]}
        phrases = []     # [(фраза, идентификатор)]
        for c in self.concepts.values():
            for w in c.words:
                if _SINGLE_TOKEN_RE.match(w):
                    key = lemma(w)
                    ids = token_map.setdefault(key, [])  # setdefault: взять список или создать пустой
                    if c.id not in ids:
                        ids.append(c.id)
                else:
                    phrases.append((w, c.id))
        return ConceptMatcher(token_map, phrases)

    # ------------------------------------------------------------------ файл

    def to_text(self):
        """Собирает текст файла словаря (шапка с пояснениями + понятия по иерархии)."""
        # Порядок вывода: направление, затем его поднаправления, затем их темы.
        ordered = []
        def walk(parent_id):
            for c in self.children(parent_id):
                ordered.append(c)
                walk(c.id)  # рекурсия: после понятия сразу его потомки
        walk(None)
        # Понятия с "потерянным" родителем (если словарь повреждён) - в конец, чтобы не терять.
        known = {c.id for c in ordered}
        ordered += [c for c in self.concepts.values() if c.id not in known]

        rows = [(c.id, c.level, c.parent or "-", "; ".join(c.words)) for c in ordered]
        # Ширины столбцов - для ровной таблицы (для красоты; на разбор не влияет).
        w_id = max([len(r[0]) for r in rows] + [2])
        w_lv = max([len(r[1]) for r in rows] + [5])
        w_pa = max([len(r[2]) for r in rows] + [8])
        lines = [HEADER.rstrip("\n"), ""]
        for cid, level, parent, words in rows:
            lines.append(f"{cid:<{w_id}} | {level:<{w_lv}} | {parent:<{w_pa}} | {words}")
        return "\n".join(lines) + "\n"

    def save(self, path):
        """Сохраняет словарь в файл (атомарно: сначала временный файл, потом замена)."""
        path = Path(path)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(self.to_text(), encoding="utf-8")
        os.replace(tmp, path)

    @classmethod
    def from_text(cls, text):
        """Читает словарь из текста. При ошибках бросает DictionaryError со ВСЕМИ проблемами.

        @classmethod - метод класса: вызывается как Dictionary.from_text(...) и создаёт
        новый объект (cls - это сам класс Dictionary).
        """
        entries, problems = [], []
        for number, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue  # пустые строки и комментарии пропускаем
            parts = [p.strip() for p in line.split("|")]
            if len(parts) != 4:
                problems.append(f"строка {number}: нужно 4 поля через '|', а найдено {len(parts)}")
                continue
            cid, level, parent, words = parts
            entries.append((number, cid, level, parent, [w for w in words.split(";")]))

        # Родители должны быть добавлены раньше потомков, поэтому сортируем записи по
        # уровню (direction, subdirection, topic). sorted стабилен: порядок внутри
        # уровня сохраняется. Неизвестный уровень ставим в конец.
        def level_rank(entry):
            return LEVELS.index(entry[2]) if entry[2] in LEVELS else len(LEVELS)

        d = cls()
        for number, cid, level, parent, words in sorted(entries, key=level_rank):
            try:
                d.add_concept(cid, level, parent, words)
            except DictionaryError as e:
                problems.append(f"строка {number}: {e}")
        if problems:
            raise DictionaryError("\n".join(problems))
        return d

    @classmethod
    def load(cls, path):
        """Читает словарь из файла."""
        return cls.from_text(Path(path).read_text(encoding="utf-8"))


# ==============================================================================
# ШАПКА ФАЙЛА И НАЧАЛЬНЫЙ НАБОР ПОНЯТИЙ
# ==============================================================================

HEADER = """\
# =============================================================================
# chatpipe: СЛОВАРЬ ПОНЯТИЙ
# =============================================================================
# Метки-якоря собираются только из слов этого словаря.
#
# Формат строки (поля разделяет символ |):
#   id | уровень | родитель | слова через ; (первое - предпочтительное)
#
# Уровни: direction (направление), subdirection (поднаправление), topic (тема).
# Родитель - id понятия уровнем выше (у direction пишется -).
# Первое слово идёт в метку; остальные - синонимы, по ним понятие находят в тексте.
# Предпочтительное слово: латиница в нижнем регистре, цифры, дефис.
# Синонимы: любой текст (можно по-русски), но без символов | и ;
# Идентификатор не меняют НИКОГДА: на него ссылаются метки на карточках.
#
# Ниже - стартовый набор-пример. Правьте и дополняйте под себя.
# ============================================================================="""


def default_dictionary():
    """Возвращает словарь со стартовым набором понятий (пример, который пользователь правит под себя)."""
    d = Dictionary()
    d.add_concept("dom.it", "direction", None, ["it", "информационные технологии"])
    d.add_concept("sub.coding", "subdirection", "dom.it",
                  ["coding", "programming", "программирование", "кодинг"])
    d.add_concept("sub.system-administration", "subdirection", "dom.it",
                  ["system-administration", "sysadmin", "администрирование", "сисадмин"])
    d.add_concept("top.python", "topic", "sub.coding", ["python", "питон", "пайтон"])
    d.add_concept("top.bash", "topic", "sub.coding", ["bash", "баш"])
    d.add_concept("top.cpp", "topic", "sub.coding", ["cpp", "c++", "си-плюс-плюс"])
    d.add_concept("top.docker", "topic", "sub.system-administration", ["docker", "докер"])
    d.add_concept("top.server-ubuntu", "topic", "sub.system-administration",
                  ["server-ubuntu", "ubuntu", "убунту"])
    d.add_concept("top.postgresql", "topic", "sub.system-administration",
                  ["postgresql", "postgres", "постгрес"])
    return d
