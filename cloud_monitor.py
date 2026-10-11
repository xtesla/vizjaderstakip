"""Vizja -> existing Google Calendar, stateless GitHub Actions worker.

Uses a fresh external Chrome/Chromium with CDP, not a browser launched through
Playwright. This is deliberate: the university SSO popup behaved differently in
Playwright-managed browsers during local v2 testing.

Never commit credentials or token files. Secrets are read from environment.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

WARSAW = ZoneInfo("Europe/Warsaw")
EXTRANET = "https://extranet.vizja.net/"
SIS_URL = "https://mojeaeh.vizja.pl/vizja-stud-app/ledge/view/stud.StartPage?action=security.authentication.ImapLogin"
API_PATH = "/vizja-stud-app/ledge/view/AJAX"
def vizja_identifiers():
    """Never keep personal/student identifiers in a public repository."""
    raw_person = os.environ.get("VIZJA_PERSON_ID", "").strip()
    raw_semester = os.environ.get("VIZJA_SEMESTER_ID", "").strip()
    if not raw_person.isdecimal() or not raw_semester.isdecimal():
        raise RuntimeError("VIZJA_PERSON_ID ve VIZJA_SEMESTER_ID secrets eksik veya hatalı")
    return int(raw_person), int(raw_semester)
WEEKS = 4
CDP_PORT = 19223
GOOGLE_SCOPE = "https://www.googleapis.com/auth/calendar.app.created"

JS_FETCH = r"""async ({personId, semesterId, weekStart}) => {
  const res = await fetch('/vizja-stud-app/ledge/view/AJAX', {
    method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type':'application/x-www-form-urlencoded', 'X-Requested-With':'XMLHttpRequest'},
    body: JSON.stringify({service:'Planowanie', method:'getOpublikowaneSpotkaniaOsoby',
      params:{idOsoby:personId,idSemestru:semesterId,poczatekTygodnia:weekStart}})
  });
  if (!res.ok) throw Error(`Vizja HTTP ${res.status}`);
  const data = await res.json();
  if (!data || data.exceptionClass || !Array.isArray(data.returnedValue?.items))
    throw Error('Vizja yanıtı geçersiz veya oturum sona ermiş.');
  return data.returnedValue.items;
}"""


def week_starts(now=None):
    now = now or datetime.now(WARSAW)
    monday = (now - timedelta(days=now.weekday())).date()
    return [int(datetime.combine(monday + timedelta(weeks=n), datetime.min.time(), WARSAW).timestamp() * 1000)
            for n in range(WEEKS)]


def parse_items(items):
    parsed = {}
    for v in items:
        key = v.get("idSpotkania") or {}
        key = key.get("idSpotkania") if isinstance(key, dict) else key
        if key is None:
            raise ValueError("Verbis kaydının ID alanı eksik")
        start, end = v["dataRozpoczecia"], v["dataZakonczenia"]
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end <= start:
            raise ValueError("Verbis kaydında tarih hatası")
        parsed[str(key)] = {
            "id": str(key), "title": v.get("nazwaPelnaPrzedmiotu", ""),
            "start": datetime.fromtimestamp(start / 1000, WARSAW).isoformat(timespec="seconds"),
            "end": datetime.fromtimestamp(end / 1000, WARSAW).isoformat(timespec="seconds"),
            "start_ms": int(start),
            "rooms": sorted({x.get("nazwaSkrocona", "") for x in v.get("sale", [])}),
            "teachers": sorted({x.get("stopienImieNazwisko", "") for x in v.get("wykladowcy", [])}),
            "groups": sorted({x.get("nazwaGrupyZajeciowej", "") for x in v.get("grupyZajeciowe", [])}),
            "status": v.get("status"),
        }
    return parsed


def chrome_binary():
    env = os.environ.get("VIZJA_CHROME_BINARY")
    if env:
        if not Path(env).is_file():
            raise RuntimeError("VIZJA_CHROME_BINARY mevcut bir dosyaya işaret etmiyor")
        return env
    for name in ("google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    if sys.platform == "win32":
        for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(key)
            if base:
                exe = Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
                if exe.is_file():
                    return str(exe)
    raise RuntimeError("Google Chrome bulunamadı")


def start_external_chrome(profile_dir):
    cmd = [chrome_binary(), "--remote-debugging-address=127.0.0.1",
           f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={profile_dir}",
           "--no-default-browser-check", "--no-first-run", "--disable-popup-blocking",
           "--window-size=1200,900", EXTRANET]
    if sys.platform != "win32":
        cmd += ["--no-sandbox", "--disable-dev-shm-usage"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if proc.poll() is not None:
            raise RuntimeError("Chrome açılamadı (CI için xvfb-run gerekli olabilir)")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=1) as response:
                if response.status == 200:
                    return proc
        except Exception:
            time.sleep(0.3)
    proc.terminate()
    raise RuntimeError("Chrome CDP bağlantısı 30 saniye içinde açılmadı")


def chrome_page_hosts():
    """Return only sanitized Chrome tab host names; never return SSO query strings."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=2) as reply:
            targets = json.load(reply)
        return sorted({urlsplit(t.get("url", "")).hostname or "about:blank"
                       for t in targets if t.get("type") == "page"})
    except Exception:
        return []


