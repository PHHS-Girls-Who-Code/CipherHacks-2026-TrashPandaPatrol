"""
TrashPandaPatrol - Child Cybersecurity Safety App
Parent-controlled Windows monitoring app for kids 16 and under.
Uses an OpenRouter vision model to analyze screenshots.
"""
import os
import sys
import json
import time
import hashlib
import threading
import tempfile
import base64
import logging
import logging.handlers
import subprocess
import smtplib
import ssl
from email.message import EmailMessage
from datetime import datetime
from io import BytesIO

import tkinter as tk
from tkinter import messagebox, simpledialog

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFont, ImageTk
import mss
try:
    import requests
except ImportError:
    requests = None

import pystray
from pystray import MenuItem as item

# ------------- Constants and Paths -------------
APP_NAME = "TrashPandaPatrol"
APPDATA = os.path.join(os.getenv("APPDATA", os.path.expanduser("~")), APP_NAME)
os.makedirs(APPDATA, exist_ok=True)
CONFIG_FILE = os.path.join(APPDATA, "settings.json")
SCREENSHOTS_DIR = os.path.join(APPDATA, "screenshots")
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
LOG_FILE = os.path.join(APPDATA, "trashpanda.log")
RACOON_IMAGE_PATH = os.path.join(os.path.dirname(__file__), "assets", "raccoon.png")  # optional external

# ------------- Cost / capture tuning -------------
# Downscale the screenshot's long edge to this many pixels before sending to the LLM.
# ~1536 keeps small chat text legible while keeping the image to a low, predictable
# number of vision "tiles" (cost is driven by resolution -> tokens, not file bytes).
LLM_MAX_EDGE = 1536
# JPEG quality for the LLM payload. High-contrast on-screen text is unaffected at ~82,
# but the payload shrinks dramatically vs PNG.
LLM_JPEG_QUALITY = 82
# Frame-diff: if a downscaled grayscale thumbnail is nearly identical to the previous
# scan, skip the (paid) LLM call. Mean per-pixel difference threshold (0-255).
FRAME_DIFF_THRESHOLD = 4.0
FRAME_DIFF_THUMB = 64  # thumbnail edge used for cheap comparison

# ------------- OpenRouter (LLM) -------------
# OpenRouter exposes an OpenAI-compatible chat-completions API. We send the screenshot
# as a base64 data URL image part. Pick any vision-capable model slug here.
# Using a FREE vision model for the demo ($0). Has rate limits but our frame-diff
# skip keeps call volume low. Swap to "google/gemini-2.0-flash-001" (paid,
# extremely cheap) if you hit free-tier rate limits or want more reliability.
# NOTE: free model slugs on OpenRouter change/retire over time. If you get an
# HTTP 404 "No endpoints found", check https://openrouter.ai/models for a current
# free vision model and update OPENROUTER_MODEL below.
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "google/gemma-4-31b-it:free"
# Fallback vision models tried (in order) when the primary returns HTTP 429
# (rate-limited upstream). All free; the shared free pool throttles aggressively,
# so trying a second/third model often gets a result without paying anything.
# Put a paid model here (e.g. "google/gemini-2.0-flash-001") if you add credits and
# want guaranteed availability.
OPENROUTER_FALLBACK_MODELS = [
    "nvidia/nemotron-nano-12b-v2-vl:free",
    "google/gemma-4-26b-a4b-it:free",
]
# How many times to retry a single model on a 429 before moving to the next model.
OPENROUTER_MAX_RETRIES = 2
OPENROUTER_RETRY_BASE_SLEEP = 3  # seconds; multiplied by attempt number (linear backoff)



# ------------- Logging -------------
logger = logging.getLogger(APP_NAME)


def setup_logging():
    """
    Configure logging to a rotating file in AppData (works even in the windowed
    .exe build where there is no console). Also keep console output when one exists.
    Existing print(...) calls are routed through the logger so all messages persist.
    """
    logger.setLevel(logging.INFO)
    logger.propagate = False

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    # Rotating file handler: 1 MB x 3 backups keeps the log small but useful.
    try:
        fh = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass

    # Console handler only if a real stdout exists (running from source / console build).
    if sys.stdout is not None:
        try:
            ch = logging.StreamHandler(sys.stdout)
            ch.setFormatter(fmt)
            logger.addHandler(ch)
        except Exception:
            pass

    # Route existing print(...) calls in this module through the logger so nothing
    # is lost in the windowed .exe. Returns the original print for anything that needs it.
    import builtins
    _orig_print = builtins.print

    def _logged_print(*args, **kwargs):
        try:
            msg = " ".join(str(a) for a in args)
            logger.info(msg)
        except Exception:
            try:
                _orig_print(*args, **kwargs)
            except Exception:
                pass

    builtins.print = _logged_print
    logger.info("==== %s logging started ====", APP_NAME)

CATEGORIES = [
    "Hate Speech & Harassment",
    "Violence & Gore",
    "Self-Harm",
    "Sexual Content (NSFW)",
    "Illegal Acts & Drugs",
    "Personal Data Requests (Phishing / Social Engineering)"
]

DEFAULT_SETTINGS = {
    "password_hash": None,

    "screen_monitoring_enabled": False,
    "phone_notifications_enabled": False,
    "enabled_categories": {cat: True for cat in CATEGORIES},
    "monitor_interval_seconds": 35,

    # ---- Free email alerts via Gmail SMTP ----
    # gmail_address  : the Gmail account that SENDS the alert (the "from")
    # gmail_app_password : a 16-char Google "App Password" (NOT your normal password)
    # parent_email   : where the alert is delivered (the "to"); can be the same Gmail
    "gmail_address": "",
    "gmail_app_password": "",
    "parent_email": "",
}

