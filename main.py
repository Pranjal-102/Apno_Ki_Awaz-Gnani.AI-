import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import gnani
import notifier

# ---- Timings in seconds. Small defaults make demos quick. Real life: 300 / 600 / 900. ----
NUDGE_AFTER = int(os.getenv("NUDGE_AFTER_SECONDS", "60"))        # no reply -> second reminder
ESCALATE_AFTER = int(os.getenv("ESCALATE_AFTER_SECONDS", "90"))  # still no reply -> alert family
SNOOZE_FOR = int(os.getenv("SNOOZE_SECONDS", "120"))             # "later" waits this long
OFFLINE_AFTER = 90                                               # nobody picked it up -> device offline

TZ = ZoneInfo("Asia/Kolkata")
FMT = "%Y-%m-%d %H:%M:%S"

# Words are chosen to be gender-neutral so they sound right in any family member's voice.
TAKEN_SAY = "बहुत अच्छा! मैंने नोट कर लिया है। अपना ध्यान रखिए।"
SNOOZE_SAY = "ठीक है। थोड़ी देर बाद फिर याद दिलाया जाएगा।"
UNCLEAR_SAY = "बात ठीक से समझ नहीं आई। कृपया बोलिए, हाँ ले ली, या, बाद में।"
EMERGENCY_SAY = "परिवार को अभी खबर की जा रही है। अगर ज़्यादा तकलीफ़ हो, तो एक सौ बारह पर फ़ोन कीजिए।"

TAKEN = ["ले ली", "ले लिया", "खा ली", "खा लिया", "कर ली", "कर लिया",
         "हाँ", "हां", "haan", "yes", "done", "taken"]
LATER = ["बाद में", "बाद", "थोड़ी देर", "नहीं", "later", "nahi"]
EMERGENCY = ["चक्कर", "दर्द", "सीने", "सांस", "साँस", "गिर", "बेहोश", "तबीयत खराब",
             "तबियत खराब", "बचाओ", "मदद", "घबराहट", "उल्टी",
             "chakkar", "dard", "saans", "bachao", "madad", "ambulance", "help"]

os.makedirs("audio", exist_ok=True)
app = FastAPI(title="Apno Ki Awaaz")


def now():
    return datetime.now(TZ).replace(tzinfo=None)


