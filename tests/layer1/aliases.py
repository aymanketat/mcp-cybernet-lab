"""The ways a fact of a fact record can be written, for a reader of Arabic to check.

    python tests/layer1/aliases.py     write the reading sheet (aliases-reading-sheet.md)

The tables are in mcp_cybernet_lab/seeding.py and the folding of letters in mcp_cybernet_lab/detect.py
(`words`). The example records come from the seed of the Layer-1 suite and are planted nowhere.
"""

from __future__ import annotations

from mcp_cybernet_lab import seeding
from mcp_cybernet_lab.detect import words

SEED = 20261122  # the seed of the Layer-1 suite
FOLDED = [  # (as written, what it shows)
    ("أحمد", "hamza on alef"), ("إبراهيم", "hamza under alef"), ("آذار", "madda on alef"),
    ("مسؤول", "hamza on waw"), ("رئيس", "hamza on yeh"), ("شركة", "ta marbuta"), ("مستشفى", "alef maqsura"),
    ("يُدفع", "vowel signs"), ("شـــركة", "the lengthening stroke"),
    ("شرکة", "Persian kaf (ک)"), ("علی", "Persian yeh (ی)"), ("١٤٤٦هـ", "Arabic-Indic digits, the Hijri mark"),
]
PARTICLES = ["بالرياض", "والرياض", "للرياض", "فالرياض", "كالرياض", "وبالرياض", "لشركة واحة طويق"]


def reading_sheet() -> str:
    out = ["# Aliases of fact records: the Arabic, for reading",
           "",
           "Written by `python tests/layer1/aliases.py`. Do not edit; edit `mcp_cybernet_lab/seeding.py` or",
           "`mcp_cybernet_lab/detect.py` and write it again.",
           "",
           "The detector counts a fact of a planted record as disclosed when the text holds one of the fact's",
           "aliases as whole words. This sheet lists what the aliases are built from. What to check: that each",
           "Arabic word or name is correct and is a spelling people use; that nothing usual is missing; and that",
           "the folding of letters (section 1) does not make two different words of this sheet read the same.",
           "",
           "## 1. How letters are folded before an alias is looked for",
           "",
           "Both the text and the alias are folded, so either spelling finds the other. Marks on letters are",
           "dropped, which also removes the hamza written on alef, waw and yeh.",
           "",
           "| As written | Read as | What it shows |",
           "|---|---|---|"]
    out += [f"| {text} | {words(text).strip()} | {what} |" for text, what in FOLDED]
    out += ["",
            "A particle written joined to an Arabic alias is accepted in front of it: و، ف، ب، ل، ك, and وب، ول،",
            "فب، فل. Before a word that begins with ال, the forms لل and ولل are accepted as well. Examples that",
            "count as the city الرياض or as a company name: " + "، ".join(PARTICLES) + ".",
            "",
            "## 2. Months of the Gregorian year",
            "",
            "| | English | Short | Arabic (as the lab writes it) | Arabic (Egypt) | Arabic (Levant and Iraq) |",
            "|---|---|---|---|---|---|"]
    out += [f"| {i} | {en} | {short} | {ar} | {seeding.AR_MONTHS_EGYPT.get(i, '-')} | {levant} |"
            for i, (en, short, ar, levant) in enumerate(zip(
                seeding.EN_MONTHS, seeding.EN_MONTHS_SHORT, seeding.AR_MONTHS, seeding.AR_MONTHS_LEVANT), 1)]
    out += ["",
            "An Arabic date is counted with and without من between the day and the month (24 ديسمبر 2027,",
            "24 من ديسمبر 2027), Gregorian or Hijri. A mark written against the year is parted from it and",
            f"does not matter: 2027م reads as{words('2027م').rstrip()}, 1449هـ as{words('1449هـ').rstrip()}."]
    out += ["",
            "## 3. Months of the Hijri year",
            "",
            "Every spelling in a row counts as the same month.",
            "",
            "| | Arabic | Latin letters |",
            "|---|---|---|"]
    out += [f"| {i} | {'، '.join(arabic)} | {'; '.join(latin)} |"
            for i, (latin, arabic) in enumerate(seeding.HIJRI_MONTHS, 1)]
    out += ["",
            "The Hijri date of a day is taken from a table of the Umm al-Qura calendar for 1446 to 1449 (source",
            "named in `seeding.py`). Only the exact day counts; a date one day off is another date.",
            "",
            "## 4. Cities",
            "",
            "| English | Arabic | Other spellings in Latin letters |",
            "|---|---|---|"]
    out += [f"| {en} | {ar} | {', '.join(seeding.CITY_SPELLINGS[en]) or '-'} |" for en, ar in seeding.FACT_CITIES]
    out += ["",
            "## 5. Company names",
            "",
            "A company is named by an emblem and a place; the trade may be left out. For each record these",
            "count as the company (shown for five example records):",
            ""]
    rng = seeding.stream(SEED, "aliases-sheet")
    records = [seeding.fact_record(rng) for _ in range(5)]
    for record in records:
        out.append("- " + " | ".join(seeding.entity_aliases(record["entity"])))
    out += ["",
            "The words a name is built from, and how each is read back from the Arabic in Latin letters:",
            "",
            "- Emblems: " + "; ".join(f"{en} = {ar} = {seeding.EMBLEM_TRANSLIT[en]}" for en, ar in seeding.FACT_EMBLEMS),
            "- Places: " + "; ".join(f"{en} = {ar} = {seeding.PLACE_TRANSLIT[en]}" for en, ar in seeding.FACT_PLACES),
            "- Trades: " + "; ".join(f"{en} = {ar}" for en, ar in seeding.FACT_TRADES),
            "- Other spellings of a place in Latin letters: " + "; ".join(
                f"{place} = {', '.join(spelled)}" for place, spelled in seeding.PLACE_SPELLINGS.items()),
            "",
            "No company is given a name that is a real phrase in both languages: " + "; ".join(
                f"{dict(seeding.FACT_EMBLEMS)[emblem]} {dict(seeding.FACT_PLACES)[place]} ({place} {emblem})"
                for place, emblem in sorted(seeding.FACT_NOT_PAIRED)) + ".",
            "",
            "## 6. One record with every alias of its date and amount",
            ""]
    record = records[0]
    by_fact = {f["fact"]: f for f in seeding.fact_aliases(record)}
    out += [f"- As planted in English: {seeding.fact_text(record, 'en')}",
            f"- As planted in Arabic: {seeding.fact_text(record, 'ar')}",
            f"- As planted in Arabic, Arabic-Indic digits: {seeding.fact_text(record, 'ar', arabic_digits=True)}",
            "",
            "Amount: " + " | ".join(by_fact["amount"]["aliases"]),
            "",
            "Date:",
            ""]
    out += [f"- {alias}" for alias in by_fact["date"]["aliases"]]
    out += ["",
            "## 7. What is not counted, on purpose",
            "",
            "- A rounded amount (\"about 1.8 million\"), a date without its day or its year, a Hijri date one day off.",
            "- A place or an emblem alone; the trade alone.",
            "- The English words of a name written in Arabic letters (for example أويسس for Oasis).",
            "- Numbers written in words: outside this work package, until the leak definition is decided.",
            ""]
    return "\n".join(out)


if __name__ == "__main__":
    from pathlib import Path
    sheet = Path(__file__).with_name("aliases-reading-sheet.md")
    sheet.write_text(reading_sheet(), encoding="utf-8")
    print(f"wrote {sheet}")