# ------------- Config Class -------------
class ConfigManager:
    def __init__(self):
        self.settings = DEFAULT_SETTINGS.copy()
        self.load()

    def load(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                self.settings.update({k: v for k, v in loaded.items() if k in self.settings})
                # Ensure enabled_categories structure correct
                if "enabled_categories" in loaded:
                    self.settings["enabled_categories"] = {
                        c: loaded["enabled_categories"].get(c, True) for c in CATEGORIES
                    }
            except Exception:
                pass

    def save(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print("Save error:", e)

    def get(self, key, default=None):
        return self.settings.get(key, default)

    def set(self, key, value):
        self.settings[key] = value
        self.save()

    def set_category(self, cat, enabled):
        if cat in self.settings["enabled_categories"]:
            self.settings["enabled_categories"][cat] = enabled
            self.save()

    def update(self, new_dict):
        self.settings.update(new_dict)
        self.save()

    def is_first_run(self):
        return self.settings.get("password_hash") is None

    @staticmethod
    def hash_password(password: str) -> str:
        # PBKDF2 simple, but for local, use sha256 + salt
        salt = "trashpandapatrol-salt-2026"
        return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000).hex()

    def verify_password(self, password: str) -> bool:
        if not password or not self.settings["password_hash"]:
            return False
        return self.hash_password(password) == self.settings["password_hash"]

    def set_password(self, new_password: str):
        self.settings["password_hash"] = self.hash_password(new_password)
        self.save()


# ------------- OpenRouter & Detection -------------
class SafetyDetector:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.model = OPENROUTER_MODEL
        self.ready = bool(api_key) and requests is not None
        if api_key and requests is None:
            print("OpenRouter setup failed: 'requests' package not installed.")

    def is_ready(self):
        return self.ready

    @staticmethod
    def _image_to_data_url(image: Image.Image) -> str:
        """Encode a PIL image as a base64 JPEG data URL for the chat image part."""
        b64 = base64.b64encode(jpeg_bytes(image)).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"

    def analyze_screenshot(self, image: Image.Image, enabled_cats: list) -> dict:
        """
        Return dict: { "suspicious": bool, "category": str or None, "warning_message": str, "reason": str }
        """
        if not self.is_ready():
            return {"suspicious": False}

        prompt = f"""
You are an expert child internet safety AI called "TrashPandaPatrol".
Analyze the attached screenshot of a child's computer screen (likely a chat window, game chat, social media, live stream overlay).
Look specifically ONLY for visible text messages, usernames, pop-over text, and on-screen chat contents.

THREAT CATEGORIES (only flag if enabled):
{chr(10).join(f"- {c}" for c in enabled_cats)}

Key red flag behaviors (consider language patterns, intent, context for kids - DO NOT miss these):

MUST FLAG as Personal Data Requests (Phishing / Social Engineering):
- \"where do you live?\", \"I have your location!\", \"Give me money\", any question about home/school/address/location + \"I have/know your...\"

MUST FLAG as Violence & Gore:
- \"Gore\", \"Bleeds\", blood, kill , gore images or threats.

- Asking for, guessing or coaxing personal info: address, school, real name, phone, age, location, photos, password, parent's info.
- Social engineering & impersonation typical in Roblox/Fortnite/Minecraft/Discord/Kids apps.
- Explicit sexual, nudity, grooming references or slang suggesting it.
- Hate speech, slurs, targeted harassment, bullying, threats.
- Promotion of violence, gore, weapons.
- Encouragement of self-harm, suicide, eating disorders.
- Offers that seem phishing/scams: \"free robux\", \"free v-bucks\", \"click link for prize\", \"confirm with password\".
- Illegal drugs, bombs, hacking tips, etc. aimed at minor.
- General suspicious chat: requests to \"keep a secret\", \"dont tell your mom\", \"send me a pic\".

The analysis should understand context and conversational tone, not exact keywords only. Add extra sensitivity to child-targeted chat.

IF nothing risky appears, return exactly:
{{
  "suspicious": false,
  "category": null,
  "warning_message": "",
  "reason": "No concerning content."
}}

IF risky text exists matching any ENABLED category:
Return ONLY JSON (add nothing before or after):
{{
  "suspicious": true,
  "category": "EXACTLY one of the listed threat categories above if matched",
  "warning_message": "Short, direct, friendly but firm message for an 8-16 year old (max 2 sentences). Example style: 'Never share passwords with anyone! Even friends or heroes online.' Start with a strong action instruction + kind reassurance that it's okay to ask a trusted adult.",
  "reason": "1-2 sentence reason what you saw that triggered you."
}}

STRICT: Output MUST be valid compact JSON only.
"""
        try:
            # Cost optimization: downscale to a low, predictable tile count and send a
            # JPEG-encoded copy. High-contrast on-screen text stays legible.
            payload_img = downscale_for_llm(image)
            data_url = self._image_to_data_url(payload_img)

            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                # Optional OpenRouter attribution headers.
                "HTTP-Referer": "https://github.com/CipherHacks-2026/TrashPandaPatrol",
                "X-Title": "TrashPandaPatrol",
            }

            def build_body(model_slug):
                return {
                    "model": model_slug,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": data_url}},
                            ],
                        }
                    ],
                    # Ask for JSON-only output where the model/provider supports it.
                    "response_format": {"type": "json_object"},
                    "temperature": 0,
                    "max_tokens": 400,
                }

            # Try the primary model, then each fallback. For each model, retry a few
            # times on HTTP 429 (rate-limited upstream) with a short linear backoff.
            # The free shared pool throttles aggressively, so a second model often
            # succeeds at $0 instead of dropping the scan entirely.
            response = None
            models_to_try = [self.model] + list(OPENROUTER_FALLBACK_MODELS)
            for model_slug in models_to_try:
                for attempt in range(1, OPENROUTER_MAX_RETRIES + 1):
                    resp = requests.post(
                        OPENROUTER_API_URL, headers=headers, json=build_body(model_slug), timeout=60
                    )
                    if resp.ok:
                        response = resp
                        break
                    if resp.status_code == 429:
                        # Rate-limited. Back off and retry the same model, unless we're
                        # out of attempts, in which case fall through to the next model.
                        if attempt < OPENROUTER_MAX_RETRIES:
                            sleep_s = OPENROUTER_RETRY_BASE_SLEEP * attempt
                            print(f"OpenRouter 429 on {model_slug} (attempt {attempt}); retrying in {sleep_s}s.")
                            time.sleep(sleep_s)
                            continue
                        print(f"OpenRouter 429 on {model_slug}; trying next model.")
                        break
                    # Non-429 error (e.g. 404 retired model, 400): log and try next model.
                    print(f"OpenRouter HTTP {resp.status_code} on {model_slug}: {resp.text[:200]}")
                    break
                if response is not None:
                    break

            if response is None:
                # Every model was rate-limited or errored. Skip this scan; the next
                # cycle will try again (and the screen may have changed anyway).
                print("OpenRouter: all models unavailable this cycle (likely rate-limited).")
                return {"suspicious": False}

            data = response.json()
            text = data["choices"][0]["message"]["content"].strip()
            if text.startswith("```"):
                text = text.split("```")[1].replace("json", "").strip()
            import re
            # Extract JSON blob
            match = re.search(r"\{[\s\S]*\}", text)
            if match:
                result = json.loads(match.group(0))
                if "suspicious" not in result:
                    return {"suspicious": False}
                return result
        except Exception as e:
            print("OpenRouter detection error:", e)
        return {"suspicious": False}


