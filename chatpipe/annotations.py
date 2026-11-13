# -*- coding: utf-8 -*-
"""
annotations.py - СЛОЙ АННОТАЦИЙ: всё, что вы (или правила) добавляете к карточкам.

------------------------------------------------------------------------------
ЧТО ТАКОЕ АННОТАЦИЯ
------------------------------------------------------------------------------
Карточка из stage1 - как страница книги: мы её не правим. Аннотация - это пометки
"на закладке" к этой странице: статус работы и метки (понятия словаря). Аннотации
хранятся ОТДЕЛЬНО от карточек и привязаны к ним по идентификатору (card_id).

Зачем отдельно:
  * файл карточек пересоздаётся при каждой новой выгрузке, а метки при этом
    должны остаться - при привязке по card_id они остаются сами;
  * ваша ручная разметка - самое ценное, что здесь появится: её удобно хранить
    маленьким отдельным файлом и делать с него резервные копии;
  * автоматические правила могут "экспериментировать", не трогая карточки.

------------------------------------------------------------------------------
СТАТУСЫ КАРТОЧКИ
------------------------------------------------------------------------------
  new       записи нет вообще (подразумевается): меток нет, никто карточку не трогал
  proposed  метки ПРЕДЛОЖИЛО правило, человек ещё не подтвердил
  tagged    метки поставил (или подтвердил) человек, но работа не закончена
  ready     человек явно поставил флаг "готово к компоновке"
  skipped   карточка не нужна (приветствие, тест, мусор)

Правила игры:
  * автоматика (правила) НИКОГДА не трогает карточки, с которыми уже работал человек
    (в записи стоит отметка user_touched) и карточки со статусом skipped. Карточки,
    на которых стоят только машинные метки, ей доступны: так одна карточка может
    получить метки от нескольких правил;
  * "ready" нельзя поставить без хотя бы одной метки;
  * у каждой метки записано, КТО её поставил (user - человек, rule - правило,
    ai - ИИ), КОГДА и по какому правилу. Поэтому любое автоматически поставленное
    можно отфильтровать, проверить или откатить (remove_by_rule).

------------------------------------------------------------------------------
ФОРМАТ ФАЙЛА .jsonl (одна запись - одна строка; записи есть только у тронутых карточек)
------------------------------------------------------------------------------
  {"schema_version": 1, "card_id": "3f9c1a7e5b2d4c80", "status": "tagged",
   "concepts": [{"id": "top.python", "by": "user", "at": "2026-10-22T09:15:00+03:00",
                 "rule": null}],
   "user_touched": true, "updated_at": "2026-10-22T09:15:00+03:00"}
В файле хранятся ИДЕНТИФИКАТОРЫ понятий; текст метки (it_coding_python) собирается
из словаря при показе (см. dictionary.py).
"""

import json                      # чтение и запись JSON
import os                        # os.replace - атомарная замена файла
from collections import Counter  # словарь-счётчик
from datetime import datetime    # время
from pathlib import Path         # пути

# Версия формата записи аннотации (по аналогии с schema_version карточки).
ANNOTATION_VERSION = 1

# Допустимые значения.
STATUSES = ("new", "proposed", "tagged", "ready", "skipped")
AUTHORS = ("user", "rule", "ai")


class AnnotationError(ValueError):
    """Ошибка работы с аннотациями (недопустимое действие или нарушен формат файла)."""


def now_iso():
    """Текущее время в формате ISO 8601 с часовым поясом компьютера, до секунд.

    datetime.now().astimezone() - "сейчас", с подставленным местным часовым поясом;
    isoformat(timespec="seconds") - без долей секунды: 2026-10-22T09:15:00+03:00.
    """
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ==============================================================================
# ПРОВЕРКА ЗАПИСИ (договор для файла аннотаций)
# ==============================================================================

