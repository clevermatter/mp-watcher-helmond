#!/usr/bin/env python3
"""
Marktplaats-watcher
===================
Checkt je zoekopdrachten op Marktplaats en stuurt een Telegram-bericht
zodra er een nieuwe advertentie verschijnt.

Gebruik:
    python marktplaats_watcher.py                  normale run (zoeken + berichten sturen)
    python marktplaats_watcher.py --test           alleen tonen wat er gestuurd zou worden
    python marktplaats_watcher.py --test-telegram  stuur een testbericht naar Telegram

Instellingen:
    zoekopdrachten.toml   je zoekopdrachten (dit bestand pas je zelf aan)
    gezien.json           advertenties die al gemeld zijn (wordt automatisch bijgehouden)

Omgevingsvariabelen (in GitHub: Settings > Secrets and variables > Actions):
    TELEGRAM_TOKEN        token van je bot (van @BotFather)
    TELEGRAM_CHAT_ID      chat-id('s), komma-gescheiden (optioneel: zonder deze
                          gebruikt het script telegram_chat_id.txt, één ontvanger
                          per regel, en vult dat bij de eerste run zelf in)

Let op: dit gebruikt de onofficiële zoek-API van de Marktplaats-website.
Als Marktplaats daar iets aan verandert, kan het script stoppen met werken.
Alleen Python-standaardbibliotheek, geen installatie nodig (Python 3.11+).
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

MAP = Path(__file__).resolve().parent
CONFIG_BESTAND = MAP / "zoekopdrachten.toml"
GEZIEN_BESTAND = MAP / "gezien.json"
CHAT_ID_BESTAND = MAP / "telegram_chat_id.txt"

ZOEK_API = "https://www.marktplaats.nl/lrp/api/search"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "nl-NL,nl;q=0.9",
}

RESULTATEN_PER_ZOEKOPDRACHT = 100  # nieuwste 100 advertenties per check
MAX_ONTHOUDEN = 500                # zoveel advertentie-id's per zoekopdracht bewaren
MAX_BERICHTEN = 10                 # max losse berichten per zoekopdracht per run
PAUZE_TUSSEN_BERICHTEN = 1.1       # Telegram staat ~1 bericht per seconde toe


# --------------------------------------------------------------------------- #
# Hulpfuncties
# --------------------------------------------------------------------------- #

def log(tekst: str) -> None:
    print(tekst, flush=True)


def verberg(waarde: str) -> None:
    """Laat GitHub deze waarde in de (openbare) logs vervangen door ***."""
    if waarde and os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::add-mask::{waarde}", flush=True)


def kort_hash(waarde: str) -> str:
    return hashlib.sha256(waarde.encode("utf-8")).hexdigest()[:12]


def in_nachtpauze(pauze: str) -> bool:
    """pauze als "02:00-06:00" (Nederlandse tijd)."""
    if not pauze:
        return False
    begin, eind = [int(t[:2]) * 60 + int(t[3:5]) for t in pauze.replace(" ", "").split("-")]
    nu = datetime.now(ZoneInfo("Europe/Amsterdam"))
    minuut = nu.hour * 60 + nu.minute
    return begin <= minuut < eind if begin < eind else (minuut >= begin or minuut < eind)


def http_json(url: str, data: dict | None = None, headers: dict | None = None,
              timeout: int = 20) -> dict:
    """GET (zonder data) of POST (met data, als JSON) en geef JSON terug."""
    body = None
    h = dict(headers or {})
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def euro(cent: int) -> str:
    """7500 -> '€ 75,00'"""
    bedrag = f"{cent / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"€ {bedrag}"


PRIJSTYPES = {
    "FREE": "Gratis",
    "FAST_BID": "Bieden",
    "RESERVED": "Gereserveerd",
    "SEE_DESCRIPTION": "Zie omschrijving",
    "NOTK": "N.o.t.k.",
    "ON_REQUEST": "Op aanvraag",
    "EXCHANGE": "Ruilen",
}


def prijs_tekst(advertentie: dict) -> str:
    info = advertentie.get("priceInfo") or {}
    soort = info.get("priceType", "")
    cent = info.get("priceCents") or 0
    if soort in ("FIXED", "MIN_BID") and cent:
        tekst = euro(cent)
        return tekst + " (bieden vanaf)" if soort == "MIN_BID" else tekst
    return PRIJSTYPES.get(soort, euro(cent) if cent else "Prijs onbekend")


def advertentie_link(advertentie: dict) -> str:
    vip = advertentie.get("vipUrl")
    if vip:
        return "https://www.marktplaats.nl" + vip
    return "https://link.marktplaats.nl/" + advertentie.get("itemId", "")


def foto_url(advertentie: dict) -> str | None:
    fotos = advertentie.get("pictures") or []
    if fotos:
        f = fotos[0]
        url = f.get("largeUrl") or f.get("mediumUrl") or f.get("extraExtraLargeUrl")
        if url:
            return url
    urls = advertentie.get("imageUrls") or []
    if urls:
        url = urls[0]
        return "https:" + url if url.startswith("//") else url
    return None


def plaats_tekst(advertentie: dict, z: dict) -> str:
    loc = advertentie.get("location") or {}
    plaats = loc.get("cityName") or ""
    afstand = loc.get("distanceMeters")
    if z.get("postcode") and isinstance(afstand, int) and afstand >= 0:
        plaats += f" ({round(afstand / 1000)} km)"
    return plaats


def zoek_link(z: dict) -> str:
    """Link naar dezelfde zoekopdracht op de website (alleen bij een zoekterm)."""
    if not z.get("zoekterm"):
        return ""
    return "https://www.marktplaats.nl/q/" + urllib.parse.quote_plus(z["zoekterm"]) + "/"


def link_regel(z: dict, tekst: str) -> str:
    link = zoek_link(z)
    return f'\n<a href="{link}">{tekst}</a>' if link else ""


def standaard_naam(z: dict) -> str:
    return z.get("zoekterm") or f"categorie {z.get('subcategorie') or z.get('categorie')}"


# --------------------------------------------------------------------------- #
# Config en geheugen
# --------------------------------------------------------------------------- #

def laad_zoekopdrachten() -> list[dict]:
    with open(CONFIG_BESTAND, "rb") as f:
        config = tomllib.load(f)
    zoekopdrachten = config.get("zoekopdracht", [])
    if not zoekopdrachten:
        sys.exit("Geen zoekopdrachten gevonden in zoekopdrachten.toml")
    namen = set()
    for z in zoekopdrachten:
        if not z.get("zoekterm") and not z.get("categorie"):
            sys.exit(f"Zoekopdracht zonder 'zoekterm' of 'categorie': {z}")
        z.setdefault("naam", standaard_naam(z))
        if z["naam"] in namen:
            sys.exit(f"Twee zoekopdrachten met dezelfde naam: {z['naam']!r}")
        namen.add(z["naam"])
    return [z for z in zoekopdrachten if z.get("actief", True)]


def laad_gezien() -> dict[str, list[str]]:
    if GEZIEN_BESTAND.exists():
        try:
            return json.loads(GEZIEN_BESTAND.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("gezien.json is beschadigd, ik begin opnieuw.")
    return {}


def bewaar_gezien(gezien: dict[str, list[str]]) -> None:
    GEZIEN_BESTAND.write_text(
        json.dumps(gezien, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# Marktplaats
# --------------------------------------------------------------------------- #

def zoek_marktplaats(z: dict) -> list[dict]:
    params: list[tuple[str, str]] = [
        ("query", z.get("zoekterm", "")),
        ("limit", str(RESULTATEN_PER_ZOEKOPDRACHT)),
        ("offset", "0"),
        ("sortBy", "SORT_INDEX"),          # sorteren op datum...
        ("sortOrder", "DECREASING"),       # ...nieuwste eerst
        ("viewOptions", "list-view"),
        ("searchInTitleAndDescription", "false" if z.get("alleen_in_titel") else "true"),
    ]
    if z.get("postcode"):
        params.append(("postcode", str(z["postcode"]).replace(" ", "").upper()))
        params.append(("distanceMeters", str(int(z.get("afstand_km", 25)) * 1000)))
    min_p, max_p = z.get("min_prijs"), z.get("max_prijs")
    if min_p is not None or max_p is not None:
        lo = "null" if min_p is None else str(int(round(min_p * 100)))
        hi = "null" if max_p is None else str(int(round(max_p * 100)))
        params.append(("attributeRanges[]", f"PriceCents:{lo}:{hi}"))
    if z.get("categorie"):
        params.append(("l1CategoryId", str(z["categorie"])))
    if z.get("subcategorie"):
        params.append(("l2CategoryIds", str(z["subcategorie"])))

    url = ZOEK_API + "?" + urllib.parse.urlencode(params)
    data = http_json(url, headers=HEADERS)
    return data.get("listings", [])


def te_oud(advertentie: dict) -> bool:
    """Ouder dan gisteren geplaatst? Marktplaats geeft alleen een tekst als datum:
    'Vandaag', 'Gisteren', 'Eergisteren' of bijvoorbeeld '28 sep 26'.
    Zo'n oude advertentie schuift soms onderaan de lijst in als er bovenaan een
    verdwijnt; die willen we niet als nieuw melden. Onbekende tekst = niet te oud."""
    datum = (advertentie.get("date") or "").strip().lower()
    return datum == "eergisteren" or bool(re.fullmatch(r"\d{1,2} \S+ \d{2,4}", datum))


def voldoet(advertentie: dict, z: dict) -> bool:
    """Extra filters die we zelf toepassen (bovenop de filters van Marktplaats)."""
    if te_oud(advertentie):
        return False
    titel = (advertentie.get("title") or "").lower()
    for woord in z.get("uitsluiten", []):
        if woord.lower() in titel:
            return False

    info = advertentie.get("priceInfo") or {}
    cent = info.get("priceCents") or 0
    if info.get("priceType") in ("FIXED", "MIN_BID") and cent:
        if z.get("max_prijs") is not None and cent > z["max_prijs"] * 100:
            return False
        if z.get("min_prijs") is not None and cent < z["min_prijs"] * 100:
            return False
    elif z.get("alleen_met_prijs"):
        return False  # 'Bieden', 'N.o.t.k.' enz. overslaan
    return True


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

class TelegramFout(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str, chat_ids: list[str], testmodus: bool = False):
        self.token = token
        self.chat_ids = chat_ids
        self.testmodus = testmodus

    def _api(self, methode: str, data: dict) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/{methode}"
        try:
            return http_json(url, data=data)
        except urllib.error.HTTPError as e:
            fout = e.read().decode("utf-8", "replace")
            raise TelegramFout(f"Telegram {methode} gaf fout {e.code}: {fout}") from None

    def _naar_iedereen(self, stuur_een) -> None:
        """Verstuur naar alle ontvangers; alleen een fout als het bij niemand lukt."""
        gelukt, laatste_fout = 0, None
        for cid in self.chat_ids:
            try:
                stuur_een(cid)
                gelukt += 1
            except TelegramFout as e:
                laatste_fout = e
                log(f"  Versturen naar {cid} mislukt: {e}")
        if not gelukt and laatste_fout:
            raise laatste_fout

    def naar(self, chat_id: str, tekst: str) -> None:
        """Bericht aan één ontvanger."""
        if self.testmodus:
            log(f"  [TEST] bericht aan {chat_id}:\n    " + tekst.replace("\n", "\n    "))
            return
        self._api("sendMessage", {
            "chat_id": chat_id, "text": tekst, "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })

    def tekst(self, tekst: str) -> None:
        if self.testmodus:
            log("  [TEST] bericht:\n    " + tekst.replace("\n", "\n    "))
            return
        self._naar_iedereen(lambda cid: self._api("sendMessage", {
            "chat_id": cid,
            "text": tekst,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }))

    def advertentie(self, advertentie: dict, z: dict) -> None:
        titel = html.escape(advertentie.get("title", "(geen titel)"))
        prijs = html.escape(prijs_tekst(advertentie))
        plaats = plaats_tekst(advertentie, z)
        omschrijving = (advertentie.get("description") or "").strip()
        if len(omschrijving) > 200:
            omschrijving = omschrijving[:200].rsplit(" ", 1)[0] + "…"

        link = advertentie_link(advertentie)
        regels = [
            f'<b><a href="{html.escape(link)}">{titel}</a></b>',
            f"<b>{prijs}</b>" + (f" · {html.escape(plaats)}" if plaats else ""),
        ]
        if omschrijving:
            regels.append(f"<i>{html.escape(omschrijving)}</i>")
        regels.append(f"Zoekopdracht: {html.escape(z['naam'])}")
        bericht = "\n".join(regels)

        if self.testmodus:
            log("  [TEST] advertentie:\n    " + bericht.replace("\n", "\n    ") + f"\n    {link}")
            return

        knop = {"inline_keyboard": [[{"text": "Bekijk op Marktplaats", "url": link}]]}
        foto = foto_url(advertentie)

        def stuur_een(cid: str) -> None:
            if foto:
                try:
                    self._api("sendPhoto", {
                        "chat_id": cid, "photo": foto, "caption": bericht,
                        "parse_mode": "HTML", "reply_markup": knop,
                    })
                    return
                except TelegramFout as e:
                    log(f"  Foto sturen lukte niet ({e}), ik stuur alleen tekst.")
            self._api("sendMessage", {
                "chat_id": cid, "text": bericht, "parse_mode": "HTML",
                "reply_markup": knop, "disable_web_page_preview": True,
            })

        self._naar_iedereen(stuur_een)

    def overzicht(self, groepen: list, kop: str | None = None) -> None:
        """Eén bericht met alle nieuwe advertenties van deze check (zo nodig in delen)."""
        totaal = sum(len(advs) for _, advs in groepen)
        blokken = [kop or f"<b>{totaal} nieuwe advertentie{'s' if totaal != 1 else ''}</b>"]
        for z, advs in groepen:
            blokken.append(f"\n<b>{html.escape(z['naam'])}</b>")
            for a in advs:
                titel = html.escape(a.get("title", "(geen titel)"))
                link = html.escape(advertentie_link(a))
                prijs = html.escape(prijs_tekst(a))
                plaats = html.escape(plaats_tekst(a, z))
                blokken.append(f'• <a href="{link}">{titel}</a>\n   <b>{prijs}</b>'
                               + (f" · {plaats}" if plaats else ""))
        berichten, huidig = [], ""
        for blok in blokken:
            if huidig and len(huidig) + len(blok) + 1 > 3800:
                berichten.append(huidig)
                huidig = ""
            huidig = f"{huidig}\n{blok}" if huidig else blok
        berichten.append(huidig)
        for i, bericht in enumerate(berichten):
            self.tekst(bericht)
            if i < len(berichten) - 1 and not self.testmodus:
                time.sleep(PAUZE_TUSSEN_BERICHTEN)


def lees_chat_ids() -> list[str]:
    """Ontvangers uit telegram_chat_id.txt: één chat-id per regel, # voor commentaar."""
    if not CHAT_ID_BESTAND.exists():
        return []
    ids = []
    for regel in CHAT_ID_BESTAND.read_text(encoding="utf-8").splitlines():
        regel = regel.split("#", 1)[0].strip()
        if regel:
            ids.append(regel)
    return ids