def wait_for_sis(context, timeout_sec=65):
    """Wait for the new SIS tab, reporting progress instead of appearing frozen.

    Chrome's tab list may change ahead of Playwright CDP's page list during SSO
    navigation. If Chrome can see the completed SIS page while Playwright cannot,
    open the already-authenticated SIS *home* in the same temporary profile.
    This does not call the Extranet SSO endpoint again.
    """
    start = time.monotonic()
    deadline = start + timeout_sec
    next_log = start + 5
    attempted_recovery = False
    while time.monotonic() < deadline:
        pages = list(context.pages)
        for page in pages:
            try:
                hostname = urlsplit(page.url).hostname
                if hostname == "mojeaeh.vizja.pl" and "/vizja-stud-app/" in page.url:
                    print("[SSO] Verbis sayfası Playwright tarafından algılandı.", flush=True)
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=2500)
                    except Exception:
                        pass
                    return page
            except Exception:
                continue  # The new popup can navigate while inspected.

        now = time.monotonic()
        if now >= next_log:
            hosts = chrome_page_hosts()
            safe_hosts = ", ".join(hosts) or "bilinmiyor"
            print(f"[SSO] Verbis bekleniyor ({int(now-start)} sn); "
                  f"Playwright sekmesi: {len(pages)}; Chrome alan adları: {safe_hosts}", flush=True)
            next_log = now + 5

            if (not attempted_recovery and now-start >= 13 and
                    "mojeaeh.vizja.pl" in hosts):
                attempted_recovery = True
                print("[SSO] Chrome Verbis'i görüyor fakat otomasyon henüz algılamadı. "
                      "Aynı oturumda Verbis ana sayfası kontrol ediliyor...", flush=True)
                try:
                    fresh = context.new_page()
                    fresh.goto("https://mojeaeh.vizja.pl/vizja-stud-app/ledge/view/stud.StartPage",
                               wait_until="domcontentloaded", timeout=12000)
                    if (urlsplit(fresh.url).hostname == "mojeaeh.vizja.pl" and
                            "/vizja-stud-app/" in fresh.url):
                        print("[SSO] Verbis aynı Chrome oturumuyla yeniden erişilebilir.", flush=True)
                        return fresh
                except Exception:
                    print("[SSO] Yedek sekme açma denenemedi; normal beklemeye dönülüyor.", flush=True)
        time.sleep(0.35)
    raise RuntimeError("Verbis Chrome'da açılsa bile otomasyon sekmeye erişemedi. "
                       "Yukarıdaki [SSO] sekme sayılarını paylaşın (tam URL/cookie değil).")