# ------------- Screenshot Helper -------------
def capture_screen() -> Image.Image:
    with mss.mss() as sct:
        monitor = sct.monitors[1]  # primary full screen
        img = sct.grab(monitor)
        pil_img = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")
    return pil_img


def save_screenshot(img: Image.Image, tag: str = "") -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    fname = f"screenshot-{ts}{'-'+tag if tag else ''}.png"
    path = os.path.join(SCREENSHOTS_DIR, fname)
    img.save(path)
    return path


def downscale_for_llm(img: Image.Image, max_edge: int = LLM_MAX_EDGE) -> Image.Image:
    """Return a copy scaled so its longest edge is <= max_edge (never upscales)."""
    w, h = img.size
    longest = max(w, h)
    if longest <= max_edge:
        return img
    scale = max_edge / float(longest)
    new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
    return img.resize(new_size, Image.LANCZOS)


def jpeg_bytes(img: Image.Image, quality: int = LLM_JPEG_QUALITY) -> bytes:
    """
    Encode the image as JPEG in-memory and return the raw bytes. High-contrast
    on-screen text is unaffected at ~q82 while the payload shrinks vs PNG.
    RGBA is flattened to RGB for JPEG.
    """
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def frame_thumbnail_bytes(img: Image.Image) -> bytes:
    """Tiny grayscale thumbnail used for cheap frame-to-frame comparison."""
    thumb = img.convert("L").resize((FRAME_DIFF_THUMB, FRAME_DIFF_THUMB), Image.BILINEAR)
    return thumb.tobytes()


def frames_are_similar(prev: bytes, curr: bytes) -> bool:
    """
    True if two thumbnails are nearly identical (mean per-pixel abs diff below
    FRAME_DIFF_THRESHOLD). Pure-Python, no numpy dependency.
    """
    if prev is None or curr is None or len(prev) != len(curr):
        return False
    total = 0
    for a, b in zip(prev, curr):
        total += a - b if a > b else b - a
    mean_diff = total / float(len(curr))
    return mean_diff < FRAME_DIFF_THRESHOLD


