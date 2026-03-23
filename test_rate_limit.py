import requests

URL = "http://127.0.0.1:8000/api/v1/accounts/login/"

for i in range(1, 56):
    response = requests.get(URL)
    print(f"Request {i}: {response.status_code}")
    if response.status_code == 429:
        print("Rate limit hit!")
        break