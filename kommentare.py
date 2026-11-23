"""Kommentar-Automatik für Exelery (läuft alle 15 Minuten in GitHub Actions).

Wer unter einem Exelery-Beitrag das Stichwort (Standard: EXELERY) kommentiert, bekommt einmal
automatisch eine Direktnachricht mit dem Wartelisten-Link (Instagram „Private Reply“) und eine
kurze öffentliche Antwort unter seinem Kommentar.

Texte stehen in kommentare/nachricht.txt (Direktnachricht) und kommentare/antwort.txt (öffentliche
Antwort) und lassen sich ohne Programmierung ändern. Erledigte Kommentare werden nur als Prüfsumme
gespeichert (kommentare/erledigt.txt), keine Namen, keine Texte.

Geheimnisse (Secrets dieses Repositorys): IG_TOKEN, IG_USER_ID, TG_BOT_TOKEN, TG_CHAT_ID.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

GRAPH = "https://graph.facebook.com/v27.0"
HIER = os.path.dirname(os.path.abspath(__file__))
ORDNER = os.path.join(HIER, "kommentare")
ERLEDIGT = os.path.join(ORDNER, "erledigt.txt")
WARNUNG = os.path.join(ORDNER, "letzte_warnung.txt")
STICHWORT = os.environ.get("STICHWORT", "EXELERY").strip() or "EXELERY"
TAGE = 7          # Instagram erlaubt die Direktnachricht nur bis 7 Tage nach dem Kommentar
BEITRAEGE = 12    # so viele neueste Beiträge werden durchsucht

NACHRICHT = ("Hey, danke für deinen Kommentar! 🙌\n\nHier ist dein Link zur Exelery-Warteliste:\n"
             "https://exelery.de/?utm_source=instagram&utm_medium=dm&utm_campaign=kommentar\n\n"
             "Trag dich ein, dann bist du beim Start dabei.")
ANTWORT = "Schau in deine Nachrichten 📩"


def geheim(name: str) -> str:
    return os.environ.get(name, "").strip()


def maskiert(text: str) -> str:
    for name in ("IG_TOKEN", "TG_BOT_TOKEN"):
        if geheim(name):
            text = text.replace(geheim(name), "***")
    return text


def text_datei(name: str, standard: str) -> str:
    pfad = os.path.join(ORDNER, name)
    if os.path.exists(pfad):
        t = open(pfad, encoding="utf-8").read().strip()
        if t:
            return t
    return standard


class GraphFehler(Exception):
    def __init__(self, code, nachricht):
        super().__init__(f"{code}: {nachricht}")
        self.code = code
        self.nachricht = nachricht


def graph(methode: str, pfad: str, felder: dict | None = None, token: str = "", json_body: dict | None = None) -> dict:
    token = token or geheim("IG_TOKEN")
    url = f"{GRAPH}/{pfad}"
    if methode == "GET":
        anfrage = urllib.request.Request(url + "?" + urllib.parse.urlencode(dict(felder or {}, access_token=token)))
    elif json_body is not None:
        anfrage = urllib.request.Request(url + "?" + urllib.parse.urlencode({"access_token": token}),
                                         data=json.dumps(json_body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
    else:
        anfrage = urllib.request.Request(url, data=urllib.parse.urlencode(dict(felder or {}, access_token=token)).encode(),
                                         method="POST")
    try:
        with urllib.request.urlopen(anfrage, timeout=60) as antwort:
            return json.loads(antwort.read())
    except urllib.error.HTTPError as fehler:
        try:
            e = json.loads(fehler.read()).get("error", {})
        except Exception:  # noqa: BLE001
            e = {}
        raise GraphFehler(e.get("code", fehler.code), maskiert(str(e.get("message", "")))[:300]) from fehler


def telegram(text: str) -> None:
    token, chat = geheim("TG_BOT_TOKEN"), geheim("TG_CHAT_ID")
    if not token or not chat:
        return
    daten = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000]}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=daten, timeout=30).read()
    except Exception as fehler:  # noqa: BLE001
        print("Telegram:", maskiert(str(fehler)))


def warnen(text: str) -> None:
    """Höchstens eine Warnung pro Tag, damit der Chat nicht vollläuft."""
    heute = dt.date.today().isoformat()
    if os.path.exists(WARNUNG) and open(WARNUNG).read().strip() == heute:
        print("Warnung heute schon geschickt:", text)
        return
    with open(WARNUNG, "w") as fh:
        fh.write(heute)
    telegram("⚠️ Kommentar-Automatik: " + text)


def pruefsumme(kommentar_id: str) -> str:
    return hashlib.sha256(("exelery-" + kommentar_id).encode()).hexdigest()[:24]


def seiten_token(ig: str) -> tuple[str, str]:
    """Für Direktnachrichten braucht Meta das Token der verknüpften Facebook-Seite."""
    try:
        j = graph("GET", "me/accounts", {"fields": "id,access_token,instagram_business_account", "limit": 50})
        for seite in j.get("data") or []:
            if (seite.get("instagram_business_account") or {}).get("id") == ig:
                return seite.get("access_token", ""), seite.get("id", "")
    except GraphFehler as fehler:
        print("Seiten-Token nicht abrufbar:", fehler)
    return "", ""


def privat_antworten(ig: str, seite: str, seiten_tok: str, kommentar_id: str, text: str) -> None:
    body = {"recipient": {"comment_id": kommentar_id}, "message": {"text": text}}
    versuche = []
    if seiten_tok:
        versuche += [(seite, seiten_tok), (ig, seiten_tok)]
    versuche.append((ig, geheim("IG_TOKEN")))
    letzter = None
    for ziel, tok in versuche:
        if not ziel:
            continue
        try:
            graph("POST", f"{ziel}/messages", token=tok, json_body=body)
            return
        except GraphFehler as fehler:
            letzter = fehler
    raise letzter or GraphFehler(0, "keine Verbindung")


def main() -> None:
    ig = geheim("IG_USER_ID")
    if not geheim("IG_TOKEN") or not ig:
        print("IG_TOKEN oder IG_USER_ID fehlt in den Secrets dieses Repositorys. Nichts zu tun.")
        return
    os.makedirs(ORDNER, exist_ok=True)
    erledigt = set(open(ERLEDIGT).read().split()) if os.path.exists(ERLEDIGT) else set()
    nachricht = text_datei("nachricht.txt", NACHRICHT)
    antwort = text_datei("antwort.txt", ANTWORT)
    muster = re.compile(r"(?<![\w])" + re.escape(STICHWORT) + r"(?![\w])", re.IGNORECASE)
    jetzt = dt.datetime.now(dt.timezone.utc)

    try:
        ich = graph("GET", ig, {"fields": "username"}).get("username", "")
        beitraege = graph("GET", f"{ig}/media", {"fields": "id,timestamp,permalink", "limit": BEITRAEGE}).get("data") or []
    except GraphFehler as fehler:
        warnen(f"Instagram antwortet nicht ({fehler}). Ist der Token abgelaufen?")
        return
    seiten_tok, seite = seiten_token(ig)

    verschickt, neu_erledigt, probleme = [], [], []
    for b in beitraege:
        try:
            alter = jetzt - dt.datetime.fromisoformat(b["timestamp"].replace("+0000", "+00:00"))
        except Exception:  # noqa: BLE001
            alter = dt.timedelta(0)
        if alter > dt.timedelta(days=60):
            continue
        try:
            kommentare = graph("GET", f"{b['id']}/comments",
                               {"fields": "id,text,timestamp,username", "limit": 50}).get("data") or []
        except GraphFehler as fehler:
            probleme.append(f"Kommentare nicht lesbar ({fehler})")
            break
        for k in kommentare:
            kid, text = k.get("id", ""), str(k.get("text") or "")
            if not kid or pruefsumme(kid) in erledigt or not muster.search(text):
                continue
            if ich and k.get("username", "").lower() == ich.lower():
                continue
            try:
                k_alter = jetzt - dt.datetime.fromisoformat(k["timestamp"].replace("+0000", "+00:00"))
            except Exception:  # noqa: BLE001
                k_alter = dt.timedelta(0)
            if k_alter > dt.timedelta(days=TAGE):
                neu_erledigt.append(pruefsumme(kid))
                continue
            try:
                privat_antworten(ig, seite, seiten_tok, kid, nachricht)
            except GraphFehler as fehler:
                probleme.append(f"Direktnachricht an @{k.get('username', '?')} ging nicht ({fehler})")
                if str(fehler.code) in ("10", "200", "3", "230", "190"):
                    break  # fehlende Rechte oder Token: nicht weiter probieren
                neu_erledigt.append(pruefsumme(kid))  # einmalige Ablehnung, nicht endlos wiederholen
                continue
            try:
                graph("POST", f"{kid}/replies", {"message": antwort})
            except GraphFehler as fehler:
                print("Öffentliche Antwort ging nicht:", fehler)
            verschickt.append("@" + k.get("username", "?"))
            neu_erledigt.append(pruefsumme(kid))

    if neu_erledigt:
        with open(ERLEDIGT, "a") as fh:
            fh.write("\n".join(neu_erledigt) + "\n")
    if verschickt:
        telegram(f"📩 Wartelisten-Link verschickt ({len(verschickt)}× „{STICHWORT}“): " + ", ".join(verschickt))
    if probleme:
        hinweis = ""
        if any("10" in p or "200" in p or "3:" in p or "230" in p for p in probleme):
            hinweis = (" Vermutlich fehlen dem Token die Rechte instagram_manage_comments, instagram_manage_messages "
                       "und pages_messaging, oder Instagram erlaubt den Zugriff auf Nachrichten noch nicht "
                       "(Einstellungen → Nachrichten → Verbundene Tools). Siehe Anleitung Kommentar-Automatik.")
        warnen("; ".join(probleme[:3]) + hinweis)
    print(f"Beiträge: {len(beitraege)}, verschickt: {len(verschickt)}, Probleme: {len(probleme)}")


if __name__ == "__main__":
    main()