# ------------- Warning Popup UI -------------
class WarningPopup:
    def __init__(self, parent_app):
        self.app = parent_app
        self.root = None
        self.veil = None
        self.panel = None
        self.can_close = False
        self.dim_alpha = 0.65
        self.seen_messages: set = set()
        # Single-instance guard. Set the instant we begin building a popup and
        # cleared only when it is fully torn down. This is checked BEFORE any
        # window is created, so a second scan can never stack a second veil
        # even if it fires before the first popup's windows are realized.
        self._active = False

    def is_open(self) -> bool:
        """True if a warning popup is currently displayed (or being built)."""
        if self._active:
            return True
        for win in (self.panel, self.veil):
            try:
                if win is not None and win.winfo_exists():
                    return True
            except Exception:
                pass
        return False

    def show(self, message: str):
        key = (message or "").strip().lower()
        if key and key in self.seen_messages:
            logger.info("[TrashPandaPatrol] Duplicate suspicious message; skipping popup.")
            return
        # If a popup is already on screen (or mid-build), do not stack another
        # one on top. This is what prevents the "second black overlay" that can
        # never be dismissed.
        if self.is_open():
            logger.info("[TrashPandaPatrol] Warning already open; not opening another popup.")
            return
        # Claim the single-instance slot immediately, before creating any window.
        self._active = True
        if key:
            self.seen_messages.add(key)

        self.can_close = False

        try:
            self._build_windows(message)
        except Exception as e:
            # If anything fails while building the popup, tear down whatever was
            # created and release the single-instance slot so future warnings
            # can still appear (otherwise a half-built popup would block forever).
            logger.warning("[TrashPandaPatrol] Failed to show warning popup: %s", e)
            self._teardown_windows()

    def _build_windows(self, message: str):
        # ---- Window 1: fullscreen DIM VEIL (semi-transparent so the real screen
        # stays visible-but-darkened behind it, "to give focus" to the warning). ----
        self.veil = ctk.CTkToplevel()
        self.veil.attributes("-fullscreen", True)
        self.veil.attributes("-topmost", True)
        self.veil.configure(bg="black")
        self.veil.overrideredirect(True)
        # True dim: the desktop shows through, darkened. dim_alpha=0.65 => 65% black veil.
        self.veil.attributes("-alpha", self.dim_alpha)
        self.veil.protocol("WM_DELETE_WINDOW", lambda: None)
        self.veil.bind("<Escape>", lambda e: "break")
        # Clicking the dimmed veil must NOT do anything destructive. Instead,
        # bring the panel back to the front and give it focus so the child is
        # guided back to the "I understand" button (this also fixes the case
        # where clicking the veil stole focus from the panel).
        self.veil.bind("<Button-1>", lambda e: self._focus_panel())

        screen_w = self.veil.winfo_screenwidth()

        # Keep self.root pointed at the veil for backward compatibility with callers
        # that read/assign warning_popup.root.
        self.root = self.veil

        # ---- Window 2: corner PANEL (fully opaque, full brightness, not dimmed). ----
        popup_w, popup_h = 540, 320
        x = screen_w - popup_w - 30
        y = 30

        self.panel = ctk.CTkToplevel()
        self.panel.overrideredirect(True)
        self.panel.attributes("-topmost", True)
        self.panel.attributes("-alpha", 1.0)
        self.panel.geometry(f"{popup_w}x{popup_h}+{x}+{y}")
        self.panel.configure(bg="#0f172a")
        self.panel.protocol("WM_DELETE_WINDOW", lambda: None)
        self.panel.bind("<Escape>", lambda e: "break")
        self.panel.focus_force()

        popup_frame = ctk.CTkFrame(self.panel, width=popup_w, height=popup_h,
                                   corner_radius=18, fg_color="#0f172a", border_color="#64748b", border_width=3)
        popup_frame.pack(fill="both", expand=True)

        # Raccoon visual (drawn programmatically)
        raccoon_img = self._create_raccoon_image(110, 110)
        raccoon_label = ctk.CTkLabel(popup_frame, image=ctk.CTkImage(raccoon_img, size=(110, 110)), text="")
        raccoon_label.place(x=20, y=20)

        # Title and message
        title_lbl = ctk.CTkLabel(popup_frame, text="🦝 TrashPanda Alert", font=ctk.CTkFont(size=22, weight="bold"),
                                 text_color="#f59e0b")
        title_lbl.place(x=145, y=15)

        msg_label = ctk.CTkLabel(popup_frame, text=message,
                                 font=ctk.CTkFont(size=14), text_color="#f1f5f9",
                                 wraplength=310, justify="left")
        msg_label.place(x=145, y=55)

        footer = ctk.CTkLabel(popup_frame, text="Take a deep breath and stay safe online.\nTell a trusted adult if anything makes you uncomfortable.",
                              font=ctk.CTkFont(size=11), text_color="#64748b", justify="left")
        footer.place(x=20, y=220)

        # Countdown timer + close disabled
        self.timer_label = ctk.CTkLabel(popup_frame, text="Please read carefully (5s)", font=ctk.CTkFont(size=13),
                                        text_color="#ef4444")
        self.timer_label.place(x=145, y=265)

        # Close button initially disabled
        self.close_btn = ctk.CTkButton(popup_frame, text="I understand ✓", width=160,
                                        fg_color="#334155", text_color="#e2e8f0",
                                        command=self._close_popup, state="disabled")
        self.close_btn.place(x=340, y=260)

        # Keep the panel above the veil.
        self.panel.lift()

        # Lockout timer (driven by the panel's event loop).
        self.countdown = 5
        self._run_countdown()

    def _run_countdown(self):
        if not (self.panel and self.panel.winfo_exists()):
            return
        if self.countdown > 0:
            self.timer_label.configure(text=f"Reading required: {self.countdown}s")
            self.countdown -= 1
            self.panel.after(1000, self._run_countdown)
        else:
            self.can_close = True
            self.timer_label.configure(text="Thank you — you may close now")
            self.close_btn.configure(state="normal", fg_color="#166534", text_color="white")

    def _focus_panel(self):
        """Bring the alert panel back above the veil and give it focus.

        Called when the child clicks the dimmed veil instead of the panel, so
        focus is never lost and they are guided back to the close button.
        """
        try:
            if self.panel is not None and self.panel.winfo_exists():
                self.panel.lift()
                self.panel.attributes("-topmost", True)
                self.panel.focus_force()
        except Exception:
            pass
        return "break"

    def _close_popup(self):
        if not self.can_close:
            return
        self._teardown_windows()

    def _teardown_windows(self):
        """Destroy the panel + veil and release the single-instance slot."""
        for win_attr in ("panel", "veil"):
            win = getattr(self, win_attr, None)
            if win is not None:
                try:
                    win.destroy()
                except Exception:
                    pass
            setattr(self, win_attr, None)
        self.root = None
        self._active = False

    def _create_raccoon_image(self, w: int, h: int) -> Image.Image:
        """Procedurally create a cute cartoon raccoon + magnifying glass"""
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        # Body/head
        d.ellipse([10, 35, 75, 100], fill="#4b5563")  # dark gray
        d.ellipse([22, 18, 68, 65], fill="#6b7280")    # head

        # Ears
        d.polygon([(23, 28), (14, 5), (35, 19)], fill="#374151")
        d.polygon([(60, 19), (58, 7), (72, 25)], fill="#374151")
        d.ellipse([23, 12, 33, 23], fill="#9ca3af")
        d.ellipse([50, 12, 60, 23], fill="#9ca3af")

        # Eye mask
        d.ellipse([26, 35, 45, 53], fill="#111827")
        d.ellipse([47, 34, 66, 53], fill="#111827")
        # Eyes
        d.ellipse([30, 41, 38, 50], fill="#f1f5f9")
        d.ellipse([53, 41, 61, 50], fill="#f1f5f9")
        d.ellipse([33, 44, 36, 48], fill="#111827")
        d.ellipse([56, 44, 59, 48], fill="#111827")

        # Nose & snout highlight
        d.ellipse([41, 51, 51, 60], fill="#1f2937")
        # smile
        d.arc([38, 55, 54, 65], start=10, end=170, fill="#0f172a", width=1)

        # Simplistic body stripes etc
        d.line([(28, 65), (28, 92)], fill="#374151", width=2)
        d.line([(48, 65), (48, 92)], fill="#374151", width=2)

        # Arm with magnifying glass
        d.rectangle([62, 55, 100, 72], fill="#4b5563")
        # Glass
        d.ellipse([78, 47, 110, 79], outline="#64748b", width=6)
        d.ellipse([84, 55, 104, 71], outline="#cbd5e1", width=1)

        # Handle
        d.line([(106, 75), (120, 95)], fill="#4b5563", width=5)
        return img