# Extranet displays a centered SIS modal. There can be TWO matching links:
# a background page link and the dialog's link. Playwright is_visible() does
# not imply that a link receives pointer events (the overlay may cover it).
SIS_CANDIDATE_JS = r"""() => {
  const links = Array.from(document.querySelectorAll('a'));
  const label = /log\s+in\s+to\s+the\s+new\s+sis/i;
  return links.flatMap((a, index) => {
    const name = (a.innerText || a.textContent || '').trim().replace(/\s+/g, ' ');
    const href = a.getAttribute('href') || '';
    if (!label.test(name) && !/ImapLogin/i.test(href)) return [];
    const r = a.getBoundingClientRect();
    const style = getComputedStyle(a);
    const visible = r.width > 0 && r.height > 0 &&
                    style.display !== 'none' && style.visibility !== 'hidden';
    if (!visible) return [];
    const hit = document.elementFromPoint(r.left + r.width/2, r.top + r.height/2);
    const unobstructed = Boolean(hit && (a === hit || a.contains(hit)));
    const modal = Boolean(a.closest(
      '[role="dialog"], .ui-dialog, .modal, .modal-dialog, .bootbox, ' +
      '.fancybox-content, .jconfirm-box, .ajs-dialog, .popup-dialog, .dialog'
    ));
    // Exact text helps avoid another nearby navigation item.
    const exact = label.test(name) && name.toLowerCase().startsWith('log in to the new sis');
    return [{index, modal, unobstructed, exact}];
  });
}"""


def get_sis_candidates(context, extranet_domain="extranet.vizja.net"):
    """Return ranked real <a> nodes, without logging URLs or credentials."""
    candidates = []
    for page in list(context.pages):
        if extranet_domain not in page.url:
            continue
        for frame in list(page.frames):
            try:
                descriptors = frame.evaluate(SIS_CANDIDATE_JS)
                for data in descriptors:
                    score = (100 if data["unobstructed"] else 0) + (40 if data["modal"] else 0) + (10 if data["exact"] else 0)
                    candidates.append((score, data, frame.locator('a').nth(data['index']), page, frame))
            except Exception:
                pass  # Frame may have navigated during inspection.
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates


def click_sis_anchor(context, candidate):
    """Click exactly the selected SIS modal link, falling back to Enter.

    Pressing Enter after focusing a link provides a keyboard activation when a
    Chromium overlay prevents a pointer click. Do not use direct cross-site
    navigation, which may skip university SSO state.
    """
    _, data, anchor, page, frame = candidate
    print(f"[SSO] Seçilen bağlantı: modal={data['modal']}, üzeri açık={data['unobstructed']}, iframe={frame != page.main_frame}.")
    try:
        anchor.click(timeout=4000, no_wait_after=True)
        print("[SSO] Bağlantıya tıklandı; Verbis sekmesi kontrol ediliyor (en fazla 65 sn).", flush=True)
    except Exception:
        print("[SSO] Fare tıklaması engellendi; seçilen SIS bağlantısı DOM üzerinden açılıyor.", flush=True)
        try:
            # Clicking the previously identified real anchor runs the site's
            # own handler. No cookies, credentials, or SSO URLs are extracted.
            anchor.evaluate("(element) => element.click()", timeout=5000)
            print("[SSO] SIS bağlantısı DOM üzerinden etkinleştirildi.", flush=True)
        except Exception:
            try:
                anchor.focus(timeout=3000)
                page.keyboard.press("Enter")
                print("[SSO] SIS bağlantısı klavyeyle etkinleştirildi.", flush=True)
            except Exception as exc:
                raise RuntimeError("SIS bağlantısı fare, DOM veya klavye ile açılamadı") from exc



