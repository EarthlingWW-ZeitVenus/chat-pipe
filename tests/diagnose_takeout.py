#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diagnose_takeout.py - ДИАГНОСТИКА реального экспорта Takeout.
Ничего не меняет: читает файл и пишет текстовый отчёт.
Значения секретов НЕ печатает (только имена ключей).

Запуск из папки проекта:
    python3 tests/diagnose_takeout.py MyActivity2.html -o diagnose_report.txt
"""

import argparse
import copy
import os
import re
import statistics
import sys
from collections import Counter
from datetime import timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from chatpipe.adapters import gemini_takeout_html as G
from chatpipe.redaction import redact_card
from chatpipe.schema import ts_to_datetime
from chatpipe.text_tools import sha

_H_RE = re.compile(r"<h([1-6])[^>]*>(.*?)</h\1>", re.S | re.I)
_MENTION_RE = re.compile(r"Служебн\w+\s+файл", re.I)
_SECRET_LOOK = re.compile(
    r"(?i)([A-Za-z0-9_.\-]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY)[A-Za-z0-9_.\-]*)"
    r"[ \t]*[=:][ \t]*[\"']?(?![\$<{])([^\s\"']{4,})")


def clip(text, n=80):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n] + "…"


def strip_tags(text):
    return re.sub(r"<[^>]+>", "", text)


def cell_of(piece):
    m = G.CELL_RE.search(piece)
    if m:
        return m.group(1)
    m = G.CELL_FALLBACK_RE.search(piece)
    if m:
        return m.group(1).split(G.CAPTION_CUT)[0]
    return None


def section_env():
    L = ["1. ОКРУЖЕНИЕ", f"  python {sys.version.split()[0]}  ({sys.executable})"]
    try:
        import pymorphy3
        form = pymorphy3.MorphAnalyzer().parse("делали")[0].normal_form
        L.append(f"  pymorphy3: работает (делали -> {form})")
    except Exception as e:
        L.append(f"  pymorphy3: НЕ работает ({type(e).__name__}: {e})")
    return L


def section_blocks(pieces):
    total = ended_hr = ended_end = mentions = 0
    h3, extra_h2 = Counter(), Counter()
    swallowed, short_blocks, mention_examples = [], [], []
    for piece in pieces:
        cell = cell_of(piece)
        if cell is None:
            continue
        tm = G.TS_RE.search(cell)
        ts, after = "?", cell
        if tm:
            dt, _known = G.parse_ts(tm)
            ts = dt.isoformat() if dt else "?"
            after = cell[tm.end():]
        after = re.sub(r"^(\s*<br\s*/?>)+", "", after)
        sm = G.SERVICE_RE.search(after)
        if not sm:
            mm = _MENTION_RE.search(after)
            if mm:
                mentions += 1
                if len(mention_examples) < 25:
                    around = strip_tags(after[max(0, mm.start() - 70):mm.end() + 70])
                    mention_examples.append((ts, clip(around, 160)))
            continue
        total += 1
        block = sm.group(0)
        if after[sm.end():].lower().startswith("<hr"):
            ended_hr += 1
        else:
            ended_end += 1
        heads = [(int(lv), clip(strip_tags(t), 70)) for lv, t in _H_RE.findall(block)]
        h2 = [t for lv, t in heads if lv == 2]
        for lv, t in heads:
            if lv == 3:
                h3[t] += 1
        for t in h2[1:]:
            extra_h2[t] += 1
        if len(h2) > 1:
            swallowed.append((ts, h2[1:4]))
        text = strip_tags(block)
        if len(text) < 400:
            short_blocks.append((ts, clip(text, 300)))

    L = ["2. СЛУЖЕБНЫЕ БЛОКИ",
         f"  карточек с блоком: {total}",
         f"    блок закончился на <hr>: {ended_hr}",
         f"    блок дошёл до конца ответа (нет <hr> после него): {ended_end}",
         f"  блоков с ЛИШНИМИ заголовками h2 (возможно, съели текст ответа): {len(swallowed)}"]
    for ts, heads in swallowed[:10]:
        L.append(f"    {ts}  {heads}")
    L.append("  заголовки h3 внутри блоков (частота):")
    for t, n in h3.most_common(15):
        L.append(f"    {n:4d}  {t}")
    L.append("  лишние h2 внутри блоков (частота):")
    for t, n in extra_h2.most_common(15):
        L.append(f"    {n:4d}  {t}")
    L.append(f"  короткие блоки (< 400 символов): {len(short_blocks)}")
    for ts, t in short_blocks[:10]:
        L.append(f"    {ts}  {t}")
    L.append(f"  упоминания «служебный файл» БЕЗ блока: {mentions}")
    for ts, t in mention_examples:
        L.append(f"    {ts}  …{t}…")
    return L


def section_time_ids(cards):
    L = ["3. ВРЕМЯ, ПОРЯДОК, ИДЕНТИФИКАТОРЫ"]
    tz = Counter()
    for c in cards:
        m = G.TS_RE.search(c["meta"].get("ts_raw", ""))
        tz[m.group(8) if (m and m.group(8)) else "(нет)"] += 1
    L.append("  названия поясов: " + ", ".join(f"{k}: {v}" for k, v in tz.most_common()))
    dts = [ts_to_datetime(c["ts"]) if c["ts"] else None for c in cards]
    known = [d for d in dts if d is not None]
    viol = [(i, d.isoformat()) for i, (p, d) in enumerate(zip(known, known[1:]), 1) if d > p]
    L.append(f"  карточек без времени: {len(cards) - len(known)}")
    L.append(f"  нарушений порядка «новые сверху»: {len(viol)}  {viol[:5]}")
    old = Counter(c["id"] for c in cards)
    new = Counter()
    for c, d in zip(cards, dts):
        if d is not None:
            new[sha(d.astimezone(timezone.utc).isoformat() + "\n" + c["prompt"])] += 1
    L.append(f"  дубликатов id по текущей формуле: {sum(v - 1 for v in old.values() if v > 1)}")
    L.append(f"  дубликатов id по формуле UTC+запрос: {sum(v - 1 for v in new.values() if v > 1)}")
    by_prompt = {}
    for c, d in zip(cards, dts):
        if d is not None:
            by_prompt.setdefault(c["prompt"], []).append(d)
    close, examples = 0, []
    for prompt, times in by_prompt.items():
        times.sort()
        for a, b in zip(times, times[1:]):
            if (b - a).total_seconds() <= 60:
                close += 1
                if len(examples) < 8:
                    examples.append((a.isoformat(), len(prompt), clip(prompt, 30)))
    L.append(f"  пар одинаковых запросов с разницей <= 60 с: {close}")
    for ex in examples:
        L.append(f"    {ex}")
    return L


def section_kinds_sizes(cards):
    L = ["4. ТИПЫ ЗАПИСЕЙ И РАЗМЕРЫ"]
    kinds = Counter(c["meta"].get("kind") for c in cards)
    for k, v in kinds.most_common(15):
        L.append(f"  {v:6d}  {k}")
    lens = [len(c["answer_md"]) for c in cards]
    if lens:
        L.append(f"  длина ответа (символы): медиана {int(statistics.median(lens))}, максимум {max(lens)}")
        L.append(f"  ответов длиннее 80000 символов: {sum(1 for n in lens if n > 80000)}")
        L.append(f"  суммарно символов в ответах: {sum(lens):,}")
    return L


def section_secrets(cards):
    L = ["5. ПОХОЖЕ НА СЕКРЕТ, НО ОСТАЛОСЬ ПОСЛЕ МАСКИРОВАНИЯ (эвристика; значения НЕ печатаются)"]
    found = []
    for c in cards:
        masked = redact_card(copy.deepcopy(c), Counter())
        for field in ("prompt", "answer_md", "service_md"):
            names = sorted({m.group(1) for m in _SECRET_LOOK.finditer(masked.get(field) or "")})
            if names:
                found.append((c["ts"] or "?", field, names))
    L.append(f"  карточек-полей с кандидатами: {len(found)}")
    for ts, field, names in found[:30]:
        L.append(f"    {ts}  {field}: {names}")
    return L


def main(argv=None):
    ap = argparse.ArgumentParser(description="Диагностика экспорта Takeout (только чтение)")
    ap.add_argument("input", help="файл экспорта, например MyActivity2.html")
    ap.add_argument("-o", "--out", default="diagnose_report.txt", help="куда записать отчёт")
    args = ap.parse_args(argv)

    raw = Path(args.input).read_text(encoding="utf-8", errors="replace")
    pieces = [p for p in G.CARD_SPLIT.split(raw) if p.startswith('<div class="outer-cell')]
    cards = list(G.iter_cards(args.input, Counter(), 0))

    L = [f"ДИАГНОСТИКА {args.input}: блоков outer-cell {len(pieces)}, "
         f"карточек разобрано {len(cards)}", ""]
    for section in (section_env(), section_blocks(pieces), section_time_ids(cards),
                    section_kinds_sizes(cards), section_secrets(cards)):
        L += section + [""]
    report = "\n".join(L)
    Path(args.out).write_text(report + "\n", encoding="utf-8")
    print(report)
    print(f"\nОтчёт записан: {Path(args.out).resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