def validate_annotation(rec):
    """Проверяет одну запись аннотации. Возвращает список проблем (пустой = запись в порядке)."""
    if not isinstance(rec, dict):
        return [f"запись должна быть словарём, а не {type(rec).__name__}"]
    problems = []
    if rec.get("schema_version") != ANNOTATION_VERSION:
        problems.append(f"schema_version должна быть {ANNOTATION_VERSION}")
    if not isinstance(rec.get("card_id"), str) or not rec.get("card_id"):
        problems.append("card_id должен быть непустым текстом")
    if rec.get("status") not in STATUSES or rec.get("status") == "new":
        # "new" в файле не хранится: у такой карточки просто нет записи.
        problems.append(f"status должен быть одним из {list(STATUSES[1:])}")
    concepts = rec.get("concepts")
    if not isinstance(concepts, list):
        problems.append("concepts должен быть списком")
        concepts = []
    for i, c in enumerate(concepts, 1):
        if not isinstance(c, dict) or not isinstance(c.get("id"), str):
            problems.append(f"concepts[{i}]: нужен объект с текстовым полем id")
            continue
        if c.get("by") not in AUTHORS:
            problems.append(f"concepts[{i}].by должен быть одним из {list(AUTHORS)}")
        if not isinstance(c.get("at"), str):
            problems.append(f"concepts[{i}].at должен быть текстом (время)")
        if c.get("rule") is not None and not isinstance(c.get("rule"), str):
            problems.append(f"concepts[{i}].rule должен быть текстом или null")
    if "user_touched" in rec and not isinstance(rec["user_touched"], bool):
        problems.append("user_touched должен быть true или false")
    if rec.get("status") == "ready" and not concepts:
        problems.append("карточка со статусом ready должна иметь хотя бы одну метку")
    return problems


# ==============================================================================
# ХРАНИЛИЩЕ АННОТАЦИЙ
# ==============================================================================

