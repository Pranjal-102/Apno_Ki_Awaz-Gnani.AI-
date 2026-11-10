import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("GNANI_API_KEY")

# Use the file name given in the command, otherwise hello.wav
audio_path = sys.argv[1] if len(sys.argv) > 1 else "hello.wav"

url = "https://api.vachana.ai/stt/v3"
headers = {"X-API-Key-ID": API_KEY}

data = {
    "model": "prisma-v3.0",
    "language_code": "hi-IN,en-IN",   # Hindi + English hints, good for Hinglish
    "format": "transcribe",           # numbers written as digits
}

with open(audio_path, "rb") as f:
    files = {"audio_file": (os.path.basename(audio_path), f)}
    response = requests.post(url, headers=headers, files=files, data=data, timeout=60)

print("Status:", response.status_code)
print(response.json())