# -*- coding: utf-8 -*-
"""
keywords.py - АНАЛИЗ СЛОВ: какие слова значимы для карточки и в каких предложениях они стоят.

Что здесь есть:
  Lemmatizer      приводит слова к начальной форме ("докеры" -> "докер")
  load_stopwords  список "пустых" слов (предлоги, союзы...), которые не показываем как значимые
  CorpusIndex     считает слова во ВСЕХ карточках и выбирает значимые для каждой (метод BM25)
  sentences_with  выборка предложений со словом (плюс соседние) - для просмотра в меню

------------------------------------------------------------------------------
КАК ВЫБИРАЮТСЯ ЗНАЧИМЫЕ СЛОВА
------------------------------------------------------------------------------
Слово значимо для карточки, если оно ЧАСТО встречается в ней, но РЕДКО в остальных
карточках. Слово "docker", которое есть в половине чатов, ничего не отличает; слово
"openrouter", которое часто повторяется в трёх карточках, - отличает. Это идея
TF-IDF; мы используем её усовершенствованный вариант BM25, который учитывает ещё и то,
что карточки бывают разной длины (от пары КБ до сотен).

Важные особенности:
  * блоки кода в подсчёте НЕ участвуют (имена переменных и логи портят статистику);
  * слова из ЗАПРОСА считаются с двойным весом (prompt_weight): запрос лучше всего
    говорит, о чём карточка;
  * слово показывается как значимое, только если оно встретилось в карточке хотя бы
    дважды или стоит в запросе (одиночные случайные слова - обычно мусор).

------------------------------------------------------------------------------
pymorphy3
------------------------------------------------------------------------------
Для русских слов леммы даёт библиотека pymorphy3 (она у вас уже установлена:
pip install pymorphy3 pymorphy3-dicts-ru). Если её нет, программа НЕ падает, а
работает в простом режиме: слова сравниваются как есть (в нижнем регистре). Режим
виден в Lemmatizer.mode - его показывает меню.
"""

import math                      # логарифм для BM25
import re                        # регулярные выражения
from collections import Counter  # словарь-счётчик
from pathlib import Path         # пути

from .dictionary import normalize_word  # единая нормализация слов
from .text_tools import MARK_RE, strip_code      # убрать блоки кода из Markdown


# ==============================================================================
# ЛЕММАТИЗАЦИЯ
# ==============================================================================

class Lemmatizer:
    """Приводит слово к начальной форме. Результаты запоминает (кэш), чтобы не считать дважды."""

    def __init__(self, use_pymorphy=True):
        self.mode = "plain"   # "plain" - слова как есть; "pymorphy3" - настоящие леммы
        self._morph = None    # анализатор pymorphy3
        self._cache = {}      # {слово: лемма}: одно и то же слово разбирается один раз
        if use_pymorphy:
            try:
                import pymorphy3
                self._morph = pymorphy3.MorphAnalyzer()
                self.mode = "pymorphy3"
            except Exception as e:
                raise RuntimeError(
                    f"лемматизация включена (use_lemmas = true), но pymorphy3 не работает: {e}. "
                    f"Установите библиотеки (инструкция печатается при запуске) или отключите "
                    f"лемматизацию в настройках: stage2.use_lemmas = false") from e

    def lemma(self, word):
        """Лемма слова (в нижнем регистре, "ё" -> "е")."""
        w = normalize_word(word)
        result = self._cache.get(w)
        if result is None:
            result = self._compute(w)
            self._cache[w] = result
        return result

    def _compute(self, w):
        """Считает лемму без кэша. Латиницу не трогаем: pymorphy3 - для русского языка."""
        if self._morph is not None and re.search(r"[а-я]", w):
            try:
                # parse(w) возвращает варианты разбора по убыванию вероятности;
                # [0] - самый вероятный; .normal_form - начальная форма.
                return normalize_word(self._morph.parse(w)[0].normal_form)
            except Exception:
                return w
        return w


# ==============================================================================
# "ПУСТЫЕ" СЛОВА
# ==============================================================================

# Небольшой встроенный список самых частых служебных слов (русских и английских).
# Он нужен, чтобы программа работала сразу; свой полный список можно подключить через
# настройки (файл stopwords.txt, по слову на строку).
_BUILTIN_STOPWORDS = set("""
и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было вот от меня
еще нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж вам ведь там
потом себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже
себе под будет ж тогда кто этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее сейчас
были куда зачем всех никогда можно при наконец два об другой хоть после над больше тот через эти нас про всего них
какая много разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой им более всегда
конечно всю между это также можете нужно давай вот сделать например такое такие
the a an and or but if then else of to in on at by for with from as is are was were be been being it this that
these those i you he she we they me my your his her our their not no yes do does did done have has had will would
can could should may might must so than too very just also into over under about up down out off
""".split())


