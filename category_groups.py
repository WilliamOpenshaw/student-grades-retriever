r'''
Grading-category families for the staff dashboard's category analysis (roadmap item 10).

Teachers name Classroom grading categories freely ("Participation & Citizenship", "Citizenship and
Participation", "C&P", "課堂表現" ...). family(name) puts each name into one broad family so categories
can be compared across classes and departments:

    Tests & summatives · Classwork & homework · Projects & presentations · Writing ·
    Participation & citizenship · Portfolio (GSP) · Mastery (mixed) · Homeroom activities ·
    No category · Other

The first matching rule wins (e.g. "Formative assessment" is classwork, "Summative Mastery" a test).

    .venv\Scripts\python.exe category_groups.py --check     # every category name in the DB and in the
                                                            # Trial Pull "Category Groups" CSV, by family
'''

import argparse
import csv
import os
import re
from collections import Counter, defaultdict

FAMILIES = ['Tests & summatives', 'Classwork & homework', 'Projects & presentations', 'Writing',
            'Participation & citizenship', 'Portfolio (GSP)', 'Mastery (mixed)', 'Homeroom activities',
            'No category', 'Other']

RULES = [
    ('No category', r'^\(?no category\)?$|^\(no graded'),
    ('Portfolio (GSP)', r'portfolio|portofolio|potfolio|portflio|\bgsps?\b|google\s?sites?|學習歷程|site construction|site set ?up'),
    ('Participation & citizenship', r'particip|partipation|citizen|citzen|\bc\s?&\s?p\b|c&d_c&p|work ethic|engagement|attendance|'
                                    r'class(room)? performance|in class performance|respect|課堂表現|課堂參與|daily records|self-progress'),
    ('Homeroom activities', r'homeroom|home room|rotat|teacher blocks|\bpol\b|\bpols\b|presentations? of learning'),
    ('Classwork & homework', r'formative|classwork|class work|homework|home learning|in-?class|worksheet|exit ticket|作業|課堂練習|'
                             r'課堂活動|課後|學習任務|隨堂|國字練習|課本|practice|tasks?\b|handed[- ]in|daily|weekly assignments|'
                             r'class assignments|classroom assignments|annotation|notes|journal|discussion|comprehension|reading|'
                             r'vocabulary|grammar|listening|speaking|drafts?\b|skill building|terms|choice ?board|labs?\b|lab report'),
    ('Classwork & homework', r'習作|甲乙本|問答題|group work|peer grading'),
    # Explicit tests first, then projects and writing (so "Final Project" / "Final Essay" / 期末報告 go
    # there), then general summative words ("Final", "Midterm", "Assessment").
    ('Tests & summatives', r'\btests?\b|exam|quiz|mock|mcq|frq|單元測驗|小考|期中考|期末考|summative'),
    ('Projects & presentations', r'project|presentation|exhibit|showcase|deliverable|prototype|proposal|pitch|venture|tour|'
                                 r'報告|gallery|poster|portfolio pieces|business canvas|marketing|brand|logo|currency|investments|'
                                 r'case|simulation|museum|magazine|speech|monologue|interview|performance|interpreting|design|sculpture|'
                                 r'drawing|\bart\b|creative|video|choropleth|model|collage|print|still life|caricature|self-portrait'),
    ('Writing', r'essay|writing|寫作|literary|paper|analysis|report|narrative|memoir|poetry|poem|book|article|script|summary|letter|dialogue'),
    ('Tests & summatives', r'summative|midterm|mid[-_ –]?term|\bfinals?\b|期中|期末|asses|major assign|unit showcase|mastery[-_ :]*quizzes'),
    ('Mastery (mixed)', r'mastery|matery'),
]
RULES = [(f, re.compile(rx, re.I)) for f, rx in RULES]


def family(name):
    n = (name or '').strip()
    if not n:
        return 'No category'
    return next((f for f, rx in RULES if rx.search(n)), 'Other')


def _check():
    from db import BASE_DIR, get_db_engine
    from sqlalchemy import text
    names = Counter()
    with get_db_engine().connect() as conn:
        for cat, n in conn.execute(text('SELECT category, COUNT(DISTINCT course_name) FROM student_grade_snapshots GROUP BY 1')):
            names[cat or ''] += n
    path = os.path.join(BASE_DIR, 'department_analysis_past_grades_csvs', 'Grade Category Trial Pull - Category Groups.csv')
    if os.path.exists(path):
        for r in csv.DictReader(open(path, encoding='utf-8-sig')):
            names[r['Category (as named in Classroom)']] += int(r['Classes Using It'] or 0)
    by = defaultdict(list)
    for n, k in names.items():
        by[family(n)].append((k, n))
    total = sum(names.values())
    for f in FAMILIES:
        items = sorted(by.get(f, []), reverse=True)
        uses = sum(k for k, _ in items)
        print(f"\n{f}: {len(items)} names, {uses} class uses ({uses * 100 / total:.0f}%)")
        print('   ' + ' · '.join(n for _, n in items[:25]) + (' …' if len(items) > 25 else ''))


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Grading-category families.')
    ap.add_argument('--check', action='store_true')
    if ap.parse_args().check:
        _check()
    else:
        ap.print_help()
