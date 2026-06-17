"""
TrashPandaPatrol - Child Cybersecurity Safety App
Parent-controlled Windows monitoring app for kids 16 and under.
Uses Gemini Vision to analyze screenshots.
"""
import os
import sys
import json
import time
import hashlib
import threading
import tempfile
import base64
from datetime import datetime
from io import BytesIO

import tkinter as tk
from tkinter import messagebox, simpledialog

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFont, ImageTk
import mss
import google.generativeai as genai
try:
    from twilio.rest import Client as TwilioClient
except ImportError:
    TwilioClient = None

import pystray
from pystray import MenuItem as item

# ------------- Constants and Paths -------------
APP_NAME = "TrashPandaPatrol"
APPDATA = os.path.join(os.getenv("APPDATA", os.path.expanduser("~")), APP_NAME)
os.makedirs(APPDATA, exist_ok=True)
CONFIG_FILE = os.path.join(APPDATA, "settings.json")
SCREENSHOTS_DIR = os.path.join(APPDATA, "screenshots")
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
RACOON_IMAGE_PATH = os.path.join(os.path.dirname(__file__), "assets", "raccoon.png")  # optional external

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
    "phone_number": "",

    "screen_monitoring_enabled": False,
    "phone_notifications_enabled": False,
    "enabled_categories": {cat: True for cat in CATEGORIES},
    "monitor_interval_seconds": 35,
    "twilio_sid": "",
    "twilio_token": "",
    "twilio_from_phone": ""
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


# ------------- Gemini & Detection -------------
class SafetyDetector:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.model = None
        if api_key:
            try:
                genai.configure(api_key=api_key)
                self.model = genai.GenerativeModel("gemini-2.0-flash")
            except Exception as e:
                print("Gemini setup failed:", e)

    def is_ready(self):
        return self.model is not None

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

Key red flag behaviors (consider language patterns, intent, context for kids):
- Asking for, guessing or coaxing personal info: address, school, real name, phone, age, location, photos, password, parent's info.
- Social engineering & impersonation typical in Roblox/Fortnite/Minecraft/Discord/Kids apps.
- Explicit sexual, nudity, grooming references or slang suggesting it.
- Hate speech, slurs, targeted harassment, bullying, threats.
- Promotion of violence, gore, weapons.
- Encouragement of self-harm, suicide, eating disorders.
- Offers that seem phishing/scams: "free robux", "free v-bucks", "click link for prize", "confirm with password".
- Illegal drugs, bombs, hacking tips, etc. aimed at minor.
- General suspicious chat: requests to "keep a secret", "dont tell your mom", "send me a pic".

The analysis should understand context and conversational tone, not exact keywords only.

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
            response = self.model.generate_content([prompt, image])
            text = response.text.strip()
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
            print("Gemini detection error:", e)
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


