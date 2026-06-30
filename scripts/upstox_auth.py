import os
import urllib.parse
import webbrowser
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.environ.get("UPSTOX_API_KEY", "").strip("'\" ")
CLIENT_SECRET = os.environ.get("UPSTOX_API_SECRET", "").strip("'\" ")
REDIRECT_URI = os.environ.get("UPSTOX_REDIRECT_URI", "").strip("'\" ")

class AuthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        query_components = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if 'code' in query_components:
            self.server.auth_code = query_components['code'][0]
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b"<html><body><h1>Authentication successful!</h1><p>You can close this tab and return to the terminal.</p></body></html>")
        else:
            self.send_response(400)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b"<html><body><h1>Authentication failed!</h1><p>No code found in URL.</p></body></html>")
    
    def log_message(self, format, *args):
        pass

def main():
    if not CLIENT_ID or not CLIENT_SECRET or not REDIRECT_URI:
        print("Missing Upstox credentials in .env")
        return

    auth_url = f"https://api.upstox.com/v2/login/authorization/dialog?response_type=code&client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}"
    
    print(f"Opening browser for login: {auth_url}")
    webbrowser.open(auth_url)

    port = int(urllib.parse.urlparse(REDIRECT_URI).port or 80)
    server = HTTPServer(('127.0.0.1', port), AuthHandler)
    server.auth_code = None

    print(f"Waiting for authorization callback on port {port}...")
    while server.auth_code is None:
        server.handle_request()

    print("\nGot auth code, exchanging for access token...")
    
    token_url = "https://api.upstox.com/v2/login/authorization/token"
    headers = {
        'accept': 'application/json',
        'Content-Type': 'application/x-www-form-urlencoded',
    }
    data = {
        'code': server.auth_code,
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'redirect_uri': REDIRECT_URI,
        'grant_type': 'authorization_code',
    }

    response = requests.post(token_url, headers=headers, data=data)
    if response.status_code == 200:
        token = response.json().get('access_token')
        print(f"\n✅ SUCCESS! Access Token retrieved.")
        
        env_file = ".env"
        content = ""
        if os.path.exists(env_file):
            with open(env_file, "r", encoding="utf-8") as f:
                content = f.read()
        
        import re
        if "UPSTOX_ACCESS_TOKEN=" in content:
            content = re.sub(r'UPSTOX_ACCESS_TOKEN=.*', f'UPSTOX_ACCESS_TOKEN="{token}"', content)
        else:
            if not content.endswith('\n'): content += '\n'
            content += f'UPSTOX_ACCESS_TOKEN="{token}"\n'
            
        with open(env_file, "w", encoding="utf-8") as f:
            f.write(content)
            
        print("Token saved to .env as UPSTOX_ACCESS_TOKEN.")
    else:
        print(f"Failed to get token: {response.text}")

if __name__ == "__main__":
    main()
