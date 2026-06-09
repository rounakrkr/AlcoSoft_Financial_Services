import requests
import re

s = requests.Session()
r = s.get('http://localhost:5000/login')
match = re.search(r'name="csrf_token"\s+value="([^"]+)"', r.text)
if not match:
    print('No CSRF found on login page')
    print(r.text[:500])
    exit(1)

csrf = match.group(1)
print('CSRF:', csrf)

r2 = s.post('http://localhost:5000/login', data={
    'username': 'rounakrkr', 
    'password': 'rounak@1982', 
    'csrf_token': csrf
})
print('Login Status:', r2.status_code)

r3 = s.get('http://localhost:5000/api/status')
print('Trading State Before:', r3.json().get('trading_state'))

match2 = re.search(r'name="csrf-token"\s+content="([^"]+)"', s.get('http://localhost:5000/').text)
if match2:
    csrf2 = match2.group(1)
    r4 = s.post('http://localhost:5000/api/emergency-squareoff', json={'confirm_action': 'SQUARE_OFF'}, headers={'X-CSRFToken': csrf2})
    print('Squareoff Result:', r4.json())

    r5 = s.get('http://localhost:5000/api/status')
    print('Trading State After:', r5.json().get('trading_state'))
else:
    print("Could not find CSRF on index")
