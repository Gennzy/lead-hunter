
import requests, re
s = requests.Session()
r = s.get('http://localhost:8000/login')
csrf = re.search(r'csrf_token.*?value="(.*?)"', r.text)
csrf = csrf.group(1) if csrf else ''
r = s.post('http://localhost:8000/login', data={'username':'admin','password':'admin','csrf_token':csrf}, allow_redirects=False)
for p in ['/leads/1527', '/billing', '/kanban', '/analytics']:
    r = s.get('http://localhost:8000' + p)
    print(f'{p}: {r.status_code}')