def haal_updates(token: str, offset: int | None = None) -> list[dict]:
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    if offset is not None:
        url += f"?offset={offset}"
    try:
        return http_json(url).get("result", [])
    except urllib.error.HTTPError as e:
        fout = e.read().decode("utf-8", "replace")
        raise TelegramFout(f"Telegram getUpdates gaf fout {e.code}: {fout}") from None


def chat_uit_update(update: dict) -> dict:
    bericht = update.get("message") or update.get("my_chat_member") or {}
    return bericht.get("chat") or {}


def zoek_chat_id(token: str, opslaan: bool = True) -> str:
    """Eerste ontvanger: de laatste persoon die de bot een bericht stuurde."""
    for update in reversed(haal_updates(token)):
        chat = chat_uit_update(update)
        if chat.get("type") == "private" and chat.get("id"):
            chat_id = str(chat["id"])
            if opslaan:
                naam = chat.get("first_name") or ""
                CHAT_ID_BESTAND.write_text(f"{chat_id}  # {naam}\n", encoding="utf-8")
            verberg(chat_id)
            log(f"Chat-id gevonden via je bericht aan de bot: {chat_id}")
            return chat_id
    return ""


def meld_aanmeldingen(token: str, tg: Telegram) -> None:
    """Laat de eerste ontvanger weten wie de bot nog meer een bericht stuurde."""
    updates = haal_updates(token)
    if not updates:
        return
    bekend, gemeld = set(tg.chat_ids), set()
    for update in updates:
        chat = chat_uit_update(update)
        cid = str(chat.get("id", ""))
        if not cid or cid in bekend or cid in gemeld:
            continue
        gemeld.add(cid)
        naam = (chat.get("title")
                or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
                or "Iemand")
        if chat.get("username"):
            naam += f" (@{chat['username']})"
        verberg(cid)
        log("Nieuwe aanmelding doorgegeven aan de eigenaar.")
        tg.naar(tg.chat_ids[0],
                f"<b>{html.escape(naam)}</b> heeft je bot een bericht gestuurd.\n"
                f"Moet diegene ook meldingen krijgen? Voeg <code>{cid}</code> toe aan het "
                f"secret TELEGRAM_CHAT_ID (komma ertussen), of geef het nummer aan Claude.")
    if not tg.testmodus:
        # bevestigen, zodat dezelfde berichten de volgende keer niet opnieuw gemeld worden
        haal_updates(token, offset=updates[-1]["update_id"] + 1)


