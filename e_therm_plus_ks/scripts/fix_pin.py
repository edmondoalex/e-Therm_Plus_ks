import re
from pathlib import Path
p=Path(r"\\192.168.3.24\addons\e-Therm_Plus_ks\app\debug_server.py")
s=p.read_text(encoding='utf-8')

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
        qpos=-1
        if q1!=-1 and q1>q2 and q1>q3 and q1>q4:
            is_fstring=True; qpos=q1
        elif q2!=-1 and q2>q3 and q2>q4:
            is_fstring=True; qpos=q2
        else:
            qpos = max(q3,q4)
            if qpos!=-1:
                close = s.find('"""', qpos+3) if q3>q4 else s.find("'''", qpos+3)
                if close!=-1 and close>start:
                    is_fstring=False
                else:
                    qpos=-1
        body=new_body
        if is_fstring:
            body = body.replace('{','{{').replace('}','}}')
        out.append(s[last:m.start()])
        out.append(m.group('prefix')+"\n"+body+"\n"+m.group('suffix'))
        last=m.end()
    out.append(s[last:])
    return ''.join(out)

new_get = """try { return null; } catch (_e) { return null; }"""
new_start = """throw new Error('PIN disabled');"""
new_ensure = """return null;"""

s = replace_func('getPinSessionToken', new_get)
s = replace_func('startPinSession', new_start)
s = replace_func('ensurePinSession', new_ensure)

p.write_text(s, encoding='utf-8')
print('Replaced PIN functions safely')
