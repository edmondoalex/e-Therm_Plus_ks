from pathlib import Path
import re
ROOT = Path(__file__).resolve().parent.parent
WL = ROOT / 'worklog.md'
text = WL.read_text(encoding='utf-8')
# Split into entries by headers that start with '## '
parts = re.split(r'(?m)^##\s+', text)
if len(parts) <= 1:
    print('No entries found or already minimal')
    raise SystemExit(0)
# The first part is header preface
preface = parts[0].rstrip() + '\n\n'
entries = []
seen = set()
for p in parts[1:]:
    header_line, *body = p.splitlines()
    header = header_line.strip()
    body_text = '\n'.join(body).strip()
    key = (header, body_text)
    if key in seen:
        # skip duplicate
        continue
    seen.add(key)
    entries.append((header, body_text))
# Rebuild file with deduped entries preserving order
out = preface
for header, body in entries:
    out += '## ' + header + '\n'
    if body:
        out += body + '\n'
    out += '\n'
WL.write_text(out, encoding='utf-8')
print('Cleaned worklog entries: deduplicated')