def welkom_nieuwe_ontvangers(tg: Telegram, gezien: dict) -> None:
    gewelkomd = gezien.setdefault("_welkom", [])  # gehasht, zodat er geen chat-id's in staan
    for cid in tg.chat_ids[1:]:
        if cid not in gewelkomd and kort_hash(cid) not in gewelkomd:
            tg.naar(cid, "Welkom! Je krijgt vanaf nu ook de meldingen van de Marktplaats-watcher.")
            gewelkomd.append(kort_hash(cid))
            log("Welkomstbericht gestuurd naar een nieuwe ontvanger.")


# --------------------------------------------------------------------------- #
# Hoofdprogramma
# --------------------------------------------------------------------------- #

def verwerk(z: dict, gezien: dict[str, list[str]], tg: Telegram, testmodus: bool,
            overzicht: list | None = None) -> None:
    naam = z["naam"]
    advertenties = zoek_marktplaats(z)
    ids_nu = [a["itemId"] for a in advertenties if a.get("itemId")]
    log(f"- {naam}: {len(advertenties)} advertenties opgehaald")

    # Nieuwe zoekopdracht: alles wat er nu staat als 'gezien' markeren,
    # zodat je niet in één keer 30 berichten krijgt.
    if naam not in gezien:
        tg.tekst(
            f"Zoekopdracht <b>{html.escape(naam)}</b> staat aan.\n"
            f"Er staan nu {len(ids_nu)} advertenties online; "
            f"vanaf nu krijg je een bericht bij elke nieuwe."
            + link_regel(z, "Bekijk huidige resultaten")
        )
        gezien[naam] = ids_nu[:MAX_ONTHOUDEN]  # pas na gelukt bericht
        log(f"  nieuwe zoekopdracht, {len(ids_nu)} advertenties als gezien gemarkeerd")
        return

    al_gezien = set(gezien[naam])
    nieuw = [a for a in advertenties if a.get("itemId") and a["itemId"] not in al_gezien]

    # Advertenties die een andere zoekopdracht al meldde (of in deze check meldt)
    # niet nog een keer sturen. Zet daarom brede zoekopdrachten onderaan in
    # zoekopdrachten.toml: die melden dan alleen wat de rest gemist heeft.
    elders = {i for k, ids in gezien.items() if k != naam and not k.startswith("_") for i in ids}
    if overzicht:
        elders |= {a["itemId"] for _, advs in overzicht for a in advs}
    dubbel = [a for a in nieuw if a["itemId"] in elders]
    nieuw = [a for a in nieuw if a["itemId"] not in elders]
    for a in dubbel:
        gezien[naam].insert(0, a["itemId"])

    passend = [a for a in nieuw if voldoet(a, z)]
    log(f"  {len(nieuw)} nieuw, waarvan {len(passend)} voldoen aan je filters"
        + (f" ({len(dubbel)} al gemeld via een andere zoekopdracht)" if dubbel else ""))

    # Niet-passende nieuwe advertenties ook onthouden, zodat ze niet steeds terugkomen
    passend_ids = {a["itemId"] for a in passend}
    for a in nieuw:
        if a["itemId"] not in passend_ids:
            gezien[naam].insert(0, a["itemId"])

    # Overzicht-stand: verzamelen, aan het eind van de run in één bericht sturen
    if overzicht is not None:
        if passend:
            overzicht.append((z, passend))
        gezien[naam] = gezien[naam][:MAX_ONTHOUDEN]
        return

    # Oudste eerst sturen, zodat de nieuwste onderaan in je chat staat
    te_sturen = list(reversed(passend))
    overig = []
    if len(te_sturen) > MAX_BERICHTEN:
        overig, te_sturen = te_sturen[:-MAX_BERICHTEN], te_sturen[-MAX_BERICHTEN:]

    if overig:
        tg.tekst(
            f"<b>{html.escape(naam)}</b>: {len(passend)} nieuwe advertenties, "
            f"ik stuur de nieuwste {MAX_BERICHTEN}."
            + link_regel(z, "Bekijk alles op Marktplaats")
        )
        for a in overig:
            gezien[naam].insert(0, a["itemId"])

    for a in te_sturen:
        tg.advertentie(a, z)
        # pas na succesvol versturen onthouden; mislukt het, dan volgende keer opnieuw
        gezien[naam].insert(0, a["itemId"])
        if not testmodus:
            time.sleep(PAUZE_TUSSEN_BERICHTEN)

    gezien[naam] = gezien[naam][:MAX_ONTHOUDEN]


