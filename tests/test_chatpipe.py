# -*- coding: utf-8 -*-
"""
test_chatpipe.py - АВТОМАТИЧЕСКИЕ ПРОВЕРКИ проекта.

Запуск (из папки проекта, рядом с run_stage1.py):
    python3 -m unittest discover -s tests -v

------------------------------------------------------------------------------
ЗАЧЕМ НУЖНЫ ТЕСТЫ
------------------------------------------------------------------------------
Тест - это маленькая программа, которая вызывает нужный кусок кода с известными
данными и сверяет результат с ожидаемым. Если вы (или я) что-то изменили и случайно
сломали, тесты сразу покажут, что именно. Перед важными правками запускайте их.

Как читать: каждый класс проверяет один модуль, каждый метод test_* - одну ситуацию.
Модуль unittest входит в стандартную поставку Python; ничего устанавливать не нужно.
Методы вида self.assertEqual(a, b) сообщают об ошибке, если a != b.
"""

import os
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from collections import Counter

# Чтобы тесты нашли пакет chatpipe, добавляем папку проекта в список поиска модулей.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from chatpipe import settings as S
from chatpipe.adapters import gemini_takeout_html as G
from chatpipe.annotations import AnnotationError, AnnotationStore
from chatpipe.bundles import compose_bundles
from chatpipe.compat import check_library, ensure_compatibility, read_cpu_info
from chatpipe.console import Console
from chatpipe.dictionary import Dictionary, DictionaryError, default_dictionary
from chatpipe.keywords import CorpusIndex, Lemmatizer, sentences_with
from chatpipe.redaction import redact
from chatpipe.rules import (MatchContext, RuleEngine, RuleSet, RuleSyntaxError, Rule, evaluate,
                            expand_numbered, parse_expression)
from chatpipe.schema import (CardValidationError, read_cards, validate_card, write_cards)
from chatpipe.stage1_report import read_service_texts, write_service_texts
from chatpipe.stage2_label import main as stage2_main
from chatpipe.stage2_session import Session, resolve_paths
from chatpipe.text_tools import MARK_RE, html_to_md, split_blocks, strip_code
from chatpipe.xml_render import cdata, check_wellformed, clean_xml_text


def read_text(path):
    """Читает файл целиком и сразу закрывает его (with закрывает файл автоматически)."""
    with open(path, encoding="utf-8") as f:
        return f.read()


def write_text(path, text):
    """Записывает текст в файл и закрывает его."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def make_card(i, prompt="запрос", answer="ответ", service=None):
    """Собирает корректную карточку по договору для проверок."""
    return {"schema_version": 1, "id": f"card{i:02d}", "source": "test",
            "conversation_id": None, "conversation_title": None,
            "ts": f"2026-07-{10 + i:02d}T10:00:00+03:00", "prompt": prompt,
            "answer_md": answer, "service_md": service, "meta": {}}


class TempDirCase(unittest.TestCase):
    """Базовый класс: у каждого теста своя временная папка, которая потом удаляется."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)  # убрать папку после теста


# ==============================================================================
class TestSchema(TempDirCase):
    def test_valid_card_has_no_problems(self):
        self.assertEqual(validate_card(make_card(1)), [])

    def test_missing_and_wrong_fields_are_reported(self):
        card = make_card(1)
        del card["prompt"]
        card["id"] = 5
        card["ts"] = "вчера"
        problems = " | ".join(validate_card(card))
        self.assertIn("prompt", problems)
        self.assertIn("'id'", problems)
        self.assertIn("'ts'", problems)

    def test_time_requires_timezone(self):
        card = make_card(1)
        card["ts"] = "2026-07-13T14:41:33"
        self.assertTrue(any("часовой пояс" in p for p in validate_card(card)))

    def test_bool_is_not_a_number(self):
        card = make_card(1)
        card["schema_version"] = True
        self.assertTrue(validate_card(card))

    def test_write_read_roundtrip_and_order_checks(self):
        path = os.path.join(self.tmp, "c.jsonl")
        cards = [make_card(1), make_card(2)]
        self.assertEqual(write_cards(path, cards), 2)
        self.assertEqual(list(read_cards(path)), cards)
        with self.assertRaises(CardValidationError):      # нарушен порядок по времени
            write_cards(path, [make_card(2), make_card(1)])
        with self.assertRaises(CardValidationError):      # повторяющийся id
            write_cards(path, [make_card(1), make_card(1)])
        self.assertFalse(os.path.exists(path + ".tmp"))   # временный файл не остался

    def test_bad_json_line_is_reported_with_number(self):
        path = os.path.join(self.tmp, "bad.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{oops\n")
        with self.assertRaises(CardValidationError) as ctx:
            list(read_cards(path))
        self.assertIn("строка 1", str(ctx.exception))