def load_stopwords(path=None):
    """Возвращает множество "пустых" слов: встроенные плюс слова из файла (если путь указан и файл есть).

    Формат файла: по слову на строку; пустые строки и строки с "#" игнорируются.
    """
    words = set(_BUILTIN_STOPWORDS)
    if path:
        p = Path(path)
        if p.exists():
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    words.add(normalize_word(line))
    return words


# ==============================================================================
# РАЗБОР ТЕКСТА НА СЛОВА
# ==============================================================================

# Слово: начинается с буквы, дальше буквы, цифры, "_" и "-". [^\W\d_] - "любой символ
# слова, кроме цифры и подчёркивания", то есть буква (работает и для кириллицы).
_TOKEN_RE = re.compile(r"[^\W\d_][\w\-]*", re.UNICODE)

# Адреса и метки маскирования секретов в статистике не нужны.
_URL_RE = re.compile(r"https?://\S+")
_REDACTED_RE = re.compile(r"<REDACTED:[a-z_]+>")

# Предложение кончается на . ! ? … с последующим пробелом, либо это отдельная строка.
# (?<=[.!?…]) - "позади стоит один из знаков"; \s+ - пробелы; | - "или"; \n+ - переносы строк.
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")

# Слова, которые не стоит показывать как "значимые": чисто числовые и похожие на хэши.
_HEXLIKE_RE = re.compile(r"^[0-9a-f]{12,}$")


def tokenize(text):
    """Слова из текста в нижнем регистре (минимум 2 символа). Дефис внутри слова сохраняется."""
    tokens = []
    for raw in _TOKEN_RE.findall(text):
        w = normalize_word(raw).strip("-")
        if len(w) >= 2:
            tokens.append(w)
    return tokens


def clean_for_analysis(text):
    """Убирает из текста адреса, метки маскирования секретов и метки вырезанных служебных текстов."""
    text = MARK_RE.sub(" ", text)
    return _REDACTED_RE.sub(" ", _URL_RE.sub(" ", text))


def analysis_parts(card):
    """Тексты карточки для анализа: (запрос, ответ_без_кода).

    Блоки кода ответа отбрасываются (см. text_tools.strip_code), адреса и метки секретов - тоже.
    """
    prompt = clean_for_analysis(card.get("prompt") or "")
    answer = clean_for_analysis(strip_code(card.get("answer_md") or ""))
    return prompt, answer


def is_display_term(lemma, stopwords):
    """Годится ли слово для показа как "значимое": не пустое слово, не число, не хэш, нормальной длины."""
    return (3 <= len(lemma) <= 30
            and lemma not in stopwords
            and not lemma.isdigit()
            and not _HEXLIKE_RE.match(lemma))


# ==============================================================================
# ПРИЗНАКИ КАРТОЧКИ И ИНДЕКС ПО ВСЕМ КАРТОЧКАМ
# ==============================================================================

class CardFeatures:
    """Подсчитанные признаки одной карточки."""

    def __init__(self, counts, prompt_lemmas, n_tokens, lower_text):
        self.counts = counts                # Counter {лемма: сколько раз} (запрос с весом)
        self.prompt_lemmas = prompt_lemmas  # множество лемм, встречающихся в запросе
        self.n_tokens = n_tokens            # длина карточки в словах (для BM25)
        self.lower_text = lower_text        # текст без кода в нижнем регистре (для поиска фраз)


