import os
import requests
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("GNANI_API_KEY")

url = "https://api.vachana.ai/api/v1/tts/inference"

headers = {
    "Content-Type": "application/json",
    "X-API-Key-ID": API_KEY,
}

payload = {
    "text": "नमस्ते दादी, आपकी दवा का समय हो गया है।",
    "voice": "Nalini",
    "model": "timbre-v2.5",
    "language": "hi-IN",
    "speed": 1.0,
    "audio_config": {
        "sample_rate": 48000,
        "num_channels": 1,
        "sample_width": 2,
        "encoding": "linear_pcm",
        "container": "wav",
    },
}

response = requests.post(url, headers=headers, json=payload, timeout=60)
print("Status:", response.status_code)

if response.status_code == 200:
    with open("hello.wav", "wb") as f:
        f.write(response.content)
    print("Saved hello.wav, play it now!")
    os.startfile("hello.wav")  # opens the audio on Windows
else:
    print(response.text)