# ==============================================================================
class TestTextTools(unittest.TestCase):
    def test_split_blocks_separates_code_and_text(self):
        md = "Текст до\n\n```bash\nls -la\n```\n\nТекст после"
        self.assertEqual(split_blocks(md), [
            ("text", "Текст до", ""), ("code", "ls -la", "bash"), ("text", "Текст после", "")])

    def test_longer_fence_protects_inner_fences(self):
        md = "````\nвнутри\n```bash\nls\n```\nещё\n````"
        blocks = split_blocks(md)
        self.assertEqual(len(blocks), 1)
        self.assertIn("```bash", blocks[0][1])

    def test_strip_code_keeps_only_text(self):
        self.assertEqual(strip_code("A\n\n```\ncode\n```\n\nB"), "A\n\nB")

    def test_html_to_md_keeps_special_characters_in_code(self):
        md = html_to_md("<p>Текст</p><pre><code>if a &lt; b: x = '&amp;'</code></pre>")
        self.assertIn("if a < b: x = '&'", md)
        self.assertEqual(split_blocks(md)[1][0], "code")

    def test_redaction_masks_secrets(self):
        counter = Counter()
        text = redact("DB_PASSWORD=supersecret1\nuser: me\nkey sk-abcdefghijklmnop1234", counter)
        self.assertNotIn("supersecret1", text)
        self.assertNotIn("sk-abcdefghijklmnop1234", text)
        self.assertEqual(counter["env_secret"], 1)
        # Ссылки на переменные и заглушки - не секреты.
        self.assertEqual(redact("PASSWORD=${DB_PASS}", Counter()), "PASSWORD=${DB_PASS}")


# ==============================================================================
class TestDictionary(TempDirCase):
    def test_label_is_built_from_the_path(self):
        d = default_dictionary()
        self.assertEqual(d.label("top.python"), "it_coding_python")

    def test_file_roundtrip(self):
        d = default_dictionary()
        d2 = Dictionary.from_text(d.to_text())
        self.assertEqual(d2.to_text(), d.to_text())
        self.assertEqual(d2.problems(), [])

    def test_rules_of_the_dictionary_are_enforced(self):
        d = default_dictionary()
        with self.assertRaises(DictionaryError):   # предпочтительное слово небезопасно для тегов
            d.add_concept("top.x", "topic", "sub.coding", ["c++"])
        with self.assertRaises(DictionaryError):   # слово уже занято на этом уровне
            d.add_concept("top.y", "topic", "sub.coding", ["python"])
        with self.assertRaises(DictionaryError):   # родитель не того уровня
            d.add_concept("top.z", "topic", "dom.it", ["zzz"])
        with self.assertRaises(DictionaryError):   # идентификатор занят
            d.add_concept("top.python", "topic", "sub.coding", ["pyt"])

    def test_changing_preferred_word_changes_labels(self):
        d = default_dictionary()
        d.add_synonym("sub.coding", "dev")
        d.set_preferred("sub.coding", "dev")
        self.assertEqual(d.label("top.python"), "it_dev_python")

    def test_search_prefers_exact_matches(self):
        d = default_dictionary()
        self.assertEqual([c.id for c in d.search("докер")], ["top.docker"])
        self.assertEqual(d.search("c++")[0].id, "top.cpp")

    def test_bad_file_reports_all_problems_with_line_numbers(self):
        with self.assertRaises(DictionaryError) as ctx:
            Dictionary.from_text("a.b | topic | zzz | x\nbroken line\n")
        message = str(ctx.exception)
        self.assertIn("строка 1", message)
        self.assertIn("строка 2", message)