class CorpusIndex:
    """Индекс слов по всем карточкам: считается один раз, дальше отвечает на вопросы мгновенно."""

    # Параметры BM25 - общепринятые значения. k1 - насколько быстро "насыщается" польза
    # от повторов слова; b - насколько сильно штрафуем длинные карточки.
    K1 = 1.5
    B = 0.75

    def __init__(self, lemmatizer, stopwords=None, prompt_weight=2):
        self.lemmatizer = lemmatizer
        self.stopwords = stopwords if stopwords is not None else load_stopwords()
        self.prompt_weight = prompt_weight
        self.features = {}   # {card_id: CardFeatures}
        self.df = Counter()  # document frequency: в скольких карточках встречается слово
        self.avg_len = 1.0   # средняя длина карточки в словах

    @classmethod
    def build(cls, cards, lemmatizer, stopwords=None, prompt_weight=2, progress=None):
        """Строит индекс по списку карточек.

        progress - необязательная функция progress(сделано, всего) для показа хода работы.
        """
        index = cls(lemmatizer, stopwords, prompt_weight)
        total_len = 0
        for n, card in enumerate(cards, 1):
            feats = index._analyze(card)
            index.features[card["id"]] = feats
            total_len += feats.n_tokens
            for lemma in feats.counts:   # перебор ключей Counter - это леммы карточки
                index.df[lemma] += 1     # каждая лемма в этой карточке: +1 к её document frequency
            if progress and n % 200 == 0:
                progress(n, len(cards))
        index.avg_len = (total_len / len(index.features)) if index.features else 1.0
        return index

    def _analyze(self, card):
        """Считает признаки одной карточки."""
        prompt, answer = analysis_parts(card)
        counts = Counter()
        prompt_lemmas = set()
        for token in tokenize(prompt):
            lemma = self.lemmatizer.lemma(token)
            prompt_lemmas.add(lemma)
            counts[lemma] += self.prompt_weight   # слова запроса весят больше
        for token in tokenize(answer):
            counts[self.lemmatizer.lemma(token)] += 1
        lower_text = normalize_word(prompt + "\n" + answer)
        return CardFeatures(counts, prompt_lemmas, sum(counts.values()), lower_text)

    # ------------------------------------------------------------------ вопросы к индексу

    def top_terms(self, card_id, n=10):
        """Самые значимые слова карточки: список (слово, сколько_раз, оценка), от лучших к худшим."""
        feats = self.features.get(card_id)
        if feats is None:
            return []
        n_docs = len(self.features)
        scored = []
        for lemma, tf in feats.counts.items():
            if not is_display_term(lemma, self.stopwords):
                continue
            # Условие значимости: слово встретилось минимум дважды ИЛИ стоит в запросе.
            if tf < 2 and lemma not in feats.prompt_lemmas:
                continue
            df = self.df[lemma]
            # idf - "редкость" слова: чем в меньшем числе карточек оно есть, тем больше.
            idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
            # Часть про повторы с учётом длины карточки (формула BM25).
            norm = tf + self.K1 * (1 - self.B + self.B * feats.n_tokens / self.avg_len)
            score = idf * tf * (self.K1 + 1) / norm
            scored.append((lemma, tf, score))
        scored.sort(key=lambda item: item[2], reverse=True)  # по убыванию оценки
        return scored[:n]

    def corpus_top_terms(self, card_ids, n=40, min_df=3, max_df_ratio=0.4):
        """Слова, характерные для ГРУППЫ карточек (нужны меню правил, чтобы показать ключевые слова).

        Берём слова, которые есть минимум в min_df карточках группы (не случайные) и не более
        чем в max_df_ratio их доли (не вездесущие). Возвращает [(слово, в_скольких_карточках)].
        """
        df = Counter()
        group = [cid for cid in card_ids if cid in self.features]
        for cid in group:
            for lemma in self.features[cid].counts:
                if is_display_term(lemma, self.stopwords):
                    df[lemma] += 1
        limit = max(min_df, int(len(group) * max_df_ratio))
        picked = [(w, c) for w, c in df.items() if min_df <= c <= limit]
        picked.sort(key=lambda item: (-item[1], item[0]))  # чаще - раньше; при равенстве по алфавиту
        return picked[:n]


# ==============================================================================
# ВЫБОРКА ПРЕДЛОЖЕНИЙ СО СЛОВОМ
# ==============================================================================

def sentences_with(card, word, lemmatizer, window=1, max_excerpts=5, max_chars=400):
    """Находит в карточке предложения со словом и показывает их вместе с соседними.

    Вход:
      card          карточка (по договору);
      word          слово, которое ищем (любая форма: найдём все формы по лемме);
      lemmatizer    Lemmatizer;
      window        сколько предложений показывать до и после найденного (для контекста);
      max_excerpts  сколько выдержек вернуть не больше;
      max_chars     максимальная длина одной выдержки (длинное обрезается).
    Выход: словарь {"total": сколько предложений со словом нашлось, "excerpts": [выдержки]}.
    Код в ответе пропускается (как и в статистике).
    """
    target = lemmatizer.lemma(word)
    prompt, answer = analysis_parts(card)
    # Предложения запроса и ответа идут подряд; пустые отбрасываем.
    sentences = [s.strip() for part in (prompt, answer)
                 for s in _SENT_SPLIT_RE.split(part) if s and s.strip()]

    # Индексы предложений, в которых есть слово (сравниваем по лемме).
    hit_indexes = [i for i, s in enumerate(sentences)
                   if any(lemmatizer.lemma(t) == target for t in tokenize(s))]

    # Окна вокруг найденных предложений; перекрывающиеся окна сливаем в одно.
    windows = []
    for i in hit_indexes:
        lo, hi = max(0, i - window), min(len(sentences) - 1, i + window)
        if windows and lo <= windows[-1][1] + 1:   # окно касается предыдущего - расширяем его
            windows[-1][1] = max(windows[-1][1], hi)
        else:
            windows.append([lo, hi])

    excerpts = []
    for lo, hi in windows[:max_excerpts]:
        text = " ".join(sentences[lo:hi + 1])
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "…"   # обрезаем слишком длинное
        excerpts.append(text)
    return {"total": len(hit_indexes), "excerpts": excerpts}