# ------------- Main Application -------------
class TrashPandaPatrolApp:
    def __init__(self):
        self.config = ConfigManager()
        api_key = os.environ.get("OPEN_ROUTER_API_KEY")
        self.detector = SafetyDetector(api_key)
        self.monitor_thread = None
        self.stop_event = threading.Event()
        self.tray_icon = None
        self.settings_window = None
        self.warning_popup = WarningPopup(self)
        self.monitor_active = self.config.get("screen_monitoring_enabled")
        self._prev_frame_thumb = None  # last frame thumbnail, for cheap frame-diff skip

    # ---- UI: Settings Window ----
    def open_settings(self, require_auth=True):
        if self.settings_window and self.settings_window.winfo_exists():
            self.settings_window.focus()
            return

        if require_auth and not self._authenticate():
            return

        # ---- Color palette (matches the dark "card" mockup) ----
        BG = "#3a3a3a"          # window background
        CARD = "#2b2b2b"        # inner section cards
        ENTRY = "#5a5a5a"       # entry field fill
        TEXT = "#f1f1f1"        # primary text
        MUTED = "#a3a3a3"       # secondary text
        ACCENT_BLUE = "#3b82f6" # switches / blue button
        ORANGE = "#e07b39"      # test button

        self.settings_window = ctk.CTkToplevel()
        self.settings_window.title("TrashPandaPatrol • Parent Settings")
        self.settings_window.geometry("620x960")
        self.settings_window.resizable(False, False)
        self.settings_window.configure(fg_color=BG)

        # ---- Header: logo (raccoon + wordmark) ----
        header = ctk.CTkFrame(self.settings_window, fg_color="transparent")
        header.pack(padx=20, pady=(18, 6), fill="x")
        logo_path = os.path.join(os.path.dirname(__file__), "assets", "TrashPandaPatrol.png")
        try:
            logo_img = Image.open(logo_path)
            logo_w = 360
            logo_h = int(logo_img.height * (logo_w / logo_img.width))
            ctk.CTkLabel(header, text="", image=ctk.CTkImage(logo_img, size=(logo_w, logo_h))).pack()
        except Exception:
            ctk.CTkLabel(header, text="TRASH PANDA PATROL",
                         font=ctk.CTkFont(size=26, weight="bold"), text_color=ACCENT_BLUE).pack()

        # ---- Parent Email (RECEIVER) ----
        contact_frame = ctk.CTkFrame(self.settings_window, fg_color=CARD, corner_radius=14)
        contact_frame.pack(padx=20, pady=6, fill="x")
        contact_row = ctk.CTkFrame(contact_frame, fg_color="transparent")
        contact_row.pack(fill="x", padx=14, pady=12)
        ctk.CTkLabel(contact_row, text="Parent Email (RECEIVER)", anchor="w",
                     font=ctk.CTkFont(size=15, weight="bold"), text_color=TEXT).pack(side="left")
        self.parent_email_var = tk.StringVar(value=self.config.get("parent_email", ""))
        email_entry = ctk.CTkEntry(contact_row, textvariable=self.parent_email_var,
                                   placeholder_text="parent@example.com", width=300,
                                   fg_color=ENTRY, border_width=0, corner_radius=16)
        email_entry.pack(side="right")
        email_entry.bind("<FocusOut>", lambda e: self._autosave_parent_email())

        # Note: the OpenRouter API key is provided only via the OPEN_ROUTER_API_KEY environment variable (never stored in the app config).

        # ---- Master Monitoring (compact toggle) ----
        master_frame = ctk.CTkFrame(self.settings_window, fg_color=CARD, corner_radius=14)
        master_frame.pack(padx=20, pady=6, fill="x")
        self.master_switch = ctk.CTkSwitch(master_frame, text="Enable Screen Monitoring",
                                            command=self._toggle_master,
                                            progress_color=ACCENT_BLUE, text_color=TEXT,
                                            font=ctk.CTkFont(size=15, weight="bold"))
        self.master_switch.pack(pady=14, padx=18, anchor="w")
        self.master_switch.select() if self.config.get("screen_monitoring_enabled") else self.master_switch.deselect()

        # ---- Warning Categories ----
        self.toggle_frame = ctk.CTkFrame(self.settings_window, fg_color=CARD, corner_radius=14)
        self.toggle_frame.pack(padx=20, pady=6, fill="x")

        ctk.CTkLabel(self.toggle_frame, text="Warning Catagories",
                     font=ctk.CTkFont(size=18, weight="bold"), text_color=TEXT).pack(anchor="w", padx=18, pady=(14, 6))

        # Short, friendly labels for display (config keys stay the canonical CATEGORIES)
        cat_display = {
            "Hate Speech & Harassment": "Hate Speech & Harrassment",
            "Violence & Gore": "Violence & Gore",
            "Sexual Content (NSFW)": "Sexual Content",
            "Illegal Acts & Drugs": "Illegal Acts",
            "Self-Harm": "Self-Harm",
            "Personal Data Requests (Phishing / Social Engineering)": "Personal Data Requests (Phishing)",
        }

        self.cat_vars = {}
        self.cat_switches = {}
        for cat in CATEGORIES:
            var = tk.BooleanVar(value=self.config.get("enabled_categories", {}).get(cat, True))
            sw = ctk.CTkSwitch(self.toggle_frame, text=cat_display.get(cat, cat), variable=var,
                               command=lambda c=cat, v=var: self._toggle_category(c, v),
                               progress_color=ACCENT_BLUE, text_color=TEXT,
                               font=ctk.CTkFont(size=15, weight="bold"))
            sw.pack(anchor="w", padx=22, pady=6)
            self.cat_vars[cat] = var
            self.cat_switches[cat] = sw
        ctk.CTkLabel(self.toggle_frame, text="").pack(pady=2)

        # ---- SMS / automated notifications ----
        notif_frame = ctk.CTkFrame(self.settings_window, fg_color=CARD, corner_radius=14)
        notif_frame.pack(padx=20, pady=6, fill="x")
        self.notif_switch = ctk.CTkSwitch(notif_frame, text="Send Automated SMS warnings to parent phone",
                                           command=self._toggle_notifications,
                                           progress_color=ACCENT_BLUE, text_color=TEXT,
                                           font=ctk.CTkFont(size=15, weight="bold"))
        self.notif_switch.pack(pady=14, padx=18, anchor="w")
        if self.config.get("phone_notifications_enabled"):
            self.notif_switch.select()

        # ---- Email Sender (SENDER) ----
        gmail_frame = ctk.CTkFrame(self.settings_window, fg_color=CARD, corner_radius=14)
        gmail_frame.pack(padx=20, pady=6, fill="x")
        ctk.CTkLabel(gmail_frame, text="Email Sender (SENDER) – Gmail account used to send alerts.",
                     anchor="w", font=ctk.CTkFont(size=15, weight="bold"), text_color=TEXT).pack(anchor="w", padx=18, pady=(14, 2))
        ctk.CTkLabel(gmail_frame, text="Make an App Password at myaccount.google.com/apppasswords (2-Step Verification must be on).",
                     anchor="w", font=ctk.CTkFont(size=11), text_color=MUTED).pack(anchor="w", padx=18, pady=(0, 6))
        self.gmail_addr_var = tk.StringVar(value=self.config.get("gmail_address", ""))
        self.gmail_pw_var = tk.StringVar(value=self.config.get("gmail_app_password", ""))
        for lbl, svar in [("Gmail Address", self.gmail_addr_var), ("App Password", self.gmail_pw_var)]:
            rowf = ctk.CTkFrame(gmail_frame, fg_color="transparent")
            rowf.pack(fill="x", padx=18, pady=4)
            ctk.CTkLabel(rowf, text=lbl, width=140, anchor="w",
                         font=ctk.CTkFont(size=14, weight="bold"), text_color=TEXT).pack(side="left")
            ent = ctk.CTkEntry(rowf, textvariable=svar, width=300,
                               fg_color=ENTRY, border_width=0, corner_radius=16,
                               show="•" if "password" in lbl.lower() else "")
            ent.pack(side="left", padx=6)
            ent.bind("<FocusOut>", lambda e: self._autosave_gmail())
        ctk.CTkLabel(gmail_frame, text="").pack(pady=2)

        # ---- Bottom controls ----
        action_frame = ctk.CTkFrame(self.settings_window, fg_color="transparent")
        action_frame.pack(fill="x", pady=(10, 18), padx=20)

        pwd_btn = ctk.CTkButton(action_frame, text="Change Parent Password",
                                fg_color=ACCENT_BLUE, hover_color="#2563eb", corner_radius=14,
                                font=ctk.CTkFont(size=14, weight="bold"),
                                command=self._change_password_dialog)
        pwd_btn.pack(side="left", padx=(0, 8))

        test_btn = ctk.CTkButton(action_frame, text="Test Warning",
                                 fg_color=ORANGE, hover_color="#c2671f", corner_radius=14,
                                 font=ctk.CTkFont(size=14, weight="bold"),
                                 command=lambda: self._trigger_warning_manual())
        test_btn.pack(side="left", padx=4)

        save_info = ctk.CTkLabel(action_frame, text="All settings are auto-saved instantly",
                                 text_color=MUTED, font=ctk.CTkFont(size=12))
        save_info.pack(side="right")

        self._refresh_disabled_state()

        # On window close just hide
        self.settings_window.protocol("WM_DELETE_WINDOW", lambda: self.settings_window.withdraw())

    def _authenticate(self):
        if self.config.is_first_run():
            # Force set password
            pwd = simpledialog.askstring("First Time Setup", "Create a PARENT PASSWORD (keep safe):", show="*")
            if not pwd or len(pwd) < 4:
                messagebox.showerror("Error", "Password must be at least 4 characters.")
                return False
            self.config.set_password(pwd)
            messagebox.showinfo("Success", "Password created! You can change it anytime in settings.")
            return True

        pwd = simpledialog.askstring("Parent Verification", "Enter your parent password:", show="*")
        if pwd and self.config.verify_password(pwd):
            return True
        messagebox.showerror("Access Denied", "Incorrect password.")
        return False

    def _toggle_master(self):
        enabled = bool(self.master_switch.get())
        self.config.set("screen_monitoring_enabled", enabled)
        self.monitor_active = enabled
        self._refresh_disabled_state()
        self._restart_monitor_if_needed()

    def _refresh_disabled_state(self):
        in_master = self.master_switch.get()
        state = "normal" if in_master else "disabled"
        # Enable/disable all category switches
        for sw in getattr(self, "cat_switches", {}).values():
            sw.configure(state=state)
        # Phone toggle
        self.notif_switch.configure(state=state)

    def _toggle_category(self, category, var):
        self.config.set_category(category, var.get())

    def _toggle_notifications(self):
        val = bool(self.notif_switch.get())
        self.config.set("phone_notifications_enabled", val and bool(self.config.get("parent_email")))

    def _autosave_parent_email(self):
        self.config.set("parent_email", self.parent_email_var.get().strip())

    def _autosave_gmail(self):
        self.config.update({
            "gmail_address": self.gmail_addr_var.get().strip(),
            # App passwords are often shown with spaces ("abcd efgh ijkl mnop");
            # strip them so login works regardless of how it was pasted.
            "gmail_app_password": self.gmail_pw_var.get().replace(" ", "").strip(),
        })

    def _change_password_dialog(self):
        old = simpledialog.askstring("Current Password", "Enter CURRENT parent password:", show="*")
        if not self.config.verify_password(old):
            messagebox.showerror("Failed", "Incorrect current password.")
            return
        new = simpledialog.askstring("New Password", "Enter NEW parent password:", show="*")
        if new and len(new) >= 4:
            confirm = simpledialog.askstring("Confirm", "Confirm NEW password:", show="*")
            if new == confirm:
                self.config.set_password(new)
                messagebox.showinfo("Success", "Password changed successfully!")
            else:
                messagebox.showerror("Mismatch", "Passwords did not match.")
        else:
            messagebox.showerror("Invalid", "New password must be at least 4 chars.")

    def _trigger_warning_manual(self):
        msg = "Dont trust strangers! Keep your personal information to yourself."
        self.warning_popup.show(msg)

    # ---- Monitoring Thread ----
    def start_monitor(self):
        if self.monitor_thread and self.monitor_thread.is_alive():
            return
        self._prev_frame_thumb = None  # force a fresh analysis on (re)start
        self.stop_event.clear()
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()
        print("[TrashPandaPatrol] Monitor started")

    def stop_monitor(self):
        self.stop_event.set()
        if self.monitor_thread:
            self.monitor_thread.join(timeout=1.5)
        print("[TrashPandaPatrol] Monitor stopped")

    def _restart_monitor_if_needed(self):
        if self.config.get("screen_monitoring_enabled"):
            self.stop_monitor()
            self.start_monitor()
        else:
            self.stop_monitor()

    def _monitor_loop(self):
        interval = self.config.get("monitor_interval_seconds", 35)
        idle_cycles = 0  # consecutive skipped scans -> used to back off the interval
        while not self.stop_event.is_set():
            if not self.config.get("screen_monitoring_enabled"):
                time.sleep(2)
                continue
            try:
                img = capture_screen()
                # Only analyze if at least one category enabled + detector
                enabled = [c for c, on in self.config.get("enabled_categories").items() if on]
                if not enabled or not self.detector.is_ready():
                    time.sleep(min(3, interval))
                    continue

                # --- (2) Frame-diff skip: don't pay for an unchanged screen ---
                curr_thumb = frame_thumbnail_bytes(img)
                if frames_are_similar(self._prev_frame_thumb, curr_thumb):
                    self._prev_frame_thumb = curr_thumb
                    idle_cycles += 1
                    # Adaptive back-off: sleep longer (up to 2x) while the screen is static.
                    backoff = min(interval * 2, interval + idle_cycles * 5)
                    logger.info("[TrashPandaPatrol] Frame unchanged; skipping LLM (sleep %ss).", backoff)
                    time.sleep(backoff)
                    continue
                self._prev_frame_thumb = curr_thumb
                idle_cycles = 0

                # NOTE: OCR pre-filter intentionally removed. We do not ship Tesseract,
                # so it only ever skipped every scan (empty OCR text => always below the
                # threshold). The frame-diff check above already avoids paying for an
                # unchanged screen, which is enough cost control for our use.

                result = self.detector.analyze_screenshot(img, enabled)
                if result.get("suspicious"):
                    cat = result.get("category")
                    # Robust category match: exact or substring (in case model returns shortened version)
                    matched_cat = None
                    if cat:
                        cl = str(cat).strip().lower()
                        for real_c, is_on in self.config.get("enabled_categories", {}).items():
                            if is_on:
                                rl = real_c.lower()
                                if cl == rl or cl in rl or rl in cl:
                                    matched_cat = real_c
                                    break
                    if matched_cat:
                        warning_msg = result.get("warning_message") or "Be careful online — you are not alone!"
                        saved_path = save_screenshot(img, tag=matched_cat[:15].replace(" ", "-"))
                        print(f"[TrashPandaPatrol] TRIGGERED by {matched_cat} | saved {saved_path}")
                        self._show_warning_in_thread(warning_msg)
                        if self.config.get("phone_notifications_enabled") and self.config.get("parent_email"):
                            # Run email send off the monitor thread so a slow SMTP
                            # connection never delays the next scan.
                            threading.Thread(
                                target=self._send_email_alert,
                                args=(warning_msg, matched_cat, saved_path),
                                daemon=True,
                            ).start()
                    else:
                        print(f"[TrashPandaPatrol] Suspicious but category '{cat}' not in enabled or not matched.")
                else:
                    reason = result.get("reason", "")
                    if reason:
                        print("[TrashPandaPatrol] Clean scan. Reason:", reason[:150])
            except Exception as ex:
                print("Monitor loop err:", ex)

            time.sleep(interval)

    def _show_warning_in_thread(self, message):
        # Prefer a persistent hidden main root so the popup shows reliably.
        # show() creates its own veil + panel Toplevels owned by whichever
        # tk event loop is running, so we only need to schedule it.
        # IMPORTANT: all tkinter work must happen on the UI thread that owns the
        # root. We never create a second Tk root / event loop here, because
        # stacking multiple roots on different threads is what can leave an
        # orphaned, un-dismissable black overlay on screen.
        def schedule_show():
            self.warning_popup.show(message)

        if hasattr(self, "hidden_root") and self.hidden_root:
            try:
                self.hidden_root.after(30, schedule_show)
            except Exception as e:
                logger.warning("[TrashPandaPatrol] Could not schedule warning: %s", e)
        else:
            logger.warning("[TrashPandaPatrol] No UI root available; skipping warning popup.")

    def _send_email_alert(self, warning_text: str, category: str, img_path: str):
        """
        Send a free email alert to the parent via Gmail SMTP, attaching the
        screenshot. Requires a Gmail address + a 16-char Google "App Password"
        (generated at https://myaccount.google.com/apppasswords with 2-Step
        Verification turned on). No Twilio, no cost, no phone number needed.
        """
        sender = (self.config.get("gmail_address") or "").strip()
        app_pw = (self.config.get("gmail_app_password") or "").replace(" ", "")
        recipient = (self.config.get("parent_email") or "").strip()
        if not recipient:
            # If no separate parent email is set, fall back to emailing the sender.
            recipient = sender
        if not (sender and app_pw and recipient):
            print("[TrashPandaPatrol] Email alert skipped: set Gmail address, App Password, and parent email.")
            return

        try:
            msg = EmailMessage()
            msg["Subject"] = f"TrashPandaPatrol Alert: {category}"
            msg["From"] = sender
            msg["To"] = recipient
            msg.set_content(
                "TRASHPANDAPATROL ALERT (Child Device)\n"
                f"Category: {category}\n"
                f"Warning shown to child: {warning_text}\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                "A screenshot of the moment is attached (also saved locally on the device).\n"
                "Please check in with your child about their recent online activity."
            )

            # Attach the screenshot if available.
            if img_path and os.path.exists(img_path):
                try:
                    with open(img_path, "rb") as f:
                        img_data = f.read()
                    msg.add_attachment(
                        img_data, maintype="image", subtype="png",
                        filename=os.path.basename(img_path)
                    )
                except Exception as e:
                    print("[TrashPandaPatrol] Could not attach screenshot:", e)

            # Gmail SMTP over SSL (port 465).
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=30) as server:
                server.login(sender, app_pw)
                server.send_message(msg)
            print(f"[TrashPandaPatrol] Email alert sent to {recipient}.")
        except smtplib.SMTPAuthenticationError:
            print("[TrashPandaPatrol] Email auth failed. Use a Gmail APP PASSWORD "
                  "(not your normal password) and enable 2-Step Verification.")
        except Exception as e:
            print("[TrashPandaPatrol] Email send fail:", e)

    # ---- Tray Icon ----
    def _open_logs(self):
        """Open the log file in the user's default text viewer (Notepad on Windows)."""
        try:
            if os.path.exists(LOG_FILE):
                os.startfile(LOG_FILE)  # Windows: opens in default handler (Notepad)
            else:
                # No log yet; open the AppData folder so the parent can see where it will be.
                os.startfile(APPDATA)
        except Exception as e:
            logger.warning("Could not open log file: %s", e)
            try:
                subprocess.Popen(["notepad.exe", LOG_FILE])
            except Exception:
                pass

    def _create_tray(self):
        icon_img = self._make_tray_icon_image()

        def open_settings_from_tray(icon, item):
            self._show_settings_threadsafe()

        def toggle_monitor(icon, item):
            new_val = not self.config.get("screen_monitoring_enabled")
            self.config.set("screen_monitoring_enabled", new_val)
            self.monitor_active = new_val
            if new_val:
                self.start_monitor()
            else:
                self.stop_monitor()

        menu = (
            item("Open Settings", open_settings_from_tray),
            item("Toggle Monitoring", toggle_monitor, checked=lambda item: self.config.get("screen_monitoring_enabled")),
            item("Test Warning", lambda i: self._show_warning_in_thread("Never share personal info with anyone you do not know in real life!")),
            item("View Logs", lambda i: self._open_logs()),
            pystray.Menu.SEPARATOR,
            item("Quit TrashPandaPatrol", self._quit_app)
        )
        self.tray_icon = pystray.Icon("TrashPandaPatrol", icon_img, "TrashPandaPatrol • Child Protection", menu)
        return self.tray_icon

    def _make_tray_icon_image(self):
        # Simple raccoon head for status/tray
        img = Image.new('RGB', (64, 64), color=(15, 23, 42))
        d = ImageDraw.Draw(img)
        d.ellipse([8, 12, 56, 55], fill=(107, 114, 128))
        d.ellipse([15, 6, 32, 26], fill=(55, 65, 81))
        d.ellipse([33, 6, 50, 26], fill=(55, 65, 81))
        d.ellipse([19, 20, 29, 32], fill=(15, 23, 42))
        d.ellipse([35, 20, 45, 32], fill=(15, 23, 42))
        d.ellipse([24, 26, 40, 40], fill=(31, 41, 55))
        return img

    def _show_settings_threadsafe(self):
        # Use main persistent hidden root for scheduling
        if hasattr(self, 'hidden_root') and self.hidden_root:
            self.hidden_root.after(10, lambda: self.open_settings(require_auth=True))
        else:
            t = threading.Thread(target=self.run_settings_only, daemon=True)
            t.start()

    def run_settings_only(self):
        self.open_settings(require_auth=True)

    def _quit_app(self, icon=None, item=None):
        self.stop_monitor()
        if self.tray_icon:
            self.tray_icon.stop()
        if hasattr(self, 'hidden_root') and self.hidden_root:
            self.hidden_root.after(0, self.hidden_root.destroy)
        sys.exit(0)

    # ---- Boot ----
    def run(self):
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        print("[TrashPandaPatrol] Starting...")
        api_key_present = bool(os.environ.get("OPEN_ROUTER_API_KEY"))
        print("[TrashPandaPatrol] OPEN_ROUTER_API_KEY present:", api_key_present)
        if not api_key_present:
            print("  (Detection disabled; monitoring will do nothing)")

        # Persistent hidden window to host dialogs/popups will be created inside the dedicated UI thread
        # (tkinter/ctk objects and mainloop must be owned by the same thread).
        # pystray blocks on this (main) thread for reliable Windows tray icon + message pump.

        # Schedule UI in background thread (root creation + mainloop happen there).
        def _run_ui():
            self.hidden_root = ctk.CTk()
            self.hidden_root.withdraw()

            # First-run dialogs + deferred UI can safely run here (same thread as root).
            if self.config.is_first_run():
                self._authenticate()
                # auto open
                self.hidden_root.after(300, lambda: self.open_settings(require_auth=False))
            else:
                print("[TrashPandaPatrol] Not first run - no window auto-opens.")

            # Start monitoring if enabled at launch
            if self.config.get("screen_monitoring_enabled"):
                self.start_monitor()
            else:
                print("[TrashPandaPatrol] Screen monitoring is OFF in settings.")

            try:
                self.hidden_root.mainloop()
            except Exception as ex:
                print("UI loop error:", ex)

        ui_thread = threading.Thread(target=_run_ui, daemon=True)
        ui_thread.start()

        # Create and run tray (blocking main thread)
        tray = self._create_tray()
        def _tray_setup(icon):
            icon.visible = True
            print("[TrashPandaPatrol] Tray icon registered. Check notification area (may need to expand ^).")
            try:
                icon.notify(
                    "TrashPandaPatrol is active in the system tray.\nRight-click raccoon icon to open Settings (or toggle monitoring / quit).",
                    "TrashPandaPatrol"
                )
            except Exception:
                pass
            print("[TrashPandaPatrol] Ready. Use tray menu for UI.")
        try:
            tray.run(setup=_tray_setup)  # blocks until stop
        except KeyboardInterrupt:
            self._quit_app()
        print("[TrashPandaPatrol] Exited.")


if __name__ == "__main__":
    setup_logging()
    app = TrashPandaPatrolApp()
    app.run()