# ==============================================================================
class TestAnnotations(TempDirCase):
    def test_automation_never_touches_cards_handled_by_a_human(self):
        s = AnnotationStore()
        s.user_add("c1", "top.python")
        self.assertFalse(s.propose("c1", "top.docker", "r1"))
        s.skip("c2")
        self.assertFalse(s.apply_by_rule("c2", "top.docker", "r1"))

    def test_several_rules_can_label_one_untouched_card(self):
        s = AnnotationStore()
        self.assertTrue(s.apply_by_rule("c1", "top.python", "r1"))
        self.assertTrue(s.apply_by_rule("c1", "top.docker", "r2"))
        self.assertEqual(len(s.concepts_of("c1")), 2)

    def test_ready_needs_a_label_and_confirms_machine_labels(self):
        s = AnnotationStore()
        with self.assertRaises(AnnotationError):
            s.mark_ready("c1")
        s.propose("c1", "top.docker", "r1")
        self.assertEqual(s.mark_ready("c1"), 1)
        self.assertEqual(s.status("c1"), "ready")
        self.assertEqual(s.concepts_of("c1")[0]["by"], "user")

    def test_removing_last_label_returns_card_to_new(self):
        s = AnnotationStore()
        s.user_add("c1", "top.python")
        s.mark_ready("c1")
        s.user_remove("c1", "top.python")
        self.assertEqual(s.status("c1"), "new")

    def test_rule_rollback_keeps_confirmed_labels(self):
        s = AnnotationStore()
        s.propose("a", "top.python", "r1")
        s.propose("b", "top.python", "r1")
        s.confirm("a")
        self.assertEqual(s.remove_by_rule("r1"), 1)
        self.assertEqual(s.status("a"), "tagged")
        self.assertEqual(s.status("b"), "new")

    def test_file_roundtrip_and_validation(self):
        path = os.path.join(self.tmp, "a.jsonl")
        s = AnnotationStore()
        s.user_add("c1", "top.python")
        s.save(path)
        self.assertEqual(AnnotationStore.load(path).records, s.records)
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"schema_version":1,"card_id":"q","status":"ready","concepts":[]}\n')
        with self.assertRaises(AnnotationError):
            AnnotationStore.load(path)


