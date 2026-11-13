# -*- coding: utf-8 -*-
"""
rules.py - ПРАВИЛА МЕТОК: логические выражения над словами, по которым автоматика
находит карточки и предлагает (или сама ставит) им метки.

------------------------------------------------------------------------------
ЯЗЫК ВЫРАЖЕНИЙ
------------------------------------------------------------------------------
Выражение - это слова и понятия, соединённые связками and / or / not и скобками:

    (docker or докер) and not kubernetes
    @top.python >= 3
    (#1 or #2) and #3          <- номера слов из списка в меню (см. expand_numbered)

Что можно писать:
    слово               встречается в карточке (по начальной форме: "докеры" = "докер")
    слово >= N          встречается не меньше N раз (допустимы также >, <=, <, ==)
    @id_понятия         хотя бы одно слово этого понятия словаря встречается в карточке
    @id_понятия >= N    слова понятия встретились суммарно не меньше N раз
    A and B             выполнено и A, и B
    A or B              выполнено A или B (или оба)
    not A               A НЕ выполнено
    ( ... )             скобки задают порядок; без скобок "and" связывает сильнее, чем "or"

Слова and, or, not зарезервированы (регистр неважен). Блоки кода в подсчёте слов
не участвуют (как и везде в программе).

------------------------------------------------------------------------------
ПОЧЕМУ НЕ eval
------------------------------------------------------------------------------
В Python есть функция eval, которая выполняет любой текст как код. Давать ей
выражение пользователя нельзя: она хрупка и небезопасна (выполнит что угодно).
Вместо этого здесь написан маленький "разборщик" (парсер): он понимает ТОЛЬКО то,
что перечислено выше. Способ называется "рекурсивный спуск": на каждое правило
грамматики - одна функция, и функции вызывают друг друга по уровням приоритета:
    or  ->  and  ->  not  ->  атом (слово или выражение в скобках)

------------------------------------------------------------------------------
ФАЙЛ ПРАВИЛ (обычный текст, по строке на правило; поля разделяет "|")
------------------------------------------------------------------------------
    id | режим | понятия через запятую | выражение
    r001 | suggest | top.docker | (docker or докер) and not kubernetes

Режимы:
    suggest      только ПРЕДЛАГАТЬ: карточки получают статус proposed, решает человек
    apply        САМО ставить метку (статус tagged); подтверждение человека не нужно
    apply-ready  ставить метку И флаг "готово" (для правил, которым вы полностью доверяете)
Автоматика не трогает карточки, с которыми уже работал человек (см. annotations.py).
"""

import os                       # os.replace - атомарная замена файла
import re                       # регулярные выражения
from pathlib import Path        # пути

from .dictionary import normalize_word


# ==============================================================================
# ОБЩЕЕ
# ==============================================================================

MODES = ("suggest", "apply", "apply-ready")


class RuleSyntaxError(ValueError):
    """Ошибка в выражении или в файле правил (текст ошибки понятен пользователю)."""


# ==============================================================================
# 1. РАЗБИЕНИЕ ВЫРАЖЕНИЯ НА "ЖЕТОНЫ" (токены)
# ==============================================================================
# Сначала строка превращается в список жетонов: слова, скобки, операторы сравнения.
# Шаблон по частям (после необязательных пробелов \s* ровно ОДНА из альтернатив):
#   (>=|<=|==|>|<)       оператор сравнения       -> группа 1
#   (\()                 открывающая скобка       -> группа 2
#   (\))                 закрывающая скобка       -> группа 3
#   ([^\s()<>=]+)        слово: всё, кроме пробелов, скобок и знаков сравнения -> группа 4
_TOKEN_RE = re.compile(r"\s*(?:(>=|<=|==|>|<)|(\()|(\))|([^\s()<>=]+))")


def tokenize_expression(text):
    """Режет выражение на жетоны: список троек (вид, значение, позиция).

    Виды: OP (сравнение), LP "(", RP ")", AND, OR, NOT, WORD (слово или @понятие).
    """
    tokens = []
    pos = 0
    text = text.rstrip()
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)  # match - совпадение строго с позиции pos
        if not m or m.end() == pos:
            # Шаблон не подошёл. Пропускаем пробелы, чтобы в сообщении назвать именно
            # "плохой" символ, а не пробел перед ним.
            bad = pos
            while bad < len(text) and text[bad].isspace():
                bad += 1
            raise RuleSyntaxError(f"неожиданный символ {text[bad:bad + 1]!r} в позиции {bad + 1}")
        start = m.start() + (len(m.group(0)) - len(m.group(0).lstrip()))  # позиция после пробелов
        op, lp, rp, word = m.groups()
        if op:
            tokens.append(("OP", op, start))
        elif lp:
            tokens.append(("LP", "(", start))
        elif rp:
            tokens.append(("RP", ")", start))
        else:
            low = word.lower()
            if low in ("and", "or", "not"):
                tokens.append((low.upper(), low, start))   # зарезервированные слова
            else:
                tokens.append(("WORD", word, start))
        pos = m.end()
    return tokens