def main() -> int:
    parser = argparse.ArgumentParser(description="Marktplaats-watcher met Telegram-meldingen")
    parser.add_argument("--test", action="store_true",
                        help="niets versturen of opslaan, alleen laten zien wat er zou gebeuren")
    parser.add_argument("--test-telegram", action="store_true",
                        help="stuur een testbericht om je Telegram-instellingen te controleren")
    args = parser.parse_args()

    if os.environ.get("AUTOMATISCH") == "true":
        pauze = laad_instellingen().get("nachtpauze", "")
        if in_nachtpauze(pauze):
            log(f"Nachtpauze ({pauze}), deze automatische check wordt overgeslagen.")
            return 0

    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_ids = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    if not args.test:
        if not token:
            log("TELEGRAM_TOKEN ontbreekt. Zet het token van je bot als secret in GitHub "
                "(Settings > Secrets and variables > Actions).")
            return 1
        if not chat_ids:
            chat_ids = lees_chat_ids()
        if not chat_ids:
            try:
                gevonden = zoek_chat_id(token)
            except TelegramFout as e:
                log(f"{e}\nControleer of TELEGRAM_TOKEN klopt.")
                return 1
            chat_ids = [gevonden] if gevonden else []
        if not chat_ids:
            log("Geen chat-id gevonden. Stuur je bot in Telegram eerst een bericht "
                "(bijvoorbeeld 'hoi') en start de run opnieuw.")
            return 1

    for cid in chat_ids:
        verberg(cid)
    tg = Telegram(token, chat_ids, testmodus=args.test)

    if args.test_telegram:
        tg.tekst("Het werkt! Je Marktplaats-watcher kan je berichten sturen.")
        log("Testbericht verstuurd.")
        return 0

    zoekopdrachten = laad_zoekopdrachten()
    gezien = laad_gezien()
    if not args.test:
        try:
            meld_aanmeldingen(token, tg)
            welkom_nieuwe_ontvangers(tg, gezien)
        except TelegramFout as e:
            log(f"  Aanmeldingen of welkomstbericht versturen mislukt: {e}")
    overzicht = [] if laad_instellingen().get("berichten", "overzicht") == "overzicht" else None
    fouten = 0
    telegram_fout = False

    for z in zoekopdrachten:
        try:
            verwerk(z, gezien, tg, args.test, overzicht)
        except TelegramFout as e:
            fouten += 1
            telegram_fout = True
            log(f"  FOUT bij versturen voor '{z['naam']}': {e}")
        except urllib.error.HTTPError as e:
            fouten += 1
            log(f"  FOUT bij Marktplaats voor '{z['naam']}': HTTP {e.code}")
        except Exception as e:  # noqa: BLE001 - één mislukte zoekopdracht mag de rest niet stoppen
            fouten += 1
            log(f"  FOUT bij '{z['naam']}': {e}")
        finally:
            if not args.test:
                bewaar_gezien(gezien)  # na elke zoekopdracht opslaan
        time.sleep(1)  # rustig aan richting Marktplaats

    if overzicht:
        try:
            tg.overzicht(overzicht)
            # pas na succesvol versturen onthouden; mislukt het, dan volgende keer opnieuw
            for z, advs in overzicht:
                gezien[z["naam"]] = ([a["itemId"] for a in advs] + gezien[z["naam"]])[:MAX_ONTHOUDEN]
            log(f"Overzicht verstuurd met {sum(len(a) for _, a in overzicht)} advertenties.")
        except TelegramFout as e:
            telegram_fout = True
            log(f"  FOUT bij versturen van het overzicht: {e}")

    if os.environ.get("VOORBEELD") == "true" and not telegram_fout:
        try:
            groepen = [(z, zoek_marktplaats(z)[:3]) for z in zoekopdrachten]
            tg.overzicht(groepen, kop="<b>Testbericht</b>: de watcher werkt. Zo ziet een "
                                      "melding eruit (de 3 nieuwste per zoekopdracht):")
            log("Testbericht verstuurd.")
        except (TelegramFout, urllib.error.HTTPError) as e:
            telegram_fout = True
            log(f"  Testbericht mislukt: {e}")

    # Zoekopdrachten die uit het bestand zijn gehaald ook uit het geheugen halen
    namen = {z["naam"] for z in laad_zoekopdrachten_alle()}
    for naam in list(gezien):
        if naam not in namen and not naam.startswith("_"):
            del gezien[naam]
    if not args.test:
        bewaar_gezien(gezien)

    if telegram_fout:
        log("Berichten versturen via Telegram mislukte. Controleer je token en chat-id.")
        return 1
    if fouten and fouten == len(zoekopdrachten):
        log("Alle zoekopdrachten mislukten.")
        return 1
    log("Klaar.")
    return 0


def laad_zoekopdrachten_alle() -> list[dict]:
    """Ook de zoekopdrachten met actief = false (die willen we niet vergeten)."""
    with open(CONFIG_BESTAND, "rb") as f:
        alle = tomllib.load(f).get("zoekopdracht", [])
    for z in alle:
        z.setdefault("naam", standaard_naam(z))
    return alle


def laad_instellingen() -> dict:
    """Algemene instellingen bovenin zoekopdrachten.toml (alles behalve de zoekopdrachten)."""
    with open(CONFIG_BESTAND, "rb") as f:
        config = tomllib.load(f)
    return {k: v for k, v in config.items() if k != "zoekopdracht"}


if __name__ == "__main__":
    sys.exit(main())
