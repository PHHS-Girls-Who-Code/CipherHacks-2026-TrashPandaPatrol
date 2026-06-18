# TrashPandaPatrol — Requirements Gap Report & Fix Plan

This document compares the current implementation in `RaccoonSafe/main.py` against the
requirements in `prompt.md`, lists every gap found, and gives detailed, step-by-step
instructions for how each gap will be filled.

---

## Summary Table

| # | Gap | Requirement (prompt.md line) | Severity | Fix Type |
|---|-----|------------------------------|----------|----------|
| 1 | SMS to parent does **not** attach a screenshot | line 14 ("...with a screenshot of the screen") | **High** | Code |
| 2 | "Darken" overlay fully hides the screen instead of dimming it | line 11 ("screen will darken to give focus") | Medium | Code |
| 3 | Inconsistent Gemini model names across files | line 15 ("Use the free Gemini API") | Low | Code/Docs |
| 4 | Silent failure when SMS enabled but Twilio creds missing | lines 6, 14 | Medium | Code/UX |
| 5 | Warning popup can be bypassed (Alt+F4 / taskbar) before 5s | line 13 ("cannot be dismissed for 5 seconds") | Medium | Code |
| 6 | No clear feedback when GEMINI_API_KEY is missing | line 15 | Low | Code/UX |

The core requirements (parent password, settings UI, master + per-category toggles,
fade-out behavior, auto-save, raccoon popup, 5s lockout, Gemini text analysis, local
screenshot saving) are **already implemented and satisfied**. The items below are the
remaining deviations.

---

## Gap 1 — SMS must include a screenshot (HIGH)

### What the requirement says
> line 14: "Sends an automated message to the parent's phone number **with a screenshot
> of the screen** if the parent enabled notifications sent to their phone."

### Current behavior
`_send_sms_alert()` (`main.py:643-661`) sends a **text-only** SMS. The code comment at
line 659 explicitly admits the image is skipped:
```python
# Note: Full MMS image can be added using a publicly hosted URL of img. We skip here...
```
Twilio MMS requires the image to be reachable via a public `media_url`; a local file path
cannot be attached directly.

### Fix plan
Twilio cannot read a local file, so the saved screenshot must be exposed at a temporary
public URL. We will use a **free, no-account image host upload** so no extra paid infra is
required, with a graceful fallback to text-only SMS if the upload fails.

Steps:
1. Add a helper `upload_image_temp(img_path) -> str | None` in the
   `TrashPandaPatrolApp` class (near `_send_sms_alert`).
   - Read the saved PNG bytes.
   - POST to a free anonymous host (e.g. `https://0x0.st` or `https://tmpfiles.org/api/v1/upload`)
     using `requests`.
   - Parse and return the public URL string, or `None` on any failure.
   - Wrap entirely in try/except; never raise into the monitor loop.
2. Modify `_send_sms_alert(self, warning_text, category, img_path)` (`main.py:643`):
   - After validating Twilio creds, call `media = self.upload_image_temp(img_path)`.
   - When creating the message:
     ```python
     kwargs = {"body": body, "from_": from_phone, "to": phone}
     if media:
         kwargs["media_url"] = [media]
     msg = client.messages.create(**kwargs)
     ```
   - If `media is None`, log that it fell back to text-only and still send the text SMS.
3. Add `requests>=2.32.0` to `requirements.txt`.
4. Add a short privacy note to `README.md` Limitations section: suspicious screenshots are
   briefly uploaded to a temporary public host **only** when MMS alerts are enabled, and
   are otherwise kept local.

### Acceptance criteria
- With Twilio creds + a phone number + notifications enabled, the parent receives an MMS
  containing the screenshot.
- If the upload fails or no creds, a text-only SMS is still sent (no crash).

---

## Gap 2 — "Darken" overlay should dim, not hide (MEDIUM)

### What the requirement says
> line 11: "The screen will **darken to give focus** to the warning message and will remain
> for 5 seconds before allowing the user to exit out."

"Darken to give focus" implies the underlying screen stays partly visible behind a dark,
semi-transparent veil — drawing attention to the popup without blacking everything out.

### Current behavior
`WarningPopup.show()` (`main.py:237-305`):
- Creates a fullscreen `CTkToplevel`, paints a solid `#1a1a1a` rectangle over a `Canvas`
  (lines 248-251), then sets window `-alpha` to `0.98` (line 305).
- Net effect: the real screen is almost entirely obscured (near-opaque), not "dimmed".

### Fix plan
Make the fullscreen overlay genuinely semi-transparent so the screen shows through dimmed.

Steps:
1. In `WarningPopup.show()`:
   - Remove the opaque `dim_canvas` solid rectangle fill (lines 248-252), OR keep a black
     background but rely on window transparency for the dim effect.
   - Set the overlay window transparency to a true dim value:
     ```python
     self.root.attributes("-alpha", self.dim_alpha)  # 0.65 = visible-but-dimmed
     ```
     and remove the later `self.root.after(100, ... "-alpha", 0.98)` line (305).
   - Keep the popup_frame fully opaque so the message + raccoon stay crisp. Because child
     widgets inherit window alpha on Windows, render the **popup panel on its own
     `Toplevel`** (a second always-on-top window placed in the corner) so it is NOT dimmed,
     while the fullscreen veil behind it uses `dim_alpha`.
2. Implementation detail (two-window approach):
   - `self.veil`  = fullscreen black `Toplevel`, `-alpha 0.65`, topmost, `overrideredirect`.
   - `self.panel` = corner `Toplevel`, `-alpha 1.0`, topmost, holding raccoon/message/timer.
   - Track both; destroy both in `_close_popup()`.
