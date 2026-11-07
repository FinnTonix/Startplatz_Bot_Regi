#!/usr/bin/env python3
"""
Startplatz-Monitor für die Datasport-Startplatzbörse (z. B. Regensburg Halbmarathon).

Lädt die Seite, prüft per Textabgleich, ob ein Startplatz verfügbar ist,
und schickt bei Treffer eine Nachricht per Telegram und/oder WhatsApp (CallMeBot).

Konfiguration komplett über Umgebungsvariablen (in GitHub: Secrets / Variables):

  PAGE_URL            URL der Startplatzbörse (Pflicht)

  Textabgleich (mindestens eins von beiden setzen):
  NO_SPOTS_TEXT       Text, der dasteht, wenn KEIN Platz frei ist,
                      z. B. "Derzeit sind keine Startplätze verfügbar".
                      -> Alarm, sobald dieser Text VERSCHWINDET.
  SPOT_TEXT           Text, der nur dasteht, wenn ein Platz frei ist,
                      z. B. "Halbmarathon" oder "In den Warenkorb".
                      -> Alarm, sobald dieser Text AUFTAUCHT.
                      
  SECTION_START: ${{ vars.SECTION_START }}
  SECTION_END: ${{ vars.SECTION_END }}
  SECTION_OCCURRENCE: ${{ vars.SECTION_OCCURRENCE }}
  

  USE_BROWSER         "1" = Seite mit Headless-Chromium (Playwright) rendern.
                      Nötig, falls die Seite ihren Inhalt per JavaScript lädt.

  TELEGRAM_BOT_TOKEN  Token von @BotFather
  TELEGRAM_CHAT_ID    Deine Chat-ID (z. B. über @userinfobot)

  WHATSAPP_PHONE      Optional: deine Nummer im Format +49170...
  WHATSAPP_APIKEY     Optional: API-Key von CallMeBot

  STATE_FILE          Datei, in der der letzte Zustand gespeichert wird
                      (Standard: state.json) – verhindert Nachrichten-Spam.
  REMIND_EVERY_RUNS   Solange ein Platz frei ist, alle N Läufe erneut erinnern
                      (Standard: 3, 0 = nur einmal benachrichtigen).
"""

import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def env(name, default=""):
    return os.environ.get(name, default).strip()


def normalize(text):
    """Kleinbuchstaben + Whitespace zusammenfassen, damit der Abgleich robust ist."""
    return re.sub(r"\s+", " ", text).strip().lower()


def fetch_text_requests(url):
    r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9"}, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(" ")


def fetch_text_browser(url):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA, locale="de-DE")
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3000)  # kurz warten, falls noch nachgeladen wird
        text = page.inner_text("body")
        browser.close()
    return text


def extract_section(t):
    """
    Schneidet aus dem Seitentext nur den Abschnitt eines Wettbewerbs heraus:
    ab SECTION_START (z. B. "Halbmarathon") bis zum nächsten Wort aus SECTION_END
    (z. B. "Marathon,Dreiviertelmarathon,Viertelmarathon").
    Gesucht wird nach ganzen Wörtern, "Marathon" trifft also NICHT "Halbmarathon".
    Kommt SECTION_START mehrfach vor (z. B. auch im Menü), wählt SECTION_OCCURRENCE
    das n-te Vorkommen (Standard 1). Alle gefundenen Abschnitte stehen im Log.
    """
    start = normalize(env("SECTION_START"))
    if not start:
        return t  # keine Eingrenzung -> ganze Seite

    ends = [normalize(e) for e in env("SECTION_END").split(",") if e.strip()]
    starts = [m.end() for m in re.finditer(rf"\b{re.escape(start)}\b", t)]
    if not starts:
        raise RuntimeError(f'"{start}" nicht auf der Seite gefunden.')

    sections = []
    for s in starts:
        end_pos = len(t)
        for e in ends:
            m = re.compile(rf"\b{re.escape(e)}\b").search(t, s)
            if m:
                end_pos = min(end_pos, m.start())
        sections.append(t[s:end_pos])

    for i, sec in enumerate(sections, 1):
        print(f"--- Abschnitt {i} ---\n{sec[:300]}{' …' if len(sec) > 300 else ''}")

    n = int(env("SECTION_OCCURRENCE", "1") or 1)
    if n > len(sections):
        raise RuntimeError(f"SECTION_OCCURRENCE={n}, aber nur {len(sections)} Abschnitt(e) gefunden.")
    print(f">>> Geprüft wird Abschnitt {n}")
    return sections[n - 1]


