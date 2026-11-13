# -*- coding: utf-8 -*-
"""
xml_render.py - СБОРКА XML ДЛЯ ИИ: карточки с метками, обёрнутые в теги.

------------------------------------------------------------------------------
ГДЕ В КОНВЕЙЕРЕ ЭТО НУЖНО
------------------------------------------------------------------------------
Внутри программы карточки хранятся как JSONL (см. schema.py). XML появляется только
на самом последнем шаге: когда из готовых карточек собирается "пачка" для ИИ.
Теги нужны, чтобы модель (и скрипты) сразу различали: где метки, где запрос, где
обычный текст, где код.

------------------------------------------------------------------------------
КАК МЫ БОРЕМСЯ С СИМВОЛАМИ < И &
------------------------------------------------------------------------------
В коде и логах полно "<" и "&". В настоящем XML их нельзя писать "как есть". Есть два
способа: заменить на &lt; и &amp; (экранирование) или завернуть кусок в особую
секцию CDATA, содержимое которой XML-разборщик не трогает. Мы выбрали CDATA для ВСЕГО
текстового содержимого (запрос, текст ответа, код):
  * текст остаётся читаемым и для человека, и для модели (без &lt; и &amp;amp;);
  * XML-разборщик возвращает его в точности таким, каким он был.
Экранирование делается ровно ОДИН раз - здесь, на последнем шаге. Если бы мы
экранировали раньше (при чтении HTML), "&" превратился бы в "&amp;", а затем, при
сборке XML, ещё раз в "&amp;amp;" - получился бы мусор.
Экранируются только значения атрибутов (id, время...), для них используется
xml.sax.saxutils.quoteattr из стандартной библиотеки.

Две тонкости:
  * внутри CDATA нельзя писать последовательность "]]>" (она закрывает секцию).
    Если она встретилась в тексте, секция разбивается на две (см. cdata);
  * XML 1.0 запрещает некоторые управляющие символы даже в CDATA. Они бывают в логах
    терминала (цветовые коды ANSI вида ESC[31m). Мы их вычищаем (см. clean_xml_text).

После сборки каждая пачка проверяется настоящим XML-разборщиком (check_wellformed):
если получилась ошибка, программа узнает об этом сразу, а не вы - при загрузке пачки.
"""

import re                                   # регулярные выражения
import xml.etree.ElementTree as ET          # стандартный разборщик XML
from xml.sax.saxutils import quoteattr      # безопасное оформление значений атрибутов

from .text_tools import split_blocks        # деление ответа на текст и код


# ==============================================================================
# ОЧИСТКА И ОФОРМЛЕНИЕ ТЕКСТА
# ==============================================================================

# Цветовые коды терминала (ANSI): ESC [ ... буква. Разбор шаблона:
#   \x1b\[          символ ESC и "["
#   [0-9;?]*        цифры и ";" - параметры
#   [ -/]*          необязательные "промежуточные" символы
#   [@-~]           завершающая буква (код команды)
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# Более редкие последовательности ESC ] ... (заголовки окна и т.п.), кончаются BEL или ESC \.
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# Всё, что XML 1.0 не допускает: любой символ ВНЕ списка разрешённых диапазонов.
# [^...] - "любой, кроме": разрешены табуляция, перевод строки, возврат каретки и
# основные диапазоны Unicode (без суррогатов и без \ufffe, \uffff).
_XML_ILLEGAL_RE = re.compile("[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def clean_xml_text(text):
    """Убирает из текста всё, что нельзя записывать в XML (цветовые коды терминала и управляющие символы)."""
    text = _ANSI_OSC_RE.sub("", _ANSI_CSI_RE.sub("", text))
    return _XML_ILLEGAL_RE.sub("", text)


def cdata(text):
    """Оборачивает текст в секцию CDATA (содержимое не экранируется).

    Если в тексте есть "]]>", секция разбивается на две так, чтобы эти три символа
    оказались в разных секциях: "]]" в конце первой и ">" в начале второй.
    """
    text = clean_xml_text(text)
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def attr(value):
    """Значение атрибута в кавычках, с экранированием спецсимволов: id="abc"."""
    return quoteattr(clean_xml_text(str(value)))


# ==============================================================================
# КАРТОЧКА И ПАЧКА
# ==============================================================================

def render_card_xml(card, concepts, dictionary, indent=1):
    """Собирает XML одной карточки.

    Вход:
      card        карточка по договору (schema.py);
      concepts    её метки из аннотаций: [{"id":..., "by":...}, ...];
      dictionary  словарь (чтобы показать текст метки рядом с идентификатором);
      indent      уровень отступа тегов (содержимое CDATA не сдвигается: пробелы
                  внутри текста - часть текста).
    Выход: строка XML.
    Структура:
      <card id=".." ts=".." source="..">
        <labels><label concept="top.python" text="it_coding_python" by="user"/></labels>
        <prompt><![CDATA[..]]></prompt>
        <answer><text>..</text><code lang="bash">..</code>...</answer>
        <service-file>..</service-file>        (только если у карточки он есть)
      </card>
    """
    pad = "  " * indent
    pad2 = pad + "  "
    pad3 = pad2 + "  "
    attrs = f"id={attr(card['id'])} ts={attr(card.get('ts') or '')} source={attr(card['source'])}"
    lines = [f"{pad}<card {attrs}>"]

    # Метки: идентификатор (для скриптов) и текст (для человека и модели).
    if concepts:
        lines.append(f"{pad2}<labels>")
        for c in concepts:
            text = dictionary.label(c["id"]) if dictionary.get(c["id"]) else ""
            lines.append(f"{pad3}<label concept={attr(c['id'])} text={attr(text)} by={attr(c.get('by', ''))}/>")
        lines.append(f"{pad2}</labels>")

    lines.append(f"{pad2}<prompt>{cdata(card['prompt'])}</prompt>")

    # Ответ делим на текст и код (одной общей функцией split_blocks): код получает свой тег.
    lines.append(f"{pad2}<answer>")
    for kind, content, lang in split_blocks(card["answer_md"]):
        if kind == "code":
            lang_attr = f" lang={attr(lang)}" if lang else ""
            lines.append(f"{pad3}<code{lang_attr}>{cdata(content)}</code>")
        else:
            lines.append(f"{pad3}<text>{cdata(content)}</text>")
    lines.append(f"{pad2}</answer>")

    if card.get("service_md"):
        lines.append(f"{pad2}<service-file>{cdata(card['service_md'])}</service-file>")
    lines.append(f"{pad}</card>")
    return "\n".join(lines)


def render_bundle_xml(card_xml_list, topic_label, concept_id, part, parts, generated_at):
    """Собирает XML пачки: корневой тег с описанием и внутри - готовые XML карточек.

    card_xml_list - список строк, полученных из render_card_xml.
    """
    attrs = (f"topic={attr(topic_label)} concept={attr(concept_id)} part=\"{part}\" "
             f"parts=\"{parts}\" cards=\"{len(card_xml_list)}\" generated={attr(generated_at)}")
    body = "\n".join(card_xml_list)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<bundle {attrs}>\n{body}\n</bundle>\n'


def check_wellformed(xml_text):
    """Проверяет, что текст - корректный XML (его принимает стандартный разборщик).

    Бросает ValueError с описанием, если нет. Возвращает корневой элемент (его можно
    использовать для проверки содержимого).
    """
    try:
        # fromstring принимает байты: так учитывается объявление кодировки в первой строке.
        return ET.fromstring(xml_text.encode("utf-8"))
    except ET.ParseError as e:
        raise ValueError(f"собранный XML некорректен: {e}") from e