3. Make `dim_alpha` adjustable (already a field, `main.py:235`).

### Acceptance criteria
- When a warning fires, the desktop is clearly visible but noticeably darkened.
- The raccoon panel and text remain full-brightness and readable.
- Closing after 5s removes both the veil and the panel.

---

## Gap 3 — Inconsistent Gemini model names (LOW)

### Current state
- `main.py:133` → `gemini-flash-latest`
- `test_detection.py:6` → `gemini-flash-lite-latest`
- `README.md:5,30` → "Gemini 1.5 Flash Vision"
- `README.md:89` → `gemini-flash-lite-latest`

Three different names create confusion and risk a wrong/unavailable model.

### Fix plan
1. Pick one canonical, free-tier, vision-capable alias and define it once as a module
   constant in `main.py`:
   ```python
   GEMINI_MODEL = "gemini-flash-latest"
   ```
2. Use `GEMINI_MODEL` in `SafetyDetector.__init__` (`main.py:133`).
3. Update `test_detection.py:6` to import/use the same value (or hardcode the same string).
4. Update all `README.md` mentions (lines 5, 30, 89) to the single canonical name.

### Acceptance criteria
- Exactly one model name appears across the codebase and docs.

---

## Gap 4 — Silent failure when SMS enabled but Twilio creds missing (MEDIUM)

### Current behavior
- `_send_sms_alert()` (`main.py:650-653`) returns silently (console-only) if SID/token/
  from-phone are missing.
- In the Settings UI, the SMS toggle can be turned on with no Twilio creds and no warning,
  so a parent may believe alerts work when they do not.

### Fix plan
1. In `_toggle_notifications()` (`main.py:523`):
   - If the user enables SMS but `phone_number` is blank → show a `messagebox.showwarning`
     explaining a phone number is required, then revert the switch (`self.notif_switch.deselect()`).
   - If enabled and phone present but Twilio creds incomplete → show an informational
     `messagebox` clarifying that Twilio credentials are required for SMS to actually send,
     but still allow the toggle (warnings/screenshots still work locally).
2. Add a small status label under the Twilio frame that reads "SMS ready ✓" or
   "SMS not configured" based on whether SID/token/from-phone are all present; refresh it
   in `_autosave_twilio()` and `_autosave_phone()`.

### Acceptance criteria
- Enabling SMS without a phone number is blocked with a clear message.
- The UI clearly communicates whether SMS is actually deliverable.

---

## Gap 5 — Warning popup can be bypassed before 5s (MEDIUM)

### What the requirement says
> line 13: "Warning **cannot be dismissed** for 5 seconds so the kid reads it."

### Current behavior
`WarningPopup.show()` blocks `WM_DELETE_WINDOW`, `<Escape>`, and click (lines 300-302),
and disables the close button until countdown ends. However:
- Alt+F4 and the OS taskbar can still close/minimize an `overrideredirect` Toplevel in some
  cases.
- The window is not `grab_set()`, so focus can move to other apps and the dim/popup can be
  sent behind other windows.

### Fix plan
1. Add `self.root.grab_set()` (modal grab) when showing, released on close.
2. Bind and swallow Alt+F4: `self.root.bind("<Alt-F4>", lambda e: "break")`.
3. Re-assert topmost on a short timer during the 5s lockout
   (`self.root.after(500, reassert_topmost)`), stopping once `can_close` is True.
4. Keep the existing disabled close button + countdown.

> Note: True OS-level un-closability is not achievable from user-space Tkinter without
> elevated/global hooks; this hardens the common bypass paths to satisfy the intent.

### Acceptance criteria
- During the 5s window, Escape, click, Alt+F4, and the close button cannot dismiss it.
- After 5s, the "I understand ✓" button works.

---

## Gap 6 — Unclear feedback when GEMINI_API_KEY is missing (LOW)

### Current behavior
If `GEMINI_API_KEY` is unset, detection is silently disabled; the only signal is a console
line (`main.py:728-729`). A parent running the built `.exe` (no console) gets no indication.

### Fix plan
1. In the Settings UI header area, add a status indicator:
   - Green "AI Detection: Active" if `self.detector.is_ready()`.
   - Red "AI Detection: OFF — set GEMINI_API_KEY" otherwise, with a short tooltip/label on
     how to set it.
2. Optionally, on first run, if monitoring is enabled but the key is missing, show a
   one-time `messagebox` explaining the key must be set as an environment variable.

### Acceptance criteria
- A non-technical parent can see at a glance whether detection is actually running.

---

## Files That Will Change

| File | Changes |
|------|---------|
| `RaccoonSafe/main.py` | Gaps 1, 2, 4, 5, 6 (SMS MMS, dim overlay, UX guards, popup hardening, status labels) |
| `RaccoonSafe/requirements.txt` | Add `requests>=2.32.0` (Gap 1) |
| `RaccoonSafe/test_detection.py` | Gap 3 (single model name) |
| `RaccoonSafe/README.md` | Gaps 1, 3 (privacy note, single model name) |

---

## Suggested Implementation Order

1. **Gap 3** (model name) — trivial, removes confusion first.
2. **Gap 1** (SMS screenshot) — highest-priority requirement miss.
3. **Gap 2** (dim overlay) — visible behavior fix.
4. **Gap 5** (popup hardening) — strengthens 5s lockout.
5. **Gap 4 & 6** (UX feedback) — polish so parents trust the tool.

---

## Out of Scope / Notes
- Twilio remains optional; without it the app still shows warnings and saves screenshots
  locally, which is acceptable for a hackathon build.
- Temporary public image upload (Gap 1) is the simplest way to satisfy "screenshot to
  parent" without standing up a server; this trade-off is documented in the README.