# ==============================================================================
# 2. РАЗБОР ЖЕТОНОВ В ДЕРЕВО (рекурсивный спуск)
# ==============================================================================
# Результат - дерево из кортежей:
#   ("or",  [узел, узел, ...])
#   ("and", [узел, узел, ...])
#   ("not", узел)
#   ("term", вид, ключ, оператор, число)    вид: "word" или "concept"

class _Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0  # какой жетон разбираем сейчас

    def _peek(self):
        """Текущий жетон (не сдвигая позицию) или None, если жетоны кончились."""
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _next(self):
        """Берёт текущий жетон и сдвигается к следующему."""
        tok = self._peek()
        self.pos += 1
        return tok

    def parse(self):
        if not self.tokens:
            raise RuleSyntaxError("выражение пустое")
        node = self._or()
        extra = self._peek()
        if extra is not None:  # после полного выражения остались лишние жетоны
            raise RuleSyntaxError(f"лишнее {extra[1]!r} в позиции {extra[2] + 1}")
        return node

    # Каждая функция ниже разбирает свой уровень приоритета.
    def _or(self):
        nodes = [self._and()]
        while self._peek() and self._peek()[0] == "OR":
            self._next()
            nodes.append(self._and())
        return nodes[0] if len(nodes) == 1 else ("or", nodes)

    def _and(self):
        nodes = [self._not()]
        while self._peek() and self._peek()[0] == "AND":
            self._next()
            nodes.append(self._not())
        return nodes[0] if len(nodes) == 1 else ("and", nodes)

    def _not(self):
        if self._peek() and self._peek()[0] == "NOT":
            self._next()
            return ("not", self._not())   # not not A тоже допустимо
        return self._atom()

    def _atom(self):
        tok = self._next()
        if tok is None:
            raise RuleSyntaxError("выражение оборвалось: ожидалось слово или '('")
        kind, value, position = tok
        if kind == "LP":
            node = self._or()              # внутри скобок - снова полное выражение
            closing = self._next()
            if closing is None or closing[0] != "RP":
                raise RuleSyntaxError("не закрыта скобка '('")
            return node
        if kind == "WORD":
            return self._term(value, position)
        raise RuleSyntaxError(f"неожиданное {value!r} в позиции {position + 1}: ожидалось слово или '('")

    def _term(self, word, position):
        """Слово или @понятие с необязательным сравнением (>= число)."""
        if word.startswith("@"):
            kind, key = "concept", word[1:].lower()
            if not key:
                raise RuleSyntaxError(f"после '@' в позиции {position + 1} нужен идентификатор понятия")
        else:
            kind, key = "word", normalize_word(word)
        op, number = ">=", 1               # по умолчанию: "встречается хотя бы раз"
        nxt = self._peek()
        if nxt and nxt[0] == "OP":
            self._next()
            op = nxt[1]
            num_tok = self._next()
            if num_tok is None or num_tok[0] != "WORD" or not num_tok[1].isdigit():
                raise RuleSyntaxError(f"после {op!r} нужно целое число")
            number = int(num_tok[1])
        return ("term", kind, key, op, number)


def parse_expression(text):
    """Разбирает текст выражения в дерево. Бросает RuleSyntaxError с понятным пояснением."""
    return _Parser(tokenize_expression(text)).parse()


# ==============================================================================
# 3. ВЫЧИСЛЕНИЕ ВЫРАЖЕНИЯ ДЛЯ КАРТОЧКИ
# ==============================================================================

class MatchContext:
    """Всё, что нужно знать о карточке, чтобы проверить выражение: сколько раз встретились слова и понятия."""

    def __init__(self, lemma_counts, concept_counts, lemmatizer):
        self.lemma_counts = lemma_counts      # Counter {лемма: сколько раз}
        self.concept_counts = concept_counts  # Counter {id понятия: сколько попаданий}
        self.lemmatizer = lemmatizer

    def count(self, kind, key):
        """Сколько раз встретилось слово (kind == "word") или понятие (kind == "concept")."""
        if kind == "concept":
            return self.concept_counts.get(key, 0)
        return self.lemma_counts.get(self.lemmatizer.lemma(key), 0)


def _compare(value, op, number):
    """Сравнение value <op> number для операторов >=, >, <=, <, ==."""
    if op == ">=":
        return value >= number
    if op == ">":
        return value > number
    if op == "<=":
        return value <= number
    if op == "<":
        return value < number
    return value == number


