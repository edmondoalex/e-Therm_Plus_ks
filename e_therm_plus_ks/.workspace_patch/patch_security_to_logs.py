from pathlib import Path
import re
p=Path(r'\\\\192.168.3.24\\addons\\e-Therm_Plus_ks\\app\\debug_server.py')
s= p.read_text(encoding='utf-8')
orig = s
# 1) Replace multiple security handlers block with a redirect to /logs
start_marker = 'if path in ("/security", "/security/"):'
end_marker = 'if path in ("/", "/index_debug", "/index_debug/"):'
si = s.find(start_marker)
ei = s.find(end_marker)
if si!=-1 and ei!=-1 and si<ei:
    new_block = """if path.startswith('/security') or path in ('/timers','/timers/'):
            try:
                print(f"[INFO] UI GET {path} (security/timers) redirected to /logs from {getattr(self.client_address,0,'')}")
            except Exception:
                pass
            self.send_response(302)
            self.send_header('Location', f'{ingress_prefix}/logs' if ingress_prefix else '/logs')
            self.end_headers()
            return
"""
    s = s[:si] + new_block + s[ei:]
# 2) Remove specific <a class="item" anchors that point to security outputs, scenarios, timers, reset, info, users
# remove anchors with href containing '/security' but keep '/logs' and keep '/thermostats'
s = re.sub(r'<a\\s+class="item"\\s+href="(?:/|)security[^\"]*"[\\s\\S]*?</a>\\s*', '', s, flags=re.IGNORECASE)
# remove tab links to /security/... in topbars
s = re.sub(r'<a\\s+class="tab"\\s+href="/security[^\"]*">[\\s\\S]*?</a>\\s*', '', s, flags=re.IGNORECASE)
# replace sticky bar /security -> /logs
s = s.replace('href="/security"', 'href="/logs"')
# also change occurrences of href="security" to href="logs"
s = s.replace('href="security"', 'href="logs"')
# ensure that the main menu at top (render_menu) includes only Registro Eventi
s = re.sub(r'(\\n\\s*<div class="list">)[\\s\\S]*?(</div>\\n\\s*</div>\\n\\s*</body>)', '\\\\n      <div class="list">\\\\n        <a class="item" href="/logs">\\\\n          <div class="left">\\\\n            <div class="icon">\\\\n              <svg width="22" height="22" viewBox="0 0 24 24" fill="none">\\\\n                <path d="M6 4h9l3 3v13H6z" stroke="currentColor" stroke-width="1.6"/>\\\\n                <path d="M9 10h6M9 14h6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>\\\\n              </svg>\\\\n            </div>\\\\n            <div>\\\\n              <div class="name">Registro eventi</div>\\\\n              <div class="meta">Log completo</div>\\\\n            </div>\\\\n          </div>\\\\n          <svg class="chev" viewBox="0 0 24 24" fill="none">\\\\n            <path d="M9 6l6 6-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>\\\\n          </svg>\\\\n        </a>\\\\n      </div>\\\\n    </div>\\\\n  </body>', s, flags=re.IGNORECASE)

if s==orig:
    print('no changes')
else:
    p.write_text(s, encoding='utf-8')
    print('patched')