def print_sis_diagnostics(context):
    """Log safe counts only; never expose SSO query strings or cookies."""
    for page in list(context.pages):
        if "extranet.vizja.net" not in page.url:
            continue
        try:
            print("[SSO] Extranet sekmesi bulundu; hassas URL bilgileri gizlendi.")
            for frame in list(page.frames):
                try:
                    details = frame.evaluate(r"""() => {
                      const elements = Array.from(document.querySelectorAll('a, [role=link]'));
                      const rel = elements.filter(x => /new\s+SIS|ImapLogin/i.test(
                        (x.innerText || x.textContent || '') + ' ' + (x.getAttribute('href') || '')));
                      return { links: elements.length, matches: rel.length,
                        visible: rel.filter(x => !!x.getClientRects().length).length };
                    }""")
                    print(f"[SSO] Çerçeve: bağlantı={details['links']}, SIS eşleşmesi={details['matches']}, görünür={details['visible']}")
                except Exception:
                    print("[SSO] Bir çerçevenin DOM bilgisi alınamadı.")
        except Exception:
            pass


def auto_sign_in(context, username, password):
    page = next((p for p in context.pages if "extranet.vizja.net" in p.url), None)
    if page is None:
        page = context.new_page()
    page.goto(EXTRANET, wait_until="domcontentloaded", timeout=50000)
    page.locator("#login").wait_for(timeout=20000)
    page.locator("#login").fill(username)
    page.locator("#haslo").fill(password)
    page.locator('input[type="submit"]').click(timeout=20000)
    try:
        page.locator("#login").wait_for(state="hidden", timeout=30000)
    except Exception as exc:
        raise RuntimeError("Extranet giriş formu kapanmadı. Şifre/CAPTCHA veya giriş engeli olabilir") from exc
    print("[SSO] Extranet giriş formu kapandı; yeni SIS bağlantısı aranıyor...")

    # Prefer the SIS anchor inside the centered modal. Linux/Xvfb can
    # report elementFromPoint() as blocked even when the anchor is visible;
    # do not require unobstructed=True for modal links.
    deadline = time.monotonic() + 35
    first_seen = None
    chosen = None
    while time.monotonic() < deadline:
        candidates = get_sis_candidates(context)
        if candidates:
            if first_seen is None:
                first_seen = time.monotonic()
            chosen = next((c for c in candidates if c[1]["modal"] and c[1]["exact"]), None)
            if chosen is None:
                chosen = next((c for c in candidates if c[1]["modal"]), None)
            if chosen is not None:
                print(f"[SSO] Modal SIS bağlantısı bulundu; aday sayısı: {len(candidates)}.", flush=True)
                break
            # Some Extranet themes use an unlabelled popup container. Allow
            # the dialog to settle, then select the last exact SIS anchor:
            # the dynamically appended popup link follows the page link.
            if time.monotonic() - first_seen >= 5:
                exact = [c for c in candidates if c[1]["exact"]]
                if exact:
                    chosen = max(exact, key=lambda c: c[1]["index"])
                else:
                    chosen = candidates[0]
                print(f"[SSO] Modal işareti bulunmadı; {len(candidates)} görünür SIS bağlantısından biri seçildi.", flush=True)
                break
        time.sleep(0.4)
    if chosen is None:
        print_sis_diagnostics(context)
        raise RuntimeError("SIS bağlantısı DOM içinde bulunamadı; tıklama yapılmadı")

    try:
        click_sis_anchor(context, chosen)
    except Exception as exc:
        print_sis_diagnostics(context)
        raise RuntimeError("SIS bağlantısı açılamadı; modal veya klavye etkinleştirme başarısız") from exc

    try:
        sis = wait_for_sis(context, timeout_sec=65)
        print("[SSO] Verbis sekmesi açıldı.")
        return sis
    except Exception:
        print_sis_diagnostics(context)
        raise