# ==============================================================================
class TestRules(unittest.TestCase):
    def context(self, words, concepts=None):
        return MatchContext(Counter(words), Counter(concepts or {}), Lemmatizer(use_pymorphy=False))

    def test_expressions(self):
        ctx = self.context({"docker": 3, "compose": 1})
        cases = {
            "docker": True, "docker >= 3": True, "docker >= 4": False,
            "not kubernetes": True, "docker and kubernetes": False,
            "docker or kubernetes and python": True,       # and сильнее or
            "(docker or kubernetes) and python": False,    # скобки меняют порядок
            "compose == 1 and docker < 5": True,
        }
        for text, expected in cases.items():
            self.assertEqual(evaluate(parse_expression(text), ctx), expected, text)

    def test_concept_terms(self):
        ctx = self.context({}, {"top.docker": 2})
        self.assertTrue(evaluate(parse_expression("@top.docker >= 2"), ctx))

    def test_syntax_errors_are_explained(self):
        for bad in ["", "docker and", "(docker", "docker )", "and docker", "docker >= x", "@"]:
            with self.assertRaises(RuleSyntaxError, msg=bad):
                parse_expression(bad)

    def test_numbered_words_are_expanded(self):
        self.assertEqual(expand_numbered("(#1 or #2) and #3", ["a", "b", "c"]), "(a or b) and c")
        with self.assertRaises(RuleSyntaxError):
            expand_numbered("#5", ["a"])

    def test_rules_file_roundtrip(self):
        text = "r001 | suggest | top.docker | (docker or докер) and not kubernetes\n"
        rs = RuleSet.from_text(text)
        self.assertEqual(RuleSet.from_text(rs.to_text()).to_text(), rs.to_text())
        with self.assertRaises(RuleSyntaxError):
            RuleSet.from_text("r1 | wrong-mode | top.x | a")

    def test_engine_respects_modes_and_human_work(self):
        cards = [make_card(1, "docker запуск", "docker compose"), make_card(2, "python", "списки"),
                 make_card(3, "docker и kubernetes", "docker kubernetes")]
        lemmatizer = Lemmatizer(use_pymorphy=False)
        index = CorpusIndex.build(cards, lemmatizer)
        engine = RuleEngine(index, default_dictionary(), lemmatizer)
        store = AnnotationStore()
        ids = [c["id"] for c in cards]
        rule = Rule("r1", "suggest", ["top.docker"], "docker and not kubernetes")
        self.assertEqual(engine.matching(rule, ids), ["card01"])
        store.user_add("card01", "top.bash")             # человек уже работал с карточкой
        self.assertEqual(engine.apply(rule, ids, store)["changed"], 0)
        store.reset("card01")
        self.assertEqual(engine.apply(rule, ids, store)["changed"], 1)
        self.assertEqual(store.status("card01"), "proposed")


# ==============================================================================
class TestKeywords(unittest.TestCase):
    def setUp(self):
        self.cards = [
            make_card(1, "Как настроить docker compose?", "Docker compose описывает сервисы docker.\n\n```yaml\nzzzcodeonly: 1\n```"),
            make_card(2, "Списки в python", "Python списки это python последовательности."),
            make_card(3, "Привет", "Здравствуйте!"),
        ]
        self.lemmatizer = Lemmatizer(use_pymorphy=False)
        self.index = CorpusIndex.build(self.cards, self.lemmatizer)

    def test_code_is_not_counted(self):
        self.assertNotIn("zzzcodeonly", self.index.features["card01"].counts)

    def test_significant_words_are_found(self):
        words = [w for w, _tf, _score in self.index.top_terms("card02", 5)]
        self.assertIn("python", words)

    def test_sentences_with_word(self):
        result = sentences_with(self.cards[0], "docker", self.lemmatizer, window=0)
        self.assertGreaterEqual(result["total"], 1)
        self.assertTrue(all("zzzcodeonly" not in e for e in result["excerpts"]))


# ==============================================================================
class TestXmlAndBundles(TempDirCase):
    NASTY_CODE = "if a < b && c > d:\n    s = '<![CDATA[ x ]]> y'\n\x1b[31mred\x1b[0m\x07"

    def test_cdata_survives_a_real_xml_parser(self):
        text = "a < b & c ]]> d"
        root = check_wellformed("<r>" + cdata(text) + "</r>")
        self.assertEqual(root.text, text)

    def test_illegal_characters_are_removed(self):
        self.assertEqual(clean_xml_text("a\x1b[31mb\x07c"), "abc")

    def test_bundles_are_written_split_and_valid(self):
        cards = [make_card(1, "Запрос <тег> & x", "Текст\n\n```bash\n" + self.NASTY_CODE + "\n```\n\nХвост"),
                 make_card(2, "второй", "слово " * 300), make_card(3, "третий", "слово " * 300),
                 make_card(4, "не готов", "x")]
        d, store = default_dictionary(), AnnotationStore()
        for card in cards:
            store.user_add(card["id"], "top.python")
        for card in cards[:3]:
            store.mark_ready(card["id"])
        out = os.path.join(self.tmp, "bundles")
        os.makedirs(out)
        write_text(os.path.join(out, "chatpipe_bundle_old_099_stage2.xml"), "устаревший файл")
        write_text(os.path.join(out, "notes.txt"), "чужой файл")
        summaries, _ = compose_bundles(cards, store, d, out, max_chars=2500)
        names = sorted(os.listdir(out))
        self.assertNotIn("chatpipe_bundle_old_099_stage2.xml", names)   # старая пачка убрана
        self.assertIn("notes.txt", names)                               # посторонний файл цел
        self.assertGreaterEqual(len(summaries), 2)                      # лимит разрезал на части
        first = ET.parse(os.path.join(out, "chatpipe_bundle_it_coding_python_001_stage2.xml")).getroot()
        card = first.find("card")
        self.assertEqual(card.find("prompt").text, "Запрос <тег> & x")
        code = card.find("answer/code")
        self.assertEqual(code.get("lang"), "bash")
        self.assertEqual(code.text, "if a < b && c > d:\n    s = '<![CDATA[ x ]]> y'\nred")
        all_text = "".join(read_text(os.path.join(out, n)) for n in names if n.endswith(".xml"))
        self.assertNotIn("не готов", all_text)                          # карточка не ready не попала


