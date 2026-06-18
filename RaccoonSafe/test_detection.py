import os
import base64
from io import BytesIO

import requests
from PIL import Image, ImageDraw, ImageFont

API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "google/gemini-2.0-flash-001"
API_KEY = os.environ["OPEN_ROUTER_API_KEY"]

# Recreate the messages the user saw
img = Image.new('RGB', (800, 160), 'white')
d = ImageDraw.Draw(img)
try:
    f = ImageFont.truetype("arial.ttf", 28)
except Exception:
    f = ImageFont.load_default()

d.text((40, 30), "where do you live?", fill="black", font=f)
d.text((40, 80), "give me money ill give you rubux", fill="black", font=f)

buf = BytesIO()
img.save(buf, format="JPEG", quality=82)
data_url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

prompt = """You are an expert child internet safety AI called \"TrashPandaPatrol\".
Analyze the attached screenshot.
Return ONLY a compact JSON (no extra text at all) that matches this schema exactly:

{
  "suspicious": true or false,
  "category": "Personal Data Requests (Phishing / Social Engineering)" or null,
  "warning_message": "short friendly warning sentence for a kid (or empty)"
}

The text is very close to known examples: asking location, and robux scam offer.
"""

body = {
    "model": MODEL,
    "messages": [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ],
    "response_format": {"type": "json_object"},
    "temperature": 0,
}

resp = requests.post(
    API_URL,
    headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
    json=body,
    timeout=60,
)
print("OPENROUTER RESULT:")
print(resp.json()["choices"][0]["message"]["content"])