def fetch_all(page):
    person_id, semester_id = vizja_identifiers()
    start_weeks = week_starts()
    items = []
    for week in start_weeks:
        raw = page.evaluate(JS_FETCH, {"personId": person_id, "semesterId": semester_id, "weekStart": week})
        items.extend(raw)
    result = parse_items(items)
    if not result:
        raise RuntimeError("Ders listesi boş geldi; güvenlik için senkronizasyon yapılmayacak")
    print(f"[Vizja] {len(result)} ders, {WEEKS} hafta alındı.")
    return start_weeks, result


def format_google(ev):
    state = (ev.get("status") or "").upper()
    prefix = "[İPTAL] " if state == "ODWOŁANE" else "[TELAFİ] " if state == "ODRABIANE" else ""
    description = "\n".join([
        "Vizja / Verbis ders programı",
        "Öğretim görevlisi: " + ", ".join(ev["teachers"] or ["-"]),
        "Grup: " + ", ".join(ev["groups"] or ["-"]),
        "Oda: " + ", ".join(ev["rooms"] or ["-"]),
        "Durum: " + (ev.get("status") or "Normal"),
        "Verbis ID: " + ev["id"],
    ])
    body = {
        "summary": prefix + ev["title"],
        "description": description,
        "start": {"dateTime": ev["start"], "timeZone": "Europe/Warsaw"},
        "end": {"dateTime": ev["end"], "timeZone": "Europe/Warsaw"},
        "location": ", ".join(ev["rooms"]),
        "transparency": "transparent" if state == "ODWOŁANE" else "opaque",
        "visibility": "private",
        "reminders": {"useDefault": False, "overrides": []} if state == "ODWOŁANE" else {"useDefault": True},
    }
    normalized = json.dumps(body, ensure_ascii=False, sort_keys=True)
    body["extendedProperties"] = {"private": {
        "vizjaLessonId": ev["id"],
        "vizjaHash": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    }}
    return body


def google_event_key(ev):
    private = ev.get("extendedProperties", {}).get("private", {})
    if "vizjaLessonId" in private:
        return str(private["vizjaLessonId"])
    # Migration: existing v4.4 entries have a human-readable Verbis ID in description.
    match = re.search(r"(?:^|\n)Verbis ID:\s*(\d+)(?:\n|$)", ev.get("description", ""))
    return match.group(1) if match else None


def comparable_google(ev):
    def stamp(field):
        dt = ev.get(field, {}).get("dateTime")
        if not dt:
            return ""
        return datetime.fromisoformat(dt.replace("Z", "+00:00")).astimezone(WARSAW).isoformat(timespec="seconds")
    return (ev.get("summary", ""), stamp("start"), stamp("end"),
            ev.get("location", ""), ev.get("description", ""))


def week_of_millis(ms):
    d = datetime.fromtimestamp(ms / 1000, WARSAW)
    monday = (d - timedelta(days=d.weekday())).date()
    return int(datetime.combine(monday, datetime.min.time(), WARSAW).timestamp() * 1000)


def event_ms(ev):
    start = ev.get("start", {}).get("dateTime")
    if not start:
        return None
    return int(datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp() * 1000)