def evaluate(node, ctx):
    """Вычисляет дерево выражения для карточки. Возвращает True или False."""
    kind = node[0]
    if kind == "or":
        return any(evaluate(n, ctx) for n in node[1])    # any - "хотя бы одно истинно"
    if kind == "and":
        return all(evaluate(n, ctx) for n in node[1])    # all - "все истинны"
    if kind == "not":
        return not evaluate(node[1], ctx)
    _, term_kind, key, op, number = node                 # узел-слово
    return _compare(ctx.count(term_kind, key), op, number)


def terms_of(node):
    """Все слова и понятия выражения: список пар (вид, ключ). Нужен для проверки по словарю."""
    if node[0] in ("or", "and"):
        return [t for n in node[1] for t in terms_of(n)]
    if node[0] == "not":
        return terms_of(node[1])
    return [(node[1], node[2])]


def explain(node):
    """Читаемое пояснение выражения по-русски: "(слово «docker» ИЛИ слово «докер») И НЕ слово «kubernetes»"."""
    kind = node[0]
    if kind in ("or", "and"):
        joiner = " ИЛИ " if kind == "or" else " И "
        return "(" + joiner.join(explain(n) for n in node[1]) + ")"
    if kind == "not":
        return "НЕ " + explain(node[1])
    _, term_kind, key, op, number = node
    name = f"понятие «{key}»" if term_kind == "concept" else f"слово «{key}»"
    return name if (op == ">=" and number == 1) else f"{name} {op} {number}"


def expand_numbered(expression, words):
    """Заменяет в выражении ссылки вида #N на N-е слово списка (нумерация с 1).

    Это то самое "пользователь видит слова с номерами и пишет (#1 or #2) and #3". В файл
    правил попадают уже САМИ СЛОВА: сохранённое правило не зависит от того, как
    пронумеровали слова в тот день.
    """
    def replace(m):
        n = int(m.group(1))
        if not 1 <= n <= len(words):
            raise RuleSyntaxError(f"в списке нет слова #{n} (в списке слов: {len(words)})")
        return words[n - 1]
    return re.sub(r"#(\d+)", replace, expression)


# ==============================================================================
# 4. ПРАВИЛО И НАБОР ПРАВИЛ
# ==============================================================================

class Rule:
    """Одно правило: идентификатор, режим, понятия для метки и выражение."""

    def __init__(self, rule_id, mode, concept_ids, expression):
        if mode not in MODES:
            raise RuleSyntaxError(f"правило {rule_id}: режим {mode!r} неизвестен (допустимы: {', '.join(MODES)})")
        if not concept_ids:
            raise RuleSyntaxError(f"правило {rule_id}: не указано ни одного понятия для метки")
        self.id = rule_id
        self.mode = mode
        self.concept_ids = list(concept_ids)
        self.expression = expression.strip()
        try:
            self.tree = parse_expression(self.expression)
        except RuleSyntaxError as e:
            raise RuleSyntaxError(f"правило {rule_id}: {e}") from e

    def check_against(self, dictionary):
        """Проверяет, что все понятия правила (и @понятия в выражении) есть в словаре. Возвращает список проблем."""
        problems = []
        for cid in self.concept_ids:
            if dictionary.get(cid) is None:
                problems.append(f"правило {self.id}: понятия {cid!r} нет в словаре")
        for kind, key in terms_of(self.tree):
            if kind == "concept" and dictionary.get(key) is None:
                problems.append(f"правило {self.id}: в выражении @{key} - такого понятия нет в словаре")
        return problems


_RULES_HEADER = """\
# =============================================================================
# chatpipe: ПРАВИЛА МЕТОК
# =============================================================================
# Формат строки (поля разделяет символ |):
#   id | режим | понятия через запятую | выражение
#
# Режимы: suggest (только предлагать), apply (само ставить метку),
#         apply-ready (ставить метку и флаг "готово").
# Выражение: слова и @понятия, связки and / or / not, скобки, сравнения (>= 3).
# Примеры:
#   r001 | suggest | top.docker | (docker or докер) and not kubernetes
#   r002 | apply   | top.python | @top.python >= 3
# Автоматика не трогает карточки, с которыми уже работал человек.
# ============================================================================="""


class RuleSet:
    """Набор правил: хранение, правка, чтение и запись файла."""

    def __init__(self):
        self.rules = []  # список Rule в порядке файла

    def get(self, rule_id):
        for r in self.rules:
            if r.id == rule_id:
                return r
        return None

    def next_id(self):
        """Свободный идентификатор вида r001, r002 ..."""
        used = {r.id for r in self.rules}
        n = 1
        while f"r{n:03d}" in used:   # {n:03d} - число, дополненное нулями до трёх знаков
            n += 1
        return f"r{n:03d}"

    def add(self, rule):
        if self.get(rule.id):
            raise RuleSyntaxError(f"правило {rule.id} уже есть")
        self.rules.append(rule)

    def remove(self, rule_id):
        before = len(self.rules)
        self.rules = [r for r in self.rules if r.id != rule_id]
        return len(self.rules) != before

    def to_text(self):
        lines = [_RULES_HEADER, ""]
        for r in self.rules:
            lines.append(f"{r.id} | {r.mode} | {', '.join(r.concept_ids)} | {r.expression}")
        return "\n".join(lines) + "\n"

    def save(self, path):
        """Сохраняет правила в файл (атомарно)."""
        path = Path(path)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(self.to_text(), encoding="utf-8")
        os.replace(tmp, path)

    @classmethod
    def from_text(cls, text):
        """Читает правила из текста. Бросает RuleSyntaxError со ВСЕМИ найденными проблемами."""
        rs, problems = cls(), []
        for number, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # split("|", 3): режем не больше чем на 4 части - всё после третьего "|"
            # остаётся выражением целиком.
            parts = [p.strip() for p in line.split("|", 3)]
            if len(parts) != 4:
                problems.append(f"строка {number}: нужно 4 поля через '|'")
                continue
            rule_id, mode, concepts, expression = parts
            try:
                ids = [c.strip() for c in concepts.split(",") if c.strip()]
                rs.add(Rule(rule_id, mode, ids, expression))
            except RuleSyntaxError as e:
                problems.append(f"строка {number}: {e}")
        if problems:
            raise RuleSyntaxError("\n".join(problems))
        return rs

    @classmethod
    def load(cls, path):
        """Читает правила из файла. Если файла нет - возвращает пустой набор."""
        path = Path(path)
        if not path.exists():
            return cls()
        return cls.from_text(path.read_text(encoding="utf-8"))


# ==============================================================================
# 5. ДВИЖОК: ПРИМЕНЕНИЕ ПРАВИЛ К КАРТОЧКАМ
# ==============================================================================

class RuleEngine:
    """Проверяет правила на карточках и применяет их к хранилищу аннотаций.

    index       CorpusIndex (слова карточек, см. keywords.py);
    dictionary  Dictionary (понятия);
    lemmatizer  Lemmatizer.
    """

    def __init__(self, index, dictionary, lemmatizer):
        self.index = index
        self.dictionary = dictionary
        self.lemmatizer = lemmatizer
        self.matcher = None
        self._contexts = {}   # кэш {card_id: MatchContext}
        self.refresh()

    def refresh(self):
        """Пересобирает распознаватель понятий. Вызывать после правки словаря."""
        self.matcher = self.dictionary.build_matcher(self.lemmatizer.lemma)
        self._contexts = {}

    def context(self, card_id):
        """MatchContext карточки (считается один раз, потом берётся из кэша)."""
        ctx = self._contexts.get(card_id)
        if ctx is None:
            feats = self.index.features[card_id]
            concept_counts = self.matcher.count(feats.counts, feats.lower_text)
            ctx = MatchContext(feats.counts, concept_counts, self.lemmatizer)
            self._contexts[card_id] = ctx
        return ctx

    def concept_hits(self, card_id):
        """Counter {id понятия: попаданий} для карточки (подсказки словаря)."""
        return self.context(card_id).concept_counts

    def matching(self, rule, card_ids):
        """Идентификаторы карточек из card_ids, подходящие под правило."""
        return [cid for cid in card_ids
                if cid in self.index.features and evaluate(rule.tree, self.context(cid))]

    def preview(self, rule, card_ids, store):
        """Пробный прогон БЕЗ изменений: что нашло бы правило.

        Возвращает словарь:
          matched   все подходящие карточки;
          eligible  из них те, которые автоматика вправе менять (человек их не трогал).
        """
        matched = self.matching(rule, card_ids)
        eligible = [cid for cid in matched if store.automation_may_touch(cid)]
        return {"matched": matched, "eligible": eligible}

    def apply(self, rule, card_ids, store):
        """Применяет правило согласно его режиму. Возвращает {"matched": N, "eligible": N, "changed": N}."""
        result = self.preview(rule, card_ids, store)
        changed = 0
        for cid in result["eligible"]:
            touched = False
            for concept_id in rule.concept_ids:
                if rule.mode == "suggest":
                    touched |= store.propose(cid, concept_id, rule.id)
                else:
                    touched |= store.apply_by_rule(
                        cid, concept_id, rule.id, ready=(rule.mode == "apply-ready"))
            changed += 1 if touched else 0
        return {"matched": len(result["matched"]), "eligible": len(result["eligible"]),
                "changed": changed}

    def apply_all(self, rules, card_ids, store):
        """Применяет все правила по очереди. Возвращает {id_правила: итог}."""
        return {r.id: self.apply(r, card_ids, store) for r in rules.rules}