@contextmanager
def db():
    conn = sqlite3.connect("apno_v2.db", timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS voices(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, embedding TEXT,
            consent INTEGER, consent_at TEXT);
        CREATE TABLE IF NOT EXISTS schedules(
            id INTEGER PRIMARY KEY AUTOINCREMENT, elder_name TEXT, task TEXT, times TEXT,
            voice_id INTEGER, audio_file TEXT, nudge_file TEXT, active INTEGER DEFAULT 1,
            created_at TEXT);
        CREATE TABLE IF NOT EXISTS doses(
            id INTEGER PRIMARY KEY AUTOINCREMENT, schedule_id INTEGER, slot TEXT,
            elder_name TEXT, task TEXT, due_at TEXT, status TEXT, attempts INTEGER DEFAULT 0,
            announced_at TEXT, voice_id INTEGER, audio_file TEXT, nudge_file TEXT,
            created_at TEXT, UNIQUE(schedule_id, slot));
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, dose_id INTEGER, kind TEXT, detail TEXT, at TEXT);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        """)
        if c.execute("SELECT COUNT(*) FROM voices").fetchone()[0] == 0 \
                and os.path.exists("voice_embedding.json"):
            with open("voice_embedding.json", encoding="utf-8") as f:
                emb = f.read()
            c.execute("INSERT INTO voices(name, embedding, consent, consent_at) VALUES (?, ?, 1, ?)",
                      ("My voice", emb, now().strftime(FMT)))


init_db()


def log_event(c, dose_id, kind, detail=""):
    c.execute("INSERT INTO events(dose_id, kind, detail, at) VALUES (?, ?, ?, ?)",
              (dose_id, kind, detail, now().strftime(FMT)))


def alert_family(dose_id, kind, text):
    sent = notifier.send(text)
    note = text if sent else text + "\n[Not sent: Telegram is not set up]"
    with db() as c:
        log_event(c, dose_id, kind, note)


def alert_async(dose_id, kind, text):
    threading.Thread(target=alert_family, args=(dose_id, kind, text), daemon=True).start()


def get_embedding(c, voice_id):
    v = c.execute("SELECT embedding FROM voices WHERE id=?", (voice_id,)).fetchone()
    if not v:
        raise HTTPException(404, "Voice not found")
    return json.loads(v["embedding"])


def speak_cached(text, voice_id):
    """Generate speech once per (voice, text) and reuse it: faster and saves credits."""
    key = hashlib.md5(f"{voice_id}|{text}".encode("utf-8")).hexdigest()[:16]
    path = f"audio/c_{key}.wav"
    if not os.path.exists(path):
        with db() as c:
            emb = get_embedding(c, voice_id)
        wav = gnani.speak(text, emb)
        with open(path, "wb") as f:
            f.write(wav)
    return path


def parse_time(x):
    try:
        h, m = [int(p) for p in x.strip().split(":")[:2]]
        if 0 <= h < 24 and 0 <= m < 60:
            return f"{h:02d}:{m:02d}"
    except ValueError:
        pass
    raise HTTPException(400, f"Invalid time: {x}")


def detect_intent(text):
    t = text.lower()
    if any(k in t for k in EMERGENCY):   # safety first: errs on the side of alerting
        return "emergency"
    if any(k in t for k in LATER):
        return "snooze"
    if any(k in t for k in TAKEN):
        return "taken"
    return "unclear"


# ====================== background scheduler ======================
def tick():
    t = now()
    alerts = []
    with db() as c:
        # 1) create today's doses from the daily schedules
        for s in c.execute("SELECT * FROM schedules WHERE active=1").fetchall():
            for hhmm in s["times"].split(","):
                try:
                    h, m = [int(p) for p in hhmm.split(":")]
                except ValueError:
                    continue
                due = t.replace(hour=h, minute=m, second=0, microsecond=0)
                if due <= t <= due + timedelta(minutes=5):
                    slot = due.strftime(FMT)
                    cur = c.execute(
                        "INSERT OR IGNORE INTO doses(schedule_id, slot, elder_name, task, due_at, status,"
                        " attempts, voice_id, audio_file, nudge_file, created_at)"
                        " VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)",
                        (s["id"], slot, s["elder_name"], s["task"], slot, s["voice_id"],
                         s["audio_file"], s["nudge_file"], t.strftime(FMT)))
                    if cur.rowcount:
                        log_event(c, cur.lastrowid, "created", "scheduled")

        # 2) escalation rules
        rows = c.execute("SELECT * FROM doses WHERE status IN ('pending','announced','nudge')").fetchall()
        for d in rows:
            due = datetime.strptime(d["due_at"], FMT)
            created = datetime.strptime(d["created_at"], FMT)
            ref = max(due, created)
            offline_msg = (f"⚠️ Apno Ki Awaaz\nCould not deliver the '{d['task']}' reminder to "
                           f"{d['elder_name']}: the companion phone seems offline. "
                           f"Please check it or call them.")
            if d["status"] == "pending":
                if t - ref > timedelta(seconds=OFFLINE_AFTER):
                    c.execute("UPDATE doses SET status='missed' WHERE id=?", (d["id"],))
                    alerts.append((d["id"], "offline", offline_msg))
                continue
            ann = datetime.strptime(d["announced_at"], FMT) if d["announced_at"] else ref
            waited = (t - ann).total_seconds()
            if d["status"] == "announced":
                if d["attempts"] < 2 and waited >= NUDGE_AFTER:
                    c.execute("UPDATE doses SET status='nudge' WHERE id=?", (d["id"],))
                elif d["attempts"] >= 2 and waited >= ESCALATE_AFTER:
                    c.execute("UPDATE doses SET status='missed' WHERE id=?", (d["id"],))
                    alerts.append((d["id"], "missed",
                                   f"⚠️ Apno Ki Awaaz\n{d['elder_name']} has not confirmed "
                                   f"'{d['task']}' (due {due:%H:%M}). Reminded {d['attempts']} times. "
                                   f"Please call them."))
            elif d["status"] == "nudge" and waited >= NUDGE_AFTER + OFFLINE_AFTER:
                c.execute("UPDATE doses SET status='missed' WHERE id=?", (d["id"],))
                alerts.append((d["id"], "offline", offline_msg))

    for dose_id, kind, text in alerts:   # sent after the database work is done
        alert_family(dose_id, kind, text)


def scheduler_loop():
    while True:
        try:
            tick()
        except Exception as e:
            print("scheduler error:", e)
        time.sleep(10)


@app.on_event("startup")
def start_scheduler():
    threading.Thread(target=scheduler_loop, daemon=True).start()


# ====================== voices ======================
@app.get("/api/voices")
def list_voices():
    with db() as c:
        return [dict(r) for r in c.execute("SELECT id, name FROM voices ORDER BY id").fetchall()]


@app.post("/api/voices")
def add_voice(name: str = Form(...), consent: str = Form(...), audio: UploadFile = File(...)):
    if consent.lower() != "true":
        raise HTTPException(400, "Consent is required: only clone your own voice, or one you have permission to use.")
    try:
        emb = gnani.make_embedding(gnani.to_wav(audio.file.read()))
    except Exception as e:
        raise HTTPException(502, str(e))
    with db() as c:
        cur = c.execute("INSERT INTO voices(name, embedding, consent, consent_at) VALUES (?, ?, 1, ?)",
                        (name, json.dumps(emb), now().strftime(FMT)))
        return {"id": cur.lastrowid, "name": name}


@app.post("/api/stt")
def stt(audio: UploadFile = File(...)):
    try:
        return {"transcript": gnani.stt(gnani.to_wav(audio.file.read()))}
    except Exception as e:
        raise HTTPException(502, str(e))


# ====================== schedules ======================
class ScheduleIn(BaseModel):
    elder_name: str
    task: str
    times: list[str]
    voice_id: int


@app.post("/api/schedules")
def create_schedule(s: ScheduleIn):
    times = sorted(set(parse_time(x) for x in s.times))
    if not times:
        raise HTTPException(400, "Add at least one time")
    elder = s.elder_name.strip() or "दादी"
    task = s.task.strip()
    if not task:
        raise HTTPException(400, "Enter the medicine")
    first = f"{elder}, {task} का समय हो गया है। कृपया अभी ले लीजिए।"
    nudge = f"{elder}, {task} का समय निकल रहा है। कृपया अभी ले लीजिए, और बोलिए, हाँ ले ली।"
    try:
        a = speak_cached(first, s.voice_id)
        b = speak_cached(nudge, s.voice_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))
    with db() as c:
        cur = c.execute(
            "INSERT INTO schedules(elder_name, task, times, voice_id, audio_file, nudge_file, active, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (elder, task, ",".join(times), s.voice_id, a, b, now().strftime(FMT)))
        return {"id": cur.lastrowid, "times": times}


@app.get("/api/schedules")
def list_schedules():
    with db() as c:
        rows = c.execute("SELECT id, elder_name, task, times FROM schedules WHERE active=1 ORDER BY id").fetchall()
    return [{"id": r["id"], "elder_name": r["elder_name"], "task": r["task"],
             "times": r["times"].split(",")} for r in rows]


@app.delete("/api/schedules/{sid}")
def delete_schedule(sid: int):
    with db() as c:
        c.execute("UPDATE schedules SET active=0 WHERE id=?", (sid,))
    return {"ok": True}


@app.post("/api/schedules/{sid}/test")
def test_schedule(sid: int):
    t = now()
    with db() as c:
        s = c.execute("SELECT * FROM schedules WHERE id=? AND active=1", (sid,)).fetchone()
        if not s:
            raise HTTPException(404, "Schedule not found")
        slot = f"test-{int(time.time() * 1000)}"
        cur = c.execute(
            "INSERT INTO doses(schedule_id, slot, elder_name, task, due_at, status, attempts, voice_id,"
            " audio_file, nudge_file, created_at) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)",
            (sid, slot, s["elder_name"], s["task"], t.strftime(FMT), s["voice_id"],
             s["audio_file"], s["nudge_file"], t.strftime(FMT)))
        log_event(c, cur.lastrowid, "created", "test reminder")
    return {"ok": True}


# ====================== elder companion ======================
@app.post("/api/heartbeat")
def heartbeat():
    with db() as c:
        c.execute("INSERT INTO meta(key, value) VALUES('last_seen', ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now().strftime(FMT),))
    return {"ok": True}


@app.get("/api/due")
def due():
    t = now()
    out = []
    with db() as c:
        c.execute("INSERT INTO meta(key, value) VALUES('last_seen', ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (t.strftime(FMT),))
        row = c.execute("SELECT * FROM doses WHERE status IN ('pending','nudge') AND due_at<=? "
                        "ORDER BY due_at LIMIT 1", (t.strftime(FMT),)).fetchone()
        if row:
            nudging = row["status"] == "nudge"
            c.execute("UPDATE doses SET status='announced', attempts=attempts+1, announced_at=? WHERE id=?",
                      (t.strftime(FMT), row["id"]))
            log_event(c, row["id"], "nudged" if nudging else "announced")
            out.append({"id": row["id"], "elder_name": row["elder_name"], "task": row["task"],
                        "nudge": nudging,
                        "audio_url": "/" + (row["nudge_file"] if nudging else row["audio_file"])})
    return out


def get_dose(did):
    with db() as c:
        d = c.execute("SELECT * FROM doses WHERE id=?", (did,)).fetchone()
    if not d:
        raise HTTPException(404, "Reminder not found")
    return d


def resolve(dose, intent, heard):
    t = now()
    did = dose["id"]
    say = {"taken": TAKEN_SAY, "snooze": SNOOZE_SAY, "emergency": EMERGENCY_SAY}.get(intent, UNCLEAR_SAY)
    with db() as c:
        if intent == "taken":
            c.execute("UPDATE doses SET status='taken' WHERE id=?", (did,))
            log_event(c, did, "taken", heard)
        elif intent == "snooze":
            later = (t + timedelta(seconds=SNOOZE_FOR)).strftime(FMT)
            c.execute("UPDATE doses SET status='pending', due_at=?, attempts=0, announced_at=NULL WHERE id=?",
                      (later, did))
            log_event(c, did, "snoozed", heard)
        elif intent == "emergency":
            c.execute("UPDATE doses SET status='alerted' WHERE id=?", (did,))
        else:
            log_event(c, did, "unclear", heard)

    if intent == "emergency":
        alert_async(did, "emergency",
                    f"🚨 Apno Ki Awaaz\n{dose['elder_name']} said something that may mean trouble:\n"
                    f"“{heard}”\nPlease call them now. (Medicine reminder: {dose['task']})")
    elif intent == "taken" and os.getenv("NOTIFY_ON_TAKEN", "1") != "0":
        threading.Thread(target=notifier.send,
                         args=(f"✅ {dose['elder_name']} confirmed: {dose['task']} ({t:%H:%M})",),
                         daemon=True).start()

    audio_url = None
    try:
        audio_url = "/" + speak_cached(say, dose["voice_id"])
    except Exception as e:
        print("voice reply failed:", e)   # the state change above is already saved
    return {"transcript": heard, "intent": intent, "audio_url": audio_url}


@app.post("/api/doses/{did}/reply")
def reply(did: int, audio: UploadFile = File(...)):
    dose = get_dose(did)
    try:
        heard = gnani.stt(gnani.to_wav(audio.file.read()))
    except Exception as e:
        raise HTTPException(502, str(e))
    return resolve(dose, detect_intent(heard), heard)


class ActionIn(BaseModel):
    action: str


@app.post("/api/doses/{did}/action")
def action(did: int, a: ActionIn):
    dose = get_dose(did)
    intent = "snooze" if a.action == "snooze" else "taken"
    return resolve(dose, intent, "(tapped)")


@app.post("/api/sos")
def sos():
    with db() as c:
        sch = c.execute("SELECT elder_name FROM schedules ORDER BY id DESC LIMIT 1").fetchone()
        voice = c.execute("SELECT id FROM voices ORDER BY id LIMIT 1").fetchone()
    elder = sch["elder_name"] if sch else "Elder"
    alert_async(None, "sos", f"🆘 Apno Ki Awaaz\n{elder} pressed the help button. Please call them now.")
    audio_url = None
    if voice:
        try:
            audio_url = "/" + speak_cached(EMERGENCY_SAY, voice["id"])
        except Exception as e:
            print("sos voice failed:", e)
    return {"audio_url": audio_url}


# ====================== family dashboard ======================
@app.get("/api/stats")
def stats():
    t = now()
    day_keys = [(t - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(29, -1, -1)]
    days = {d: {"day": d, "taken": 0, "bad": 0, "open": 0} for d in day_keys}
    with db() as c:
        rows = c.execute("SELECT status, substr(due_at,1,10) AS day FROM doses WHERE substr(due_at,1,10)>=?",
                         (day_keys[0],)).fetchall()
        today_rows = c.execute("SELECT elder_name, task, due_at, status FROM doses "
                               "WHERE substr(due_at,1,10)=? ORDER BY due_at", (day_keys[-1],)).fetchall()
        seen = c.execute("SELECT value FROM meta WHERE key='last_seen'").fetchone()
        events = c.execute("SELECT e.kind, e.detail, e.at, d.elder_name, d.task FROM events e "
                           "LEFT JOIN doses d ON d.id=e.dose_id WHERE e.kind!='created' "
                           "ORDER BY e.id DESC LIMIT 15").fetchall()

    for r in rows:
        d = days.get(r["day"])
        if not d:
            continue
        if r["status"] == "taken":
            d["taken"] += 1
        elif r["status"] in ("missed", "alerted"):
            d["bad"] += 1
        else:
            d["open"] += 1

    last7 = [days[k] for k in day_keys[-7:]]
    taken = sum(d["taken"] for d in last7)
    bad = sum(d["bad"] for d in last7)
    adherence = round(100 * taken / (taken + bad)) if taken + bad else None

    streak = 0
    for i, k in enumerate(reversed(day_keys)):
        d = days[k]
        if d["bad"] > 0:
            break
        if d["taken"] == 0:
            if i == 0:
                continue   # today may not have finished yet
            break
        streak += 1

    seen_ago = None
    if seen:
        seen_ago = int((t - datetime.strptime(seen["value"], FMT)).total_seconds())

    return {
        "adherence": adherence, "taken": taken, "missed": bad, "streak": streak,
        "online": seen_ago is not None and seen_ago <= 20, "seen_ago": seen_ago,
        "telegram": notifier.configured(),
        "days": last7,
        "today": [dict(r) for r in today_rows],
        "events": [dict(r) for r in events],
    }


# Must stay at the bottom: serves the audio files and the web page
app.mount("/audio", StaticFiles(directory="audio"), name="audio")
app.mount("/", StaticFiles(directory="static", html=True), name="static")