# ==============================================================================
class TestSettings(TempDirCase):
    def test_defaults_roundtrip_with_comments(self):
        path = os.path.join(self.tmp, "s.jsonc")
        S.save_settings(path)
        self.assertEqual(S.load_settings(path), S.default_settings())
        self.assertEqual(S.settings_problems(S.load_settings(path)), [])
        self.assertIn("// ", read_text(path))                          # комментарии на месте

    def test_user_values_survive_resave_and_urls_with_slashes_are_safe(self):
        path = os.path.join(self.tmp, "s.jsonc")
        S.save_settings(path)
        text = read_text(path)
        text = text.replace('"bundle_max_chars": 80000', '"bundle_max_chars": 50000')
        text = text.replace('"anchor_rules_file": null', '"anchor_rules_file": "https://x.example/r.txt"')
        write_text(path, text)
        values = S.load_settings(path)
        S.save_settings(path, values)
        self.assertEqual(S.load_settings(path), values)
        self.assertEqual(values["limits"]["bundle_max_chars"], 50000)

    def test_syntax_error_has_the_right_line_number(self):
        with self.assertRaises(S.SettingsError) as ctx:
            S.parse_jsonc('// комментарий\n{\n  "a": 1,\n}\n')
        self.assertIn("строка 4", str(ctx.exception))


