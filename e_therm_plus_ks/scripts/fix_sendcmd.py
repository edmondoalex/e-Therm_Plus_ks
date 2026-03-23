import re
from pathlib import Path
p=Path(r"\\192.168.3.24\addons\e-Therm_Plus_ks\app\debug_server.py")
s=p.read_text(encoding='utf-8')

# reuse same safe replacement approach to handle f-strings

def replace_func(name, new_body):
    pat = re.compile(r'(?P<prefix>(?:async\s+)?function\s+'+re.escape(name)+r'\([^)]*\)\s*\{)(?P<body>[\s\S]*?)(?P<suffix>\n\s*\})', re.M)
    out=[]
    last=0
    for m in pat.finditer(s):
        start=m.start()
        q1 = s.rfind('f"""', 0, start)
        q2 = s.rfind("f'''", 0, start)
        q3 = s.rfind('"""', 0, start)
        q4 = s.rfind("'''", 0, start)
        is_fstring=False
        if q1!=-1 and q1>q2 and q1>q3 and q1>q4:
            is_fstring=True
        elif q2!=-1 and q2>q3 and q2>q4:
            is_fstring=True
        # prepare body
        body=new_body
        if is_fstring:
            body = body.replace('{','{{').replace('}','}}')
        out.append(s[last:m.start()])
        out.append(m.group('prefix')+"\n"+body+"\n"+m.group('suffix'))
        last=m.end()
    out.append(s[last:])
    return ''.join(out)

# unified sendCmd JS implementation (avoid ${} template expressions to reduce f-string conflicts)
new_send = '''
// Unified sendCmd: accepts (type,id,action,value) or a single payload object
async function sendCmd() {
  const args = Array.from(arguments);
  let payload = {};
  if (args.length === 1 && typeof args[0] === 'object') {
    payload = args[0];
  } else {
    payload.type = String(args[0] || '');
    payload.id = Number(args[1] || 0);
    payload.action = String(args[2] || '');
    if (args.length > 3) payload.value = args[3];
  }
  const status = document.getElementById('status');
  try {
    if (status) status.innerText = 'Invio comando: ' + payload.type + '/' + payload.id + ' ' + payload.action;
    const res = await fetch('./api/cmd', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const text = await res.text();
    if (status) status.innerText = res.ok ? ('OK: ' + text) : ('ERR(' + res.status + '): ' + text);
  } catch (e) {
    if (status) status.innerText = 'ERR: ' + e;
  }
}
'''

s = replace_func('sendCmd', new_send)

p.write_text(s, encoding='utf-8')
print('Replaced sendCmd definitions safely')
