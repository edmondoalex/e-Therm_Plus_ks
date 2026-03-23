import re
from pathlib import Path
p=Path(r"\\192.168.3.24\addons\e-Therm_Plus_ks\app\debug_server.py")
s=p.read_text(encoding='utf-8')
backup = p.with_suffix(p.suffix + '.safe.bak')
backup.write_text(s, encoding='utf-8')
print('Backup written to', backup)

replacements = {}
replacements['getPinSessionToken'] = "try {{ return null; }} catch (_e) {{ return null; }}"
replacements['startPinSession'] = "throw new Error('PIN disabled');"
replacements['ensurePinSession'] = "return null;"
replacements['sendCmd'] = (
"// Unified sendCmd: accepts (type,id,action,value) or a single payload object\n"
"async function sendCmd() {{\n"
"  const args = Array.from(arguments);\n"
"  let payload = {};\n"
"  if (args.length === 1 && typeof args[0] === 'object') {{ payload = args[0]; }} else {{\n"
"    payload.type = String(args[0] || '');\n"
"    payload.id = Number(args[1] || 0);\n"
"    payload.action = String(args[2] || '');\n"
"    if (args.length > 3) payload.value = args[3];\n"
"  }}\n"
"  const status = document.getElementById('status');\n"
"  try {{\n"
"    if (status) status.innerText = 'Invio comando: ' + payload.type + '/' + payload.id + ' ' + payload.action;\n"
"    const res = await fetch('./api/cmd', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify(payload) }});\n"
"    const text = await res.text();\n"
"    if (status) status.innerText = res.ok ? ('OK: ' + text) : ('ERR(' + res.status + '): ' + text);\n"
"  }} catch (e) {{\n"
"    if (status) status.innerText = 'ERR: ' + e;\n"
"  }}\n"
"}}"
)

# ensure braces are doubled (to be safe inside f-strings)
for k in replacements:
    replacements[k] = replacements[k].replace('{','{{').replace('}','}}')

# pattern to replace function definitions
pattern = re.compile(r"(?P<prefix>(?:async\\s+)?function\\s+(?P<name>\w+)\\([^)]*\\)\\s*\\{)(?P<body>[\\s\\S]*?)(?P<suffix>\\n\\s*\\})", re.M)

last = 0
out = []
for m in pattern.finditer(s):
    name = m.group('name')
    out.append(s[last:m.start()])
    if name in replacements:
        out.append(m.group('prefix') + "\n" + replacements[name] + "\n" + m.group('suffix'))
        print('Replaced', name)
    else:
        out.append(m.group(0))
    last = m.end()
out.append(s[last:])
new = ''.join(out)

p.write_text(new, encoding='utf-8')
print('Applied replacements')
