from pathlib import Path
import re
p=Path(r'\\\\192.168.3.24\\addons\\e-Therm_Plus_ks\\app\\debug_server.py')
s=p.read_text(encoding='utf-8')
out=[]
lines=s.splitlines(True)
i=0
n=len(lines)
while i<n:
    line=lines[i]
    m=re.match(r'^(def\s+render_security_[a-zA-Z0-9_]+\s*\(.*)$', line)
    if m:
        name = m.group(1).split('(')[0].split()[1]
        # write a small stub function replacing the entire original function
        stub = f"def {name}(*args, **kwargs):\n    return (200, '<html><body><h3>Rimosso</h3></body></html>')\n"
        out.append(stub)
        # skip until next top-level def or EOF
        i+=1
        while i<n and not re.match(r'^def\s+\w+', lines[i]):
            i+=1
        continue
    out.append(line)
    i+=1
new=''.join(out)
if new==s:
    print('no changes')
else:
    p.write_text(new, encoding='utf-8')
    print('stubbed')