# ------------- Warning Popup UI -------------
class WarningPopup:
    def __init__(self, parent_app):
        self.app = parent_app
        self.root = None
        self.can_close = False
        self.dim_alpha = 0.65

    def show(self, message: str):
        # Use root of main but separate overlay
        self.root = ctk.CTkToplevel()
        self.root.attributes("-fullscreen", True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 1)
        self.root.configure(bg="black")
        self.root.overrideredirect(True)
        self.root.focus_force()

        # Full black dim layer
        dim_canvas = tk.Canvas(self.root, bg="black", highlightthickness=0)
        dim_canvas.pack(fill="both", expand=True)
        dim_canvas.create_rectangle(0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight(),
                                    fill="#1a1a1a", outline="")
        dim_canvas.update_idletasks()

        # Pop-up content - Corner warning panel
        popup_w, popup_h = 540, 320
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = screen_w - popup_w - 30
        y = 30

        popup_frame = ctk.CTkFrame(self.root, width=popup_w, height=popup_h,
                                   corner_radius=18, fg_color="#0f172a", border_color="#64748b", border_width=3)
        popup_frame.place(x=x, y=y)

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

        # Lockout timer
        self.countdown = 5
        self._run_countdown()

        # Block Alt-F4 somewhat by focusing
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)
        self.root.bind("<Escape>", lambda e: None)
        self.root.bind("<Button-1>", lambda e: None)  # click dim ignore

        # Make sure other windows can be seen through but darkened
        self.root.after(100, self.root.attributes, "-alpha", 0.98)

    def _run_countdown(self):
        if self.countdown > 0:
            self.timer_label.configure(text=f"Reading required: {self.countdown}s")
            self.countdown -= 1
            self.root.after(1000, self._run_countdown)
        else:
            self.can_close = True
            self.timer_label.configure(text="Thank you — you may close now")
            self.close_btn.configure(state="normal", fg_color="#166534", text_color="white")

    def _close_popup(self):
        if self.can_close and self.root:
            self.root.destroy()
            self.root = None

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
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            print("WARNING: No GEMINI_API_KEY environment variable set. Detection will be disabled. Set it to enable monitoring.")
        self.detector = SafetyDetector(api_key)
        self.monitor_thread = None
        self.stop_event = threading.Event()
        self.tray_icon = None
        self.settings_window = None
        self.warning_popup = WarningPopup(self)
        self.monitor_active = self.config.get("screen_monitoring_enabled")

    # ---- UI: Settings Window ----
    def open_settings(self, require_auth=True):
        if self.settings_window and self.settings_window.winfo_exists():
            self.settings_window.focus()
            return

        if require_auth and not self._authenticate():
            return

        self.settings_window = ctk.CTkToplevel()
        self.settings_window.title("TrashPandaPatrol • Parent Settings")
        self.settings_window.geometry("680x720")
        self.settings_window.resizable(False, False)

        # Password section
        pass_frame = ctk.CTkFrame(self.settings_window, corner_radius=12)
        pass_frame.pack(padx=20, pady=(20, 10), fill="x")

        ctk.CTkLabel(pass_frame, text="🔐 Access Protected • Changes auto-saved", font=ctk.CTkFont(size=16, weight="bold")).pack(pady=8)

        # Parent Phone
        contact_frame = ctk.CTkFrame(self.settings_window)
        contact_frame.pack(padx=20, pady=6, fill="x")

        ctk.CTkLabel(contact_frame, text="Parent Phone Number (E.164: +15551234567) — leave blank to disable SMS", anchor="w").pack(fill="x", padx=10, pady=(8, 2))
        self.phone_var = tk.StringVar(value=self.config.get("phone_number", ""))
        phone_entry = ctk.CTkEntry(contact_frame, textvariable=self.phone_var, placeholder_text="+1XXXXXXXXXX", width=340)
        phone_entry.pack(padx=12, pady=4)
        phone_entry.bind("<FocusOut>", lambda e: self._autosave_phone())

        # Note: Gemini API key is now provided only via the GEMINI_API_KEY environment variable (never stored in the app config).

        # Master Monitoring
        master_frame = ctk.CTkFrame(self.settings_window)
        master_frame.pack(padx=20, pady=10, fill="x")

        self.master_switch = ctk.CTkSwitch(master_frame, text="✅ ENABLE SCREEN MONITORING for online safety & warnings",
                                            command=self._toggle_master)
        self.master_switch.pack(pady=8, padx=10, anchor="w")
        self.master_switch.select() if self.config.get("screen_monitoring_enabled") else self.master_switch.deselect()

        # Sub toggles section
        self.toggle_frame = ctk.CTkFrame(self.settings_window)
        self.toggle_frame.pack(padx=20, pady=2, fill="both")

        ctk.CTkLabel(self.toggle_frame, text="Warning Categories (only fires if Screen Monitoring enabled):",
                    font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=10, pady=8)

        self.cat_vars = {}
        for i, cat in enumerate(CATEGORIES):
            var = tk.BooleanVar(value=self.config.get("enabled_categories", {}).get(cat, True))
            sw = ctk.CTkSwitch(self.toggle_frame, text=cat, variable=var,
                               command=lambda c=cat, v=var: self._toggle_category(c, v))
            sw.pack(anchor="w", padx=14, pady=3)
            self.cat_vars[cat] = var

        # Phone Notifications
        notif_frame = ctk.CTkFrame(self.settings_window)
        notif_frame.pack(padx=20, pady=10, fill="x")
        self.notif_switch = ctk.CTkSwitch(notif_frame, text="📱 Send automated SMS warnings to parent phone (requires valid number + Twilio below if using images)",
                                           command=self._toggle_notifications)
        self.notif_switch.pack(pady=6, padx=10, anchor="w")
        if self.config.get("phone_notifications_enabled"):
            self.notif_switch.select()

        # Twilio (optional)
        twilio_frame = ctk.CTkFrame(self.settings_window)
        twilio_frame.pack(padx=20, pady=(0,10), fill="x")
        ctk.CTkLabel(twilio_frame, text="Twilio Settings (OPTIONAL — only needed for MMS image alerts)", anchor="w", font=ctk.CTkFont(size=12)).pack(padx=10, pady=4)
        self.tw_sid_var = tk.StringVar(value=self.config.get("twilio_sid", ""))
        self.tw_token_var = tk.StringVar(value=self.config.get("twilio_token", ""))
        self.tw_from_var = tk.StringVar(value=self.config.get("twilio_from_phone", ""))
        for lbl, svar in [("Account SID", self.tw_sid_var), ("Auth Token", self.tw_token_var), ("Twilio From Phone (+1..)", self.tw_from_var)]:
            rowf = ctk.CTkFrame(twilio_frame)
            rowf.pack(fill="x", padx=8)
            ctk.CTkLabel(rowf, text=lbl, width=130).pack(side="left", pady=3)
            ent = ctk.CTkEntry(rowf, textvariable=svar, width=420, show="•" if "token" in lbl.lower() else "")
            ent.pack(side="left", pady=2, padx=4)
            ent.bind("<FocusOut>", lambda e: self._autosave_twilio())

        # Bottom controls
        action_frame = ctk.CTkFrame(self.settings_window, fg_color="transparent")
        action_frame.pack(fill="x", pady=18, padx=20)

        pwd_btn = ctk.CTkButton(action_frame, text="Change Parent Password", fg_color="#334155",
                                command=self._change_password_dialog)
        pwd_btn.pack(side="left", padx=6)

        test_btn = ctk.CTkButton(action_frame, text="Test Warning Popup", fg_color="#854d0e",
                                 command=lambda: self._trigger_warning_manual())
        test_btn.pack(side="left", padx=6)

        save_info = ctk.CTkLabel(action_frame, text="All settings are auto-saved instantly.", text_color="#64748b", font=ctk.CTkFont(size=11))
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
        color = None if in_master else "#475569"
        for sw, var in [(self.notif_switch, None)]:
            # The sub switches
            pass
        # Disable/enable all cat_vars
        for cat, var in self.cat_vars.items():
            # Widgets we stored? We can just reactive change bindings
            # Instead toggle visually
            for child in self.toggle_frame.winfo_children():
                if isinstance(child, ctk.CTkSwitch) and cat in str(child.cget("text")):
                    child.configure(state=state)
        # Phone toggle
        self.notif_switch.configure(state=state)

    def _toggle_category(self, category, var):
        self.config.set_category(category, var.get())

    def _toggle_notifications(self):
        val = bool(self.notif_switch.get())
        self.config.set("phone_notifications_enabled", val and bool(self.config.get("phone_number")))

    def _autosave_phone(self):
        self.config.set("phone_number", self.phone_var.get().strip())

    def _autosave_twilio(self):
        self.config.update({
            "twilio_sid": self.tw_sid_var.get().strip(),
            "twilio_token": self.tw_token_var.get().strip(),
            "twilio_from_phone": self.tw_from_var.get().strip()
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

                result = self.detector.analyze_screenshot(img, enabled)
                if result.get("suspicious"):
                    cat = result.get("category")
                    if cat and self.config.get("enabled_categories").get(cat, False):
                        warning_msg = result.get("warning_message") or "Be careful online — you are not alone!"
                        # Save screenshot for future review
                        saved_path = save_screenshot(img, tag=cat[:15].replace(" ", "-"))
                        print(f"[TrashPandaPatrol] TRIGGERED by {cat} | saved {saved_path}")
                        # Show warning immediately
                        self._show_warning_in_thread(warning_msg)
                        # Notify SMS?
                        if self.config.get("phone_notifications_enabled") and self.config.get("phone_number"):
                            self._send_sms_alert(warning_msg, cat, saved_path)
                else:
                    pass  # nothing
            except Exception as ex:
                print("Monitor loop err:", ex)

            time.sleep(interval)

    def _show_warning_in_thread(self, message):
        # Prefer a persistent hidden main root so popup shows reliable.
        def schedule_show():
            if hasattr(self, "hidden_root") and self.hidden_root:
                self.warning_popup.root = self.hidden_root
                self.warning_popup.show(message)
            else:
                # fallback standalone popup host
                temp = ctk.CTk()
                temp.withdraw()
                self.warning_popup.root = temp
                self.warning_popup.show(message)
                temp.mainloop()

        if hasattr(self, "hidden_root") and self.hidden_root:
            self.hidden_root.after(30, schedule_show)
        else:
            threading.Thread(target=schedule_show, daemon=True).start()

    def _send_sms_alert(self, warning_text: str, category: str, img_path: str):
        phone = self.config.get("phone_number")
        if not phone or not TwilioClient:
            return
        sid = self.config.get("twilio_sid")
        token = self.config.get("twilio_token")
        from_phone = self.config.get("twilio_from_phone")
        if not (sid and token and from_phone):
            # Try plain SMS note (cannot attach)
            print("[TrashPandaPatrol] Missing Twilio creds. SMS alerts need Twilio account.")
            return
        try:
            client = TwilioClient(sid, token)
            body = f"🗑️ TRASHPANDAPATROL ALERT (Child Device)\nCategory: {category}\nWarning shown: {warning_text}\n\nCheck child's recent activity. Screenshots saved locally on device."
            msg = client.messages.create(body=body, from_=from_phone, to=phone)
            print("[TrashPandaPatrol] SMS sent:", msg.sid)
            # Note: Full MMS image can be added using a publicly hosted URL of img. We skip here to stay simple & local.
        except Exception as e:
            print("SMS send fail:", e)

    # ---- Tray Icon ----
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

        # Persistent hidden window to host dialogs/popups
        self.hidden_root = ctk.CTk()
        self.hidden_root.withdraw()

        # If first run - guide user through settings
        if self.config.is_first_run():
            self._authenticate()
            # auto open
            self.hidden_root.after(300, lambda: self.open_settings(require_auth=False))

        # Start monitoring if enabled at launch
        if self.config.get("screen_monitoring_enabled"):
            self.start_monitor()

        # Start tray in background thread so mainloop stays alive
        tray = self._create_tray()
        threading.Thread(target=tray.run, daemon=True).start()

        try:
            self.hidden_root.mainloop()
        except KeyboardInterrupt:
            self._quit_app()


if __name__ == "__main__":
    app = TrashPandaPatrolApp()
    app.run()