def plan_changes(data, remote_events, starts):
    """Pure reconciliation plan; only events tagged by this project can be removed."""
    old = {}
    for remote in remote_events:
        key = google_event_key(remote)
        if key and remote.get("status") != "cancelled":
            if key in old:
                raise RuntimeError(f"Google takviminde mükerrer Verbis kaydı var: {key}")
            old[key] = remote
    if remote_events and not old and len(remote_events) >= 5:
        raise RuntimeError("Takvimde mevcut etkinlikler var ancak Verbis kimliği bulunamadı; mükerrer riskine karşı durduruldu")
    lower = starts[0]
    upper = int((datetime.fromtimestamp(starts[-1] / 1000, WARSAW) + timedelta(weeks=1)).timestamp() * 1000)
    active_weeks = {week_of_millis(ms) for ev in old.values() if (ms := event_ms(ev)) is not None}
    old_in_window = {k: v for k, v in old.items() if (ms := event_ms(v)) is not None and lower <= ms < upper}
    if len(old_in_window) >= 10 and len(data) < len(old_in_window) * 0.4:
        raise RuntimeError("Verbis ders sayısı beklenmedik ölçüde azaldı, silme yapılmadı")
    writes = []
    deletions = []
    notices = []
    for key, item in sorted(data.items(), key=lambda kv: kv[1]["start_ms"]):
        desired = format_google(item)
        existing = old.get(key)
        if existing is None:
            writes.append(("create", key, None, desired))
            if week_of_millis(item["start_ms"]) in active_weeks:
                notices.append(f"➕ Yeni ders: {item['title']} — {item['start'][:16]}")
        else:
            same = comparable_google(existing) == comparable_google(desired)
            priv = existing.get("extendedProperties", {}).get("private", {})
            if not same or priv.get("vizjaHash") != desired["extendedProperties"]["private"]["vizjaHash"]:
                writes.append(("update", key, existing["id"], desired))
                if not same:
                    notices.append(f"✏️ Ders değişti: {item['title']} — {item['start'][:16]}")
    for key, existing in old_in_window.items():
        if key not in data:
            deletions.append((key, existing["id"]))
            notices.append(f"❌ Ders kaldırıldı: {existing.get('summary', 'Ders')} ")
    return writes, deletions, notices


def google_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    token_string = os.environ.get("GOOGLE_TOKEN_JSON", "")
    if not token_string:
        raise RuntimeError("GOOGLE_TOKEN_JSON secret eksik")
    try:
        token = json.loads(token_string)
        if not token.get("refresh_token"):
            raise ValueError("refresh_token mevcut değil")
        creds = Credentials.from_authorized_user_info(token, scopes=[GOOGLE_SCOPE])
        if not creds.valid:
            creds.refresh(Request())
    except Exception as exc:
        raise RuntimeError("Google OAuth yenilenemedi. Testing modundaki 7 günlük süre dolmuş olabilir") from exc
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def existing_google_events(service, calid):
    rows = []
    token = None
    while True:
        try:
            page = service.events().list(calendarId=calid, maxResults=2500,
                                         pageToken=token, showDeleted=False).execute()
        except Exception as exc:
            raise RuntimeError(f"Google events.list başarısız: {safe_google_error(exc)}") from exc
        rows.extend(page.get("items", []))
        token = page.get("nextPageToken")
        if not token:
            return rows


def safe_google_error(exc):
    """Only print an HTTP status and vetted Google reason; never a URL or token."""
    status = getattr(getattr(exc, "resp", None), "status", "?")
    reason = "unknown"
    try:
        raw = getattr(exc, "content", b"")
        parsed = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        detail = parsed.get("error", {})
        errors = detail.get("errors") or []
        value = (errors[0].get("reason") if errors else None) or detail.get("status") or "unknown"
        # Limit to a fixed-safe character set to keep private details out of public Actions logs.
        reason = re.sub(r"[^a-zA-Z0-9_.-]", "", str(value))[:60] or "unknown"
    except (TypeError, ValueError, AttributeError, KeyError):
        pass
    return f"Google Calendar API HTTP {status}; reason={reason}"


