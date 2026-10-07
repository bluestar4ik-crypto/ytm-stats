import os
import re
import io
import json
import time
import sqlite3
import urllib.request
from datetime import datetime, timezone, timedelta
from collections import Counter
from ytmusicapi import YTMusic
from PIL import Image, ImageDraw, ImageFont

DB_FILE = "stats_fm.db"
AUTH_FILE = "ytm_auth_temp.json"
BOT_STATE_FILE = "bot_state.json"
LOCAL_TZ = timezone(timedelta(hours=3))

LASTFM_API_KEY = "635fab358fac6c9b77311507574f7010"
CUSTOM_TAGS = {
    "XLOV": "k-pop, korean",
    "Stray Kids": "k-pop, korean",
    "ENHYPEN": "k-pop, korean",
    "OSTY": "ukrainian indie, pop",
    "Клавдія Петрівна": "ukrainian pop, indie",
    "Lely45": "ukrainian, indie rock",
    "Sorry Jesus": "pop punk, russian rock",
    "Три дня дождя": "alternative rock, russian rock"
}

def get_artist_tags(artist_name):
    if artist_name in CUSTOM_TAGS:
        return CUSTOM_TAGS[artist_name]
    url = f"http://ws.audioscrobbler.com/2.0/?method=artist.gettoptags&artist={urllib.parse.quote(artist_name)}&api_key={LASTFM_API_KEY}&format=json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "YTM.stats Enricher"})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
            tags = data.get("toptags", {}).get("tag", [])
            valid_tags = []
            for t in tags:
                t_name = t.get("name", "").lower()
                if t_name and t_name not in ["seen live", "favorite", "любимое", "under 2000 listeners"]:
                    valid_tags.append(t_name)
                if len(valid_tags) >= 3:
                    break
            return ", ".join(valid_tags)
    except Exception:
        return ""

def enrich_genres(cur, conn):
    cur.execute("PRAGMA table_info(streams)")
    cols = [c[1] for c in cur.fetchall()]
    if "genres" not in cols:
        cur.execute("ALTER TABLE streams ADD COLUMN genres TEXT DEFAULT ''")
    
    cur.execute("SELECT DISTINCT artist FROM streams WHERE genres IS NULL OR genres = ''")
    artists = [r[0] for r in cur.fetchall() if r[0] and r[0] != "Unknown"]
    
    if artists:
        print(f"[*] Starting genre enrichment for {len(artists)} artists...")
        updated = 0
        for artist in artists:
            tags = get_artist_tags(artist)
            if tags:
                cur.execute("UPDATE streams SET genres = ? WHERE artist = ?", (tags, artist))
                updated += 1
            time.sleep(0.1) # small delay for API
        conn.commit()
        print(f"[OK] Added genres for {updated} artists.")

def parse_duration_to_sec(dur_str: str) -> int:
    if not dur_str:
        return 195
    parts = [int(p) for p in dur_str.split(":") if p.isdigit()]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    elif len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return 195

def sync_ytmusic():
    auth_json = os.environ.get("YTM_AUTH_JSON", "").strip()
    if not auth_json:
        print("[!] YTM_AUTH_JSON is empty.")
        return None, 0

    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        f.write(auth_json)

    added_count = 0
    now_playing = None
    try:
        yt = YTMusic(AUTH_FILE)
        history = yt.get_history()
        now_dt = datetime.now(LOCAL_TZ)
        now_ts = int(now_dt.timestamp())

        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        
        # Запускаем обогатитель жанров перед синхронизацией
        enrich_genres(cur, conn)

        if not history:
            return None, 0

        top_item = history[0]
        top_vid = top_item.get("videoId", "")
        top_title = top_item.get("title", "Unknown")
        top_artists = top_item.get("artists", [])
        top_artist = top_artists[0]["name"] if top_artists else "Unknown"
        top_artist = re.sub(r"\s*-\s*Topic$", "", top_artist).strip()
        if "XLOV" in top_title.upper() or "엑스러브" in top_title:
            top_artist = "XLOV"

        now_playing = {
            "video_id": top_vid,
            "title": top_title,
            "artist": top_artist,
            "last_check": now_dt.strftime("%d.%m %H:%M")
        }

        cur.execute("SELECT video_id, timestamp FROM streams WHERE source != 'MV (YouTube)' ORDER BY timestamp DESC LIMIT 1")
        last_row = cur.fetchone()
        last_db_vid = last_row[0] if last_row else None
        last_db_ts = last_row[1] if last_row else 0

        today_items = [item for item in history[:100] if item.get("played", "").lower() in ("today", "сегодня", "сьогодні", "")]
        if not today_items:
            today_items = history[:15]

        new_slice = []
        found_anchor = False
        for idx, item in enumerate(today_items):
            if item.get("videoId") == last_db_vid:
                found_anchor = True
                new_slice = [] if idx == 0 else today_items[:idx]
                break

        if not found_anchor:
            new_slice = today_items[:40]

        if new_slice:
            ordered_new = list(reversed(new_slice))
            total_dur = sum((it.get("duration_seconds") or parse_duration_to_sec(it.get("duration", ""))) for it in ordered_new[:-1])
            cursor_ts = max(last_db_ts + 35, now_ts - total_dur)

            for item in ordered_new:
                vid = item.get("videoId")
                if not vid: continue
                title = item.get("title", "Unknown")
                artists = item.get("artists", [])
                artist = artists[0]["name"] if artists else "Unknown"
                artist = re.sub(r"\s*-\s*Topic$", "", artist).strip()
                if "XLOV" in title.upper() or "엑스러브" in title: artist = "XLOV"
                dur_sec = item.get("duration_seconds") or parse_duration_to_sec(item.get("duration", ""))
                
                # Получаем теги для новой песни на лету
                tags = get_artist_tags(artist)

                while cursor_ts <= last_db_ts: cursor_ts += 2
                dt_iso = datetime.fromtimestamp(cursor_ts, tz=LOCAL_TZ).isoformat()
                
                cur.execute("""
                    INSERT OR IGNORE INTO streams (video_id, title, artist, source, played_at, timestamp, duration_sec, genres)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (vid, title, artist, "Live Radar", dt_iso, cursor_ts, dur_sec, tags))
                
                if cur.rowcount > 0:
                    added_count += 1
                    last_db_ts = cursor_ts
                cursor_ts += min(dur_sec, 240)

        conn.commit()
        conn.close()
    finally:
        if os.path.exists(AUTH_FILE):
            os.remove(AUTH_FILE)
            
    return now_playing, added_count

def export_web_data(now_playing):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    # Теперь мы вытаскиваем и колонку genres
    cur.execute("SELECT video_id, title, artist, source, timestamp, duration_sec, genres FROM streams ORDER BY timestamp ASC")
    rows = cur.fetchall()
    conn.close()

    compact_rows = [[r[0], r[1], r[2], 1 if r[3] == "MV (YouTube)" else (2 if r[3] == "Live Radar" else 0), r[4], r[5], r[6] or ""] for r in rows]
    payload = {
        "updated_at": datetime.now(LOCAL_TZ).strftime("%d.%m.%Y %H:%M"),
        "now_playing": now_playing,
        "streams": compact_rows
    }
    with open("data.js", "w", encoding="utf-8") as f:
        f.write("window.YTM_CLOUD_DATA = " + json.dumps(payload, ensure_ascii=False) + ";\n")
    print(f"[OK] data.js updated ({len(compact_rows)} streams).")

def process_telegram_bot(now_playing, added_count):
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    if not token: return
    state = {"offset": 0, "chat_id": None, "last_weekly": ""}
    if os.path.exists(BOT_STATE_FILE):
        try:
            with open(BOT_STATE_FILE, "r", encoding="utf-8") as f: state.update(json.load(f))
        except Exception: pass

    def tg_call(method, data_dict):
        url = f"https://api.telegram.org/bot{token}/{method}"
        req = urllib.request.Request(url, data=json.dumps(data_dict).encode("utf-8"), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    kb = {"keyboard": [[{"text": "🟢 Сейчас в эфире"}, {"text": "📊 Топ недели"}], [{"text": "👑 Топ за всё время"}]], "resize_keyboard": True}

    try:
        res = tg_call("getUpdates", {"offset": state["offset"], "timeout": 2})
        for upd in res.get("result", []):
            state["offset"] = upd["update_id"] + 1
            msg = upd.get("message", {})
            cid = msg.get("chat", {}).get("id")
            text = msg.get("text", "")
            if not cid: continue
            state["chat_id"] = cid
            
            if text in ("/start", "🟢 Сейчас в эфире"):
                np_txt = f"🎵 <b>{now_playing['artist']} — {now_playing['title']}</b>" if now_playing else "В эфире тишина"
                tg_call("sendMessage", {"chat_id": cid, "text": f"🛰 <b>Облачный радар YTM.stats активен 24/7!</b>\nПоследний трек: {np_txt}\nДобавлено новых за сеанс: +{added_count}", "parse_mode": "HTML", "reply_markup": kb})
            elif text in ("📊 Топ недели", "👑 Топ за всё время"):
                days = 7 if "недели" in text else 0
                conn = sqlite3.connect(DB_FILE)
                cur = conn.cursor()
                cur.execute("SELECT MAX(timestamp) FROM streams")
                max_ts = cur.fetchone()[0] or 0
                cutoff = (max_ts - days * 86400) if days > 0 else 0
                cur.execute("SELECT artist, title, duration_sec FROM streams WHERE timestamp >= ?", (cutoff,))
                r_list = cur.fetchall()
                conn.close()

                ac = Counter(r[0] for r in r_list)
                tc = Counter(f"{r[0]} — {r[1]}" for r in r_list)
                mins = sum(r[2] for r in r_list) // 60
                a_str = "\n".join(f"  {i}. <b>{k}</b> ({v})" for i, (k, v) in enumerate(ac.most_common(7), 1))
                t_str = "\n".join(f"  {i}. <b>{k}</b> ({v}x)" for i, (k, v) in enumerate(tc.most_common(7), 1))
                title_lbl = "за 7 дней" if days == 7 else "за всё время"
                tg_call("sendMessage", {"chat_id": cid, "text": f"🎧 <b>Статистика {title_lbl}:</b>\nСтримов: <b>{len(r_list)}</b> (~{mins} мин.)\n\n👑 <b>Артисты:</b>\n{a_str}\n\n🔥 <b>Треки:</b>\n{t_str}", "parse_mode": "HTML", "reply_markup": kb})

        with open(BOT_STATE_FILE, "w", encoding="utf-8") as f: json.dump(state, f)
    except Exception as e: print("TG Error:", e)

if __name__ == "__main__":
    np_info, added = sync_ytmusic()
    export_web_data(np_info)
    process_telegram_bot(np_info, added)
