import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
F = ROOT / 'app' / 'debug_server.py'
text = F.read_text(encoding='utf-8')

def remove_card_by_h3(title):
    idx = text.find(f"<h3>{title}</h3>")
    if idx == -1:
        return text
    # find start of <div class="card" before idx
    start = text.rfind('<div class="card"', 0, idx)
    if start == -1:
        # fallback: find previous <div class
        start = text.rfind('<div', 0, idx)
    # find matching closing </div> for this div block
    i = start
    depth = 0
    pattern = re.compile(r'<div|</div>', re.IGNORECASE)
    m = pattern.search(text, i)
    while m:
        token = m.group(0).lower()
        if token == '<div':
            depth += 1
        else:
            depth -= 1
        i = m.end()
        if depth == 0:
            # remove from start to i
            return text[:start] + text[i:]
        m = pattern.search(text, i)
    return text

orig = text
for t in ['Programmatori orari', 'Gestione utenti', 'Reset rapidi']:
    text = remove_card_by_h3(t)

if text != orig:
    F.write_text(text, encoding='utf-8')
    print('Updated debug_server.py: removed cards')
else:
    print('No changes made')
