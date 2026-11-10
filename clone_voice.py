import os
import sys
import json
import requests
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("GNANI_API_KEY")
HEADERS = {"X-API-Key-ID": API_KEY}
BASE = "https://api.vachana.ai"

audio_path = sys.argv[1] if len(sys.argv) > 1 else "myvoice_long.m4a"

# ---- Part A: create the voice embedding (do this once per voice) ----
print("Creating voice embedding...")
with open(audio_path, "rb") as f:
    r = requests.post(
        f"{BASE}/api/v1/tts/voice-clone/embeddings",
        headers=HEADERS,
        files={"audio_file": (os.path.basename(audio_path), f)},
        timeout=120,
    )
print("Embedding status:", r.status_code)
if r.status_code != 200:
    print(r.text)
    sys.exit(1)

embedding = r.json()["data"]["voice_clone_embedding"]
with open("voice_embedding.json", "w") as f:
    json.dump(embedding, f)
print("Saved voice_embedding.json | shape:", embedding["shape"])

# ---- Part B: speak a reminder in the cloned voice ----
payload = {
    "text": "दादी, रात के सात बजे हैं। अब अपनी बी पी की गोली ले लीजिए।",
    "model": "vachana-vc-v1",
    "speaker_embedding": embedding,
    "audio_config": {
        "sample_rate": 44100,
        "num_channels": 1,
        "sample_width": 2,
        "encoding": "linear_pcm",
        "container": "wav",
    },
}

print("Generating cloned speech...")
r = requests.post(
    f"{BASE}/api/v1/tts/inference",
    headers=HEADERS,
    json=payload,
    timeout=120,
)
print("TTS status:", r.status_code)

if r.status_code == 200:
    with open("cloned_hello.wav", "wb") as f:
        f.write(r.content)
    print("Saved cloned_hello.wav, playing now")
    os.startfile("cloned_hello.wav")
else:
    print(r.text)