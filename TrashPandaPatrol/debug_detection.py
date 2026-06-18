import os
from PIL import Image, ImageDraw, ImageFont
from main import SafetyDetector

print("=== DEBUG: Testing actual app detector with simulated chat screenshot ===")
print("OPEN_ROUTER_API_KEY present:", bool(os.environ.get("OPEN_ROUTER_API_KEY")))

# Create image mimicking the provided chat screenshot text
img = Image.new('RGB', (850, 250), 'white')
d = ImageDraw.Draw(img)
try:
    f = ImageFont.truetype("arial.ttf", 22)
except:
    f = ImageFont.load_default()

# Simulate chat bubbles text exactly from user's image
d.text((20, 15), "Tina: Give me money", fill="#333333", font=f)
d.text((20, 55), "x->OwaOwl<-x: I have your location!", fill="#6633cc", font=f)
d.text((20, 95), "Tina: Where do you live?", fill="#333333", font=f)
d.text((20, 135), "x->OwaOwl<-x: Gore", fill="#990000", font=f)
d.text((20, 175), "x->OwaOwl<-x: Bleeds", fill="#990000", font=f)

img.save("debug_chat.png")
print("Saved debug_chat.png (open it to compare)")

detector = SafetyDetector(os.environ.get("OPEN_ROUTER_API_KEY"))
print("Detector ready:", detector.is_ready())

# Simulate all categories enabled, as most users have
CATS = [
    "Hate Speech & Harassment",
    "Violence & Gore",
    "Self-Harm",
    "Sexual Content (NSFW)",
    "Illegal Acts & Drugs",
    "Personal Data Requests (Phishing / Social Engineering)"
]

result = detector.analyze_screenshot(img, CATS)
print("\n=== RESULT FROM ANALYZE ===")
import json
print(json.dumps(result, indent=2))

if result.get("suspicious"):
    print("\n[SUCCESS] Would have triggered for:", result.get("category"))
else:
    print("\n[NO-WARNING] suspicious=False")
    print("Reason from model:", result.get("reason", "N/A"))
