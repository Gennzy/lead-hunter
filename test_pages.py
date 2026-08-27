
import requests
s = requests.Session()
# Get CSRF
r = s.get('http://localhost:8000/login')
import re
csrf = re.search(r'csrf_token.*?value="(.*?)"', r.text)
csrf = csrf.group(1) if csrf else ''
# Login
r = s.post('http://localhost:8000/login', data={'username':'admin','password':'admin','csrf_token':csrf}, allow_redirects=False)
print('Login:', r.status_code, r.headers.get('location',''))
# Test pages
for p in ['/leads/1527', '/billing', '/kanban']:
    r = s.get('http://localhost:8000' + p)
    print(f'{p}: {r.status_code}')