# ==============================================================================
class TestStage2EndToEnd(TempDirCase):
    """Сквозная проверка: меню этапа 2 управляется заготовленными ответами вместо клавиатуры."""

    def run_session(self, answers):
        feed = iter(answers)
        printed = []

        def fake_input(prompt):
            try:
                return next(feed)
            except StopIteration:
                raise EOFError           # ответы кончились = Ctrl+D

        console = Console(input_fn=fake_input, print_fn=lambda text="": printed.append(str(text)))
        old = os.getcwd()
        os.chdir(self.tmp)               # программа работает с относительными путями
        try:
            code = stage2_main([], console=console)
        finally:
            os.chdir(old)
        return code, "\n".join(printed)

    def test_full_labeling_session(self):
        os.makedirs(os.path.join(self.tmp, "output"))
        write_cards(os.path.join(self.tmp, "output", "chatpipe_cards_stage1.jsonl"), [
            make_card(1, "docker compose не стартует", "Проверьте docker. Перезапустите docker."),
            make_card(2, "списки python", "python списки python срезы"),
            make_card(3, "Привет", "Здравствуйте")])
        answers = [
            "1", "6", "python",            # разметка -> необработанные со словом python
            "lf python",                   # метка + готово для карточки 2 (очередь кончилась -> главное меню)
            "1", "3",                      # разметка -> новые (карточки 1 и 3)
            "l docker", "f", "s",          # карточка 1: метка и готово; карточка 3: пропуск
            "4", "1", "", "д",             # пачки: только ready, лимит по умолчанию
            "0"]                           # выход
        code, transcript = self.run_session(answers)
        self.assertEqual(code, 0)
        store = AnnotationStore.load(os.path.join(self.tmp, "user-data", "chatpipe_annotations_stage2.jsonl"))
        self.assertEqual(store.status("card01"), "ready")
        self.assertEqual(store.status("card02"), "ready")
        self.assertEqual(store.status("card03"), "skipped")
        bundles = os.listdir(os.path.join(self.tmp, "output", "bundles"))
        self.assertIn("chatpipe_bundle_it_coding_python_001_stage2.xml", bundles)
        self.assertIn("chatpipe_bundle_it_system-administration_docker_001_stage2.xml", bundles)

    def test_non_interactive_commands(self):
        os.makedirs(os.path.join(self.tmp, "output"))
        write_cards(os.path.join(self.tmp, "output", "chatpipe_cards_stage1.jsonl"),
                    [make_card(1, "docker", "docker docker")])
        os.makedirs(os.path.join(self.tmp, "user-data"))
        RuleSet.from_text("r001 | apply-ready | top.docker | docker >= 2").save(
            os.path.join(self.tmp, "user-data", "chatpipe_anchor-rules_stage2.txt"))
        old = os.getcwd()
        os.chdir(self.tmp)
        try:
            quiet = Console(print_fn=lambda text="": None)
            self.assertEqual(stage2_main(["--apply-rules"], console=quiet), 0)
            self.assertEqual(stage2_main(["--compose"], console=quiet), 0)
            self.assertEqual(stage2_main(["--stats"], console=quiet), 0)
            self.assertEqual(stage2_main(["--stats", "--cards", "/нет/такого.jsonl"], console=quiet), 1)
        finally:
            os.chdir(old)
        self.assertTrue(any(n.endswith("_stage2.xml") for n in os.listdir(os.path.join(self.tmp, "output", "bundles"))))


# ==============================================================================
def make_piece(head, ts_text, answer_html=""):
    """Собирает кусок HTML одной карточки в формате Takeout (упрощённо)."""
    return (
        '<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp"><div class="mdl-grid">'
        '<div class="header-cell mdl-cell mdl-cell--12-col"><p class="mdl-typography--title">'
        'Gemini Apps<br></p></div>'
        '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">'
        f'{head}<br>{ts_text}<br>{answer_html}<br></div>'
        '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1 '
        'mdl-typography--text-right"></div>'
        '<div class="content-cell mdl-cell mdl-cell--12-col mdl-typography--caption">'
        '<b>Products:</b></div></div></div>')


class TestTakeoutCardParsing(unittest.TestCase):
    def test_id_does_not_depend_on_timezone_name(self):
        summer = G._parse_card(make_piece("Prompted Привет", "Jul 18, 2026, 10:46:52 AM EEST",
                                          "<p>a</p>"), Counter())
        winter = G._parse_card(make_piece("Prompted Привет", "Jul 18, 2026, 9:46:52 AM EET",
                                          "<p>a</p>"), Counter())
        other = G._parse_card(make_piece("Prompted Другое", "Jul 18, 2026, 10:46:52 AM EEST",
                                         "<p>a</p>"), Counter())
        self.assertEqual(summer["id"], winter["id"])      # один момент времени = один id
        self.assertNotEqual(summer["id"], other["id"])    # другой запрос = другой id

    def test_non_prompted_records_get_skip_reason(self):
        feedback = G._parse_card(make_piece("Gave feedback: Good response",
                                            "Jul 18, 2026, 10:46:52 AM EEST"), Counter())
        normal = G._parse_card(make_piece("Prompted Привет", "Jul 18, 2026, 10:46:52 AM EEST",
                                          "<p>a</p>"), Counter())
        self.assertIn("skip_reason", feedback["meta"])
        self.assertNotIn("skip_reason", normal["meta"])

    def test_kind_is_normalized(self):
        canvas = G._parse_card(make_piece("Created Gemini Canvas titled Vagus nerve",
                                          "Jul 18, 2026, 10:46:52 AM EEST"), Counter())
        feedback = G._parse_card(make_piece("Gave feedback: Good response",
                                            "Jul 18, 2026, 10:46:52 AM EEST"), Counter())
        self.assertEqual(canvas["meta"]["kind"], "Created Gemini Canvas")
        self.assertEqual(feedback["meta"]["kind"], "Gave feedback")

    def test_service_block_is_cut_out_and_replaced_by_marker(self):
        html = ("<p>Текст до</p><hr><h2>🗃️ Служебный файл проекта X</h2><h3>1. Main Goal</h3>"
                "<p>Цель проекта</p><hr><p>Текст после</p>")
        card = G._parse_card(make_piece("Prompted Вопрос", "Jul 18, 2026, 10:46:52 AM EEST",
                                        html), Counter())
        self.assertIn("Цель проекта", card["service_md"])
        self.assertNotIn("Цель проекта", card["answer_md"])
        self.assertIn("Текст до", card["answer_md"])
        self.assertIn("Текст после", card["answer_md"])
        found = MARK_RE.search(card["answer_md"])
        self.assertIsNotNone(found)
        self.assertEqual(found.group(1), "svc-" + card["id"])