def check_available(text):
    """True = Platz frei, False = kein Platz. Wirft Fehler bei fehlender Konfiguration."""
    t = normalize(text)
    no_spots = normalize(env("NO_SPOTS_TEXT"))
    spot = normalize(env("SPOT_TEXT"))

    if not no_spots and not spot:
        raise SystemExit("Bitte NO_SPOTS_TEXT und/oder SPOT_TEXT setzen.")

    # Schutz vor Fehlalarm: Wenn die Seite fast leer ist (Fehlerseite, Captcha,
    # JS nicht geladen), lieber nichts melden.
    if len(t) < 200:
        raise RuntimeError(f"Seite verdächtig kurz ({len(t)} Zeichen) – evtl. USE_BROWSER=1 setzen?")

    t = extract_section(t)
    # Leerer/winziger Abschnitt = Struktur passt nicht -> kein Alarm, sondern Fehler
    if no_spots and not spot and len(t.strip()) < 5:
        raise RuntimeError("Halbmarathon-Abschnitt ist leer – SECTION_END/SECTION_OCCURRENCE prüfen.")

    if spot:
        return spot in t
    return no_spots not in t


def send_telegram(msg):
    token, chat_id = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return False
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": msg, "disable_web_page_preview": "false"},
        timeout=20,
    )
    print("Telegram:", r.status_code, r.text[:200])
    return r.ok


def send_whatsapp(msg):
    phone, key = env("WHATSAPP_PHONE"), env("WHATSAPP_APIKEY")
    if not (phone and key):
        return False
    url = (
        "https://api.callmebot.com/whatsapp.php?"
        + urllib.parse.urlencode({"phone": phone, "text": msg, "apikey": key})
    )
    r = requests.get(url, timeout=30)
    print("WhatsApp:", r.status_code, r.text[:200])
    return r.ok


def notify(msg):
    sent = send_telegram(msg) | send_whatsapp(msg)
    if not sent:
        print("WARNUNG: Keine Benachrichtigung verschickt – Telegram/WhatsApp-Secrets prüfen.")
    return sent


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"available": False, "runs_since_notify": 0, "errors": 0}


def save_state(path, state):
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def main():
    url = env("PAGE_URL")
    if not url:
        raise SystemExit("PAGE_URL fehlt.")

    state_file = env("STATE_FILE", "state.json")
    remind_every = int(env("REMIND_EVERY_RUNS", "3") or 0)
    state = load_state(state_file)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Testmodus: python monitor.py --test  -> schickt nur eine Testnachricht
    if "--test" in sys.argv:
        notify(f"✅ Startplatz-Monitor Test ({now}) – Benachrichtigung funktioniert.\n{url}")
        return

    try:
        text = fetch_text_browser(url) if env("USE_BROWSER") == "1" else fetch_text_requests(url)
        available = check_available(text)
        state["errors"] = 0
    except Exception as e:  # Netzwerkfehler, Seite down, etc.
        print("Fehler beim Prüfen:", e)
        state["errors"] = state.get("errors", 0) + 1
        # Nach 12 Fehlern in Folge einmal Bescheid geben, damit du weißt, dass der Monitor blind ist
        if state["errors"] == 12:
            notify(f"⚠️ Startplatz-Monitor: Seite seit 12 Läufen nicht prüfbar.\nLetzter Fehler: {e}\n{url}")
        save_state(state_file, state)
        return

    print(f"[{now}] Platz verfügbar: {available}")

    if available:
        first_time = not state.get("available")
        state["runs_since_notify"] = state.get("runs_since_notify", 0) + 1
        due = first_time or (remind_every > 0 and state["runs_since_notify"] >= remind_every)
        if due:
            prefix = "🏃 STARTPLATZ FREI!" if first_time else "⏰ Erinnerung: Startplatz noch frei"
            notify(f"{prefix}\nRegensburg Halbmarathon – jetzt schnell anmelden:\n{url}")
            state["runs_since_notify"] = 0
    else:
        if state.get("available"):
            print("Platz ist wieder weg.")
        state["runs_since_notify"] = 0

    state["available"] = available
    state["last_check"] = now
    save_state(state_file, state)


if __name__ == "__main__":
    main()
