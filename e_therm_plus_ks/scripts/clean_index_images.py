from pathlib import Path
import re
ROOT = Path(__file__).resolve().parent.parent
F = ROOT / 'app' / 'debug_server.py'
text = F.read_text(encoding='utf-8')
start_pat = 'def render_index(snapshot):'
end_pat = '\ndef def render_index_tag_styles'  # unlikely
# find the actual next def after render_index
start = text.find(start_pat)
if start == -1:
    print('render_index not found')
    raise SystemExit(1)
# find the next 'def ' after start+1
next_def = text.find('\ndef ', start+1)
# we need the correct next def for render_index; to be safe, search for 'def render_index_tag_styles' specifically
next_tag = text.find('def render_index_tag_styles', start+1)
if next_tag != -1:
    end = next_tag
else:
    # fallback: use next_def
    end = next_def if next_def != -1 else len(text)
block = text[start:end]
# remove <img ... e-safe_scr ...> and also occurrences of img with 'eTherm' if needed
new_block = re.sub(r"<img[^>]*e-safe_scr[^>]*>\s*", '', block)
# remove any lines containing 'Foto' (case-insensitive)
new_block = re.sub(r"(?im)^.*\bFoto\b.*$\n", '', new_block)
if new_block != block:
    new_text = text[:start] + new_block + text[end:]
    F.write_text(new_text, encoding='utf-8')
    print('Cleaned images in render_index')
else:
    print('No images removed')