class TestServiceTexts(TempDirCase):
    def test_write_and_read_roundtrip(self):
        path = os.path.join(self.tmp, "svc.md")
        cards = [{"id": "abc123", "ts": "2026-07-18T10:00:00+03:00",
                  "service_md": "### 1. Main Goal\n\nЦель"},
                 {"id": "def456", "ts": None, "service_md": None}]
        self.assertEqual(write_service_texts(path, cards), 1)
        self.assertEqual(read_service_texts(path), {"svc-abc123": "### 1. Main Goal\n\nЦель"})
        self.assertFalse(os.path.exists(path + ".tmp"))


class TestAutoSkip(TempDirCase):
    def test_store_auto_skip(self):
        store = AnnotationStore()
        self.assertTrue(store.auto_skip("c1", "причина"))
        self.assertFalse(store.auto_skip("c1", "причина"))        # повторно ничего не делает
        self.assertEqual(store.status("c1"), "skipped")
        self.assertFalse(store.automation_may_touch("c1"))
        path = os.path.join(self.tmp, "a.jsonl")
        store.save(path)
        self.assertEqual(AnnotationStore.load(path).status("c1"), "skipped")

    def test_session_skips_cards_with_skip_reason_on_load(self):
        os.makedirs(os.path.join(self.tmp, "output"))
        unclear = make_card(2)
        unclear["meta"] = {"skip_reason": "тест"}
        write_cards(os.path.join(self.tmp, "output", "chatpipe_cards_stage1.jsonl"),
                    [make_card(1), unclear])
        old = os.getcwd()
        os.chdir(self.tmp)
        try:
            settings = S.default_settings()
            session = Session(settings, resolve_paths(settings), say=lambda *a, **k: None)
            session.load()
        finally:
            os.chdir(old)
        self.assertEqual(session.store.status("card01"), "new")
        self.assertEqual(session.store.status("card02"), "skipped")


class TestCompat(unittest.TestCase):
    def test_missing_library_is_detected(self):
        self.assertEqual(check_library({"smoke": "import no_such_module_xyz"})[0], "missing")

    def test_working_library_is_ok(self):
        self.assertEqual(check_library({"smoke": "print('ok')", "expect": "ok"})[0], "ok")

    def test_illegal_instruction_is_detected(self):
        lib = {"smoke": "import os, signal; os.kill(os.getpid(), signal.SIGILL)"}
        self.assertEqual(check_library(lib)[0], "cpu")

    def test_cpu_info_is_readable_or_absent(self):
        info = read_cpu_info()
        self.assertTrue(info is None or "flags" in info)

    def test_check_is_skipped_when_lemmas_disabled(self):
        printed = []
        self.assertTrue(ensure_compatibility(False, write=printed.append))
        self.assertTrue(printed)


if __name__ == "__main__":
    unittest.main()