def send_telegram(lines):
    bot = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot or not chat:
        print("[Bildirim] Telegram secrets henüz eklenmedi; değişiklikler yalnızca loglandı.")
        return
    data = json.dumps({"chat_id": chat, "text": "Vizja ders programı güncellendi:\n" + "\n".join(lines[:25])}).encode("utf-8")
    req = urllib.request.Request(f"https://api.telegram.org/bot{bot}/sendMessage", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            if response.status != 200:
                raise RuntimeError("Bildirim gönderilemedi")
    except Exception as exc:
        raise RuntimeError("Telegram mesajı gönderilemedi; takvim eşitlemesi tamamlandı") from exc


def sync_to_google(data, starts):
    calid = os.environ.get("GOOGLE_CALENDAR_ID", "").strip()
    if not calid or calid == "primary":
        raise RuntimeError("GOOGLE_CALENDAR_ID eksik veya primary; sadece ayrı Vizja University takvimi desteklenir")
    service = google_service()
    print("[Google] OAuth okundu; hedef takvim erişimi kontrol ediliyor.", flush=True)
    try:
        info = service.calendars().get(calendarId=calid).execute()
    except Exception as exc:
        raise RuntimeError(f"Google calendars.get başarısız: {safe_google_error(exc)}") from exc
    if info.get("summary") != "Vizja University":
        raise RuntimeError("Takvim adı Vizja University değil; yanlış takvime yazma ihtimaline karşı durduruldu")
    print("[Google] Hedef takvim doğrulandı; mevcut etkinlikler okunuyor.", flush=True)
    existing = existing_google_events(service, calid)
    writes, deletes, notices = plan_changes(data, existing, starts)
    print(f"[Google] Ön kontrol: {len(writes)} ekleme/güncelleme; {len(deletes)} silme; {len(notices)} bildirim.")
    created = updated = removed = 0
    for op, key, eid, body in writes:
        try:
            if op == "create":
                service.events().insert(calendarId=calid, body=body).execute()
                created += 1
            else:
                service.events().patch(calendarId=calid, eventId=eid, body=body).execute()
                updated += 1
        except Exception as exc:
            raise RuntimeError(f"{safe_google_error(exc)} | işlem: {op} | ders kimliği loglarda gizlendi") from exc
    for key, eid in deletes:
        try:
            service.events().delete(calendarId=calid, eventId=eid).execute()
            removed += 1
        except Exception as exc:
            raise RuntimeError(f"{safe_google_error(exc)} | işlem: delete | ders kimliği loglarda gizlendi") from exc
    print(f"[Google] Eklendi: {created}, güncellendi: {updated}, silindi: {removed}, hata: 0")
    if notices:
        send_telegram(notices)
    return created, updated, removed


def run(browser_mode, username, password):
    from playwright.sync_api import sync_playwright
    with tempfile.TemporaryDirectory(prefix="vizja_cloud_chrome_") as profile:
        process = start_external_chrome(profile)
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}", timeout=15000)
                try:
                    context = browser.contexts[0]
                    page = auto_sign_in(context, username, password)
                    starts, events = fetch_all(page)
                    if browser_mode == "login-test":
                        print("[Test] Bulut tarzı sıfırdan giriş ve ders çekme başarılı. Google Calendar değiştirilmedi.")
                        return
                    sync_to_google(events, starts)
                finally:
                    browser.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()


def main():
    parser = argparse.ArgumentParser(description="Vizja -> Google Calendar stateless sync")
    parser.add_argument("--login-test", action="store_true", help="Yalnızca otomatik Extranet girişini test et")
    parser.add_argument("--sync", action="store_true", help="Dersleri Google Calendar'a eşitle")
    args = parser.parse_args()
    if args.login_test == args.sync:
        parser.error("--login-test veya --sync seç")
    username = os.environ.get("VIZJA_USERNAME")
    password = os.environ.get("VIZJA_PASSWORD")
    if (args.sync or os.environ.get("GITHUB_ACTIONS") == "true") and (not username or not password):
        parser.error("VIZJA_USERNAME ve VIZJA_PASSWORD secrets gerekli")
    if args.login_test and os.environ.get("GITHUB_ACTIONS") != "true":
        username = username or input("Extranet kullanıcı adı: ")
        password = password or getpass.getpass("Extranet şifresi (gizli): ")
    try:
        run("login-test" if args.login_test else "sync", username, password)
        return 0
    except Exception as exc:
        # Avoid printing exception chaining (requests or Playwright may embed SSO urls).
        print("[HATA]", str(exc).splitlines()[0][:230] if type(exc) is RuntimeError else type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