class AnnotationStore:
    """Все аннотации в памяти + чтение и запись файла + допустимые действия над карточками.

    Все изменения идут через методы этого класса: он следит за правилами игры
    (см. шапку файла), и нарушить их из других модулей нельзя.
    """

    def __init__(self):
        self.records = {}  # {card_id: запись-словарь}; у карточек без записи статус new

    # ------------------------------------------------------------------ чтение

    def status(self, card_id):
        """Статус карточки; у карточки без записи - "new"."""
        rec = self.records.get(card_id)
        return rec["status"] if rec else "new"

    def concepts_of(self, card_id):
        """Список меток карточки (копия): [{"id":..., "by":..., "at":..., "rule":...}, ...]."""
        rec = self.records.get(card_id)
        return [dict(c) for c in rec["concepts"]] if rec else []

    def concept_ids(self, card_id):
        """Только идентификаторы понятий карточки."""
        rec = self.records.get(card_id)
        return [c["id"] for c in rec["concepts"]] if rec else []

    def status_counts(self, all_card_ids):
        """Сколько карточек в каждом статусе (карточки без записи считаются как new)."""
        counts = Counter({s: 0 for s in STATUSES})
        for cid in all_card_ids:
            counts[self.status(cid)] += 1
        return counts

    def automation_may_touch(self, card_id):
        """Может ли автоматика (правила) менять эту карточку?

        Да - если записи нет (карточка new) или человек её ещё не трогал и она не skipped.
        """
        rec = self.records.get(card_id)
        if rec is None:
            return True
        return not rec.get("user_touched") and rec["status"] != "skipped"

    def cards_with_machine_labels(self):
        """Идентификаторы карточек, у которых есть метки, поставленные правилами (by == "rule").

        Нужна для режима "проверить то, что поставила автоматика".
        """
        return [cid for cid, rec in self.records.items()
                if any(c["by"] == "rule" for c in rec["concepts"])]

    def cards_with_status(self, *statuses):
        """Идентификаторы карточек, у которых есть запись с одним из указанных статусов."""
        return [cid for cid, rec in self.records.items() if rec["status"] in statuses]

    # ------------------------------------------------------------------ служебное

    def _touch(self, card_id, default_status):
        """Возвращает запись карточки, создавая её при необходимости, и обновляет updated_at."""
        rec = self.records.get(card_id)
        if rec is None:
            rec = {"schema_version": ANNOTATION_VERSION, "card_id": card_id,
                   "status": default_status, "concepts": [], "user_touched": False,
                   "updated_at": now_iso()}
            self.records[card_id] = rec
        rec["updated_at"] = now_iso()
        return rec

    def _drop_if_empty(self, card_id):
        """Если у карточки не осталось меток и она не skipped - запись удаляется (снова new)."""
        rec = self.records.get(card_id)
        if rec and not rec["concepts"] and rec["status"] != "skipped":
            del self.records[card_id]

    def _find_concept(self, rec, concept_id):
        """Метка с таким id внутри записи или None."""
        for c in rec["concepts"]:
            if c["id"] == concept_id:
                return c
        return None

    # ------------------------------------------------------------------ действия правил (автоматика)

    def propose(self, card_id, concept_id, rule_id):
        """Правило ПРЕДЛАГАЕТ метку: статус proposed. Возвращает True, если что-то изменилось.

        Карточки, которых касался человек, и пропущенные (skipped) не затрагиваются.
        """
        if not self.automation_may_touch(card_id):
            return False
        rec = self._touch(card_id, "proposed")
        if self._find_concept(rec, concept_id):
            return False  # такая метка уже есть
        rec["concepts"].append({"id": concept_id, "by": "rule", "at": now_iso(), "rule": rule_id})
        return True

    def apply_by_rule(self, card_id, concept_id, rule_id, ready=False):
        """Правило САМО ставит метку: статус tagged (или ready, если правило этого требует).

        Если карточка уже ready (от более раннего доверенного правила), статус не понижается.
        """
        if not self.automation_may_touch(card_id):
            return False
        rec = self._touch(card_id, "tagged")
        changed = False
        if not self._find_concept(rec, concept_id):
            rec["concepts"].append({"id": concept_id, "by": "rule", "at": now_iso(), "rule": rule_id})
            changed = True
        new_status = "ready" if (ready or rec["status"] == "ready") else "tagged"
        if rec["status"] != new_status:
            rec["status"] = new_status
            changed = True
        return changed

    def remove_by_rule(self, rule_id):
        """Откат правила: убирает все метки, поставленные этим правилом и НЕ подтверждённые человеком.

        Когда человек подтверждает предложение (confirm), метка получает by="user"
        (поле rule остаётся как память о происхождении), и откат её уже не затронет.
        Возвращает число убранных меток.
        """
        removed = 0
        for card_id in list(self.records):  # list(...): копия ключей, ведь запись может удалиться
            rec = self.records[card_id]
            keep = [c for c in rec["concepts"] if not (c["by"] == "rule" and c.get("rule") == rule_id)]
            gone = len(rec["concepts"]) - len(keep)
            if not gone:
                continue  # это правило к карточке отношения не имело
            removed += gone
            rec["concepts"] = keep
            if not keep and rec["status"] != "skipped":
                del self.records[card_id]  # меток не осталось: карточка снова new
                continue
            if keep and rec["status"] == "proposed" and all(c["by"] != "rule" for c in keep):
                rec["status"] = "tagged"   # предложений не осталось, остались только метки человека
            rec["updated_at"] = now_iso()
        return removed

    # ------------------------------------------------------------------ действия человека

    def user_add(self, card_id, concept_id):
        """Человек ставит метку. Статус становится tagged (если был new, proposed или skipped)."""
        rec = self._touch(card_id, "tagged")
        rec["user_touched"] = True
        existing = self._find_concept(rec, concept_id)
        if existing:
            # Метка уже была (возможно, предложена правилом): человек берёт ответственность на себя.
            existing["by"] = "user"
            existing["at"] = now_iso()
        else:
            rec["concepts"].append({"id": concept_id, "by": "user", "at": now_iso(), "rule": None})
        if rec["status"] in ("new", "proposed", "skipped"):
            rec["status"] = "tagged"

    def user_remove(self, card_id, concept_id):
        """Человек снимает метку. Если меток не осталось, ready сбрасывается и карточка снова new."""
        rec = self.records.get(card_id)
        if not rec or not self._find_concept(rec, concept_id):
            raise AnnotationError(f"у карточки {card_id} нет метки {concept_id}")
        rec["concepts"] = [c for c in rec["concepts"] if c["id"] != concept_id]
        rec["user_touched"] = True
        rec["updated_at"] = now_iso()
        if not rec["concepts"] and rec["status"] == "ready":
            rec["status"] = "tagged"  # без меток ready невозможен; _drop_if_empty вернёт карточку в new
        self._drop_if_empty(card_id)

    def confirm(self, card_id):
        """Человек подтверждает предложения правил: все метки становятся его, статус tagged."""
        rec = self.records.get(card_id)
        if not rec or rec["status"] != "proposed":
            raise AnnotationError(f"у карточки {card_id} нет предложений для подтверждения")
        for c in rec["concepts"]:
            c["by"] = "user"   # поле rule остаётся: помним, откуда метка появилась
            c["at"] = now_iso()
        rec["status"] = "tagged"
        rec["user_touched"] = True
        rec["updated_at"] = now_iso()

    def mark_ready(self, card_id):
        """Человек ставит флаг "готово к компоновке". Требует хотя бы одну метку.

        Ставя флаг, человек отвечает за ВСЕ метки карточки: машинные метки (от правил),
        которые он не снял, считаются подтверждёнными (by становится "user"; поле rule
        остаётся как память о происхождении). Возвращает, сколько меток подтверждено.
        """
        rec = self.records.get(card_id)
        if not rec or not rec["concepts"]:
            raise AnnotationError("нельзя поставить 'готово' без единой метки")
        confirmed = 0
        for c in rec["concepts"]:
            if c["by"] == "rule":
                c["by"] = "user"
                c["at"] = now_iso()
                confirmed += 1
        rec["status"] = "ready"
        rec["user_touched"] = True
        rec["updated_at"] = now_iso()
        return confirmed

    def unmark_ready(self, card_id):
        """Снимает флаг "готово": ready -> tagged."""
        rec = self.records.get(card_id)
        if not rec or rec["status"] != "ready":
            raise AnnotationError("у карточки нет флага 'готово'")
        rec["status"] = "tagged"
        rec["user_touched"] = True
        rec["updated_at"] = now_iso()

    def skip(self, card_id):
        """Помечает карточку как ненужную (метки, если были, сохраняются, но в компоновку не идут)."""
        rec = self._touch(card_id, "skipped")
        rec["status"] = "skipped"
        rec["user_touched"] = True

    def auto_skip(self, card_id, reason):
        """Система сама помечает карточку skipped (человек её не трогал).

        Возвращает True, если запись создана. Если у карточки уже есть запись
        (например, вы её сами разметили), ничего не меняет и возвращает False.
        """
        if card_id in self.records:
            return False
        rec = self._touch(card_id, "skipped")
        rec["skip_reason"] = reason
        return True

    def unskip(self, card_id):
        """Возвращает карточку из skipped: tagged, если есть метки, иначе снова new."""
        rec = self.records.get(card_id)
        if not rec or rec["status"] != "skipped":
            raise AnnotationError("карточка не помечена как пропущенная")
        rec["status"] = "tagged"
        rec["user_touched"] = True
        rec["updated_at"] = now_iso()
        self._drop_if_empty(card_id)

    def reset(self, card_id):
        """Полностью стирает аннотацию карточки (она снова new)."""
        self.records.pop(card_id, None)

    # ------------------------------------------------------------------ файл

    def save(self, path):
        """Сохраняет все записи в файл .jsonl (атомарно). Записи отсортированы по card_id."""
        path = Path(path)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for card_id in sorted(self.records):
                f.write(json.dumps(self.records[card_id], ensure_ascii=False) + "\n")
        os.replace(tmp, path)

    @classmethod
    def load(cls, path):
        """Читает файл аннотаций. Если файла нет - возвращает пустое хранилище."""
        store = cls()
        path = Path(path)
        if not path.exists():
            return store
        with path.open("r", encoding="utf-8") as f:
            for number, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as e:
                    raise AnnotationError(f"{path}, строка {number}: не корректный JSON ({e})") from e
                problems = validate_annotation(rec)
                if problems:
                    raise AnnotationError(f"{path}, строка {number}: " + "; ".join(problems))
                store.records[rec["card_id"]] = rec
        return store
