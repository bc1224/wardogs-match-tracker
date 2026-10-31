"""SQLite storage for sessions, matches, API snapshots and screen templates."""
import json
import os
import re
import sqlite3
import time

from .ocr import CYRILLIC, NOT_TEAM, ROLES, cash_summary, details, gun_name, player_name, repair_lines, reward_label

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL);
CREATE TABLE IF NOT EXISTS matches (
  id INTEGER PRIMARY KEY, session_id INTEGER, started_at REAL, ended_at REAL NOT NULL,
  xp INTEGER, cash_earned INTEGER, cash_spent INTEGER,
  source TEXT, screenshot TEXT, ocr_json TEXT, role_levels TEXT,
  reviewed INTEGER NOT NULL DEFAULT 0, notes TEXT);
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY, taken_at REAL NOT NULL, synced_at TEXT UNIQUE, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS templates (
  id INTEGER PRIMARY KEY, created_at REAL NOT NULL, sig BLOB NOT NULL, match_id INTEGER);
CREATE TABLE IF NOT EXISTS unlocks (
  game_id TEXT PRIMARY KEY, name TEXT, grp TEXT, url TEXT, image TEXT, level INTEGER,
  first_seen REAL NOT NULL, synced_at TEXT, baseline INTEGER NOT NULL DEFAULT 0,
  level_seen REAL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT, updated REAL);
"""
HINT_RE = re.compile(r"xp|\$", re.I)

EDITABLE = ("xp", "cash_earned", "cash_spent", "reviewed", "notes")
RESULTS = ("victory", "defeat", "draw")  # what you can set a match's result to by hand
SETTINGS = ("daily_xp_goal", "weekly_xp_goal")


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(templates)")}
        if "match_id" not in cols:  # databases made before templates were linked to matches
            self.db.execute("ALTER TABLE templates ADD COLUMN match_id INTEGER")
        if "details" not in {r[1] for r in self.db.execute("PRAGMA table_info(matches)")}:
            self.db.execute("ALTER TABLE matches ADD COLUMN details TEXT")
        # older versions logged "Revived Team Mate" as a team called "mate"
        self.db.execute(
            f"""UPDATE matches SET details = json_set(details, '$.hud.team', json('null'))
                WHERE json_valid(details) AND lower(json_extract(details, '$.hud.team'))
                IN ({",".join("?" * len(NOT_TEAM))})""", tuple(NOT_TEAM))
        self._fix_names()
        self._fix_role_xp()
        self._fix_cyrillic()

    def _fix_cyrillic(self):
        """Once: results screens read by a recognizer for a Cyrillic language (before
        2026-10-30, issue #28) are repaired and read again. Only what a match is missing
        is filled in, nothing set already (or by hand) changes, and the text as first
        read is kept in details["ocr_before_repair"]. Matches with no Cyrillic in their
        text, which is everyone reading with English, aren't touched."""
        if self.get_meta("cyrillic_fixed"):
            return
        rows = self.db.execute("SELECT id, cash_earned, cash_spent, ocr_json, details FROM matches "
                               "WHERE ocr_json IS NOT NULL").fetchall()
        for mid, earned, spent, ocr, det in rows:
            lines = json.loads(ocr)
            if not any(re.search(CYRILLIC, l.get("text") or "") for l in lines if isinstance(l, dict)):
                continue
            fixed = repair_lines(lines)
            if [l.get("text") for l in fixed] == [l.get("text") for l in lines if isinstance(l, dict)]:
                continue
            det = json.loads(det) if det else {}
            det.setdefault("ocr_before_repair", lines)
            info = details(fixed)
            summ = cash_summary(fixed)
            if summ:
                info["cash_summary"] = summ
            for k, v in info.items():
                if v not in (None, {}, []) and det.get(k) in (None, {}, []):
                    det[k] = v
            if earned is None and spent is None and summ:
                earned, spent = summ.get("gained"), summ.get("spent")
            self.db.execute("UPDATE matches SET cash_earned=?, cash_spent=?, ocr_json=?, details=? WHERE id=?",
                            (earned, spent, json.dumps(fixed), json.dumps(det), mid))
        self.set_meta("cyrillic_fixed", 1)

    def _fix_role_xp(self):
        """Once: versions before 2026-10-29 read a Progression card's next-level target
        as XP after the round, so their per-role gains (and a round XP added up from
        them) were wrong. Forget those gains; the XP figure seen stays as "seen"."""
        if self.get_meta("role_xp_fixed"):
            return
        rows = self.db.execute("SELECT id, xp, details FROM matches WHERE details LIKE '%role_xp%'").fetchall()
        for mid, xp, det in rows:
            det = json.loads(det)
            rx = det.get("role_xp") or {}
            old = {r: v for r, v in rx.items() if isinstance(v, dict) and "next" not in v}
            if not old:
                continue
            if xp is not None and xp == sum(v.get("gain") or 0 for v in old.values()):
                xp = None
            det["role_xp"] = {r: {"seen": v.get("before"), "gain": None} if r in old else v for r, v in rx.items()}
            self.db.execute("UPDATE matches SET xp=?, details=? WHERE id=?", (xp, json.dumps(det), mid))
        self.set_meta("role_xp_fixed", 1)

    def _fix_names(self):
        """Names misread by older versions, once: guns ('MPS', 'MSOO') become the gun and
        equipment (range finders) stops counting as one; rewards ('Targetspotted', a
        teammate's name) merge into the real reward or Other; kill feed names lose the
        kill distance ('1220m) Hydra')."""
        if self.get_meta("names_fixed"):
            return
        known, gear = self.unlock_names("weapons"), self.gear_names()

        def fix(v):
            if isinstance(v, list):
                return [fix(x) for x in v]
            if not isinstance(v, dict):
                return v
            out = {k: gun_name(x, known, gear) if k == "gun" and isinstance(x, str) else fix(x) for k, x in v.items()}
            if isinstance(v.get("guns"), dict):
                out["guns"] = {}
                for name, n in v["guns"].items():
                    g = gun_name(name, known, gear)
                    if g:
                        out["guns"][g] = out["guns"].get(g, 0) + n
            if isinstance(v.get("rewards"), dict):
                out["rewards"] = {}
                for name, r in v["rewards"].items():
                    each = (r.get("cash") or 0) / max(1, r.get("n") or 1) if not r.get("xp") else None
                    t = out["rewards"].setdefault(reward_label(name, each), {"n": 0, "cash": 0, "xp": 0})
                    for k in t:
                        t[k] += r.get(k) or 0
            for key in ("sources", "feed"):
                if isinstance(v.get(key), list):
                    out[key] = [dict(e, label=reward_label(e["label"], None if e.get("xp") else e.get("cash"))) if isinstance(e, dict) and isinstance(e.get("label"), str) else e
                                for e in out[key]]
            for key in ("kill_log", "kills", "deaths"):
                if isinstance(v.get(key), list):
                    out[key] = [dict(e, other=player_name(e["other"])) if isinstance(e, dict) and isinstance(e.get("other"), str) else e
                                for e in out[key]]
            return out
        for mid, raw in self.db.execute("SELECT id, details FROM matches WHERE json_valid(details)").fetchall():
            det = json.loads(raw)
            new = fix(det)
            if new != det:
                self.db.execute("UPDATE matches SET details=? WHERE id=?", (json.dumps(new), mid))
        self.set_meta("names_fixed", True)

    def version(self):
        """Changes whenever another connection (the tracker) writes."""
        return self.db.execute("PRAGMA data_version").fetchone()[0]

    # sessions
    def start_session(self, at=None):
        return self.db.execute("INSERT INTO sessions(started_at) VALUES (?)",
                               (at or time.time(),)).lastrowid

    def open_session(self):
        """The session still open after a tracker restart mid-game, if any."""
        row = self.db.execute("SELECT id FROM sessions WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def end_session(self, sid, at=None):
        self.db.execute("UPDATE sessions SET ended_at=? WHERE id=?", (at or time.time(), sid))

    def close_open_sessions(self):
        """Sessions left open by a crash end at their last match (or start)."""
        self.db.execute("""UPDATE sessions SET ended_at = COALESCE(
            (SELECT MAX(ended_at) FROM matches m WHERE m.session_id = sessions.id), started_at)
            WHERE ended_at IS NULL""")

    # matches
    def add_match(self, session_id, started_at, ended_at, source, screenshot=None,
                  ocr=None, parsed=None, role_levels=None, details=None):
        parsed = parsed or {}
        return self.db.execute(
            """INSERT INTO matches(session_id, started_at, ended_at, xp, cash_earned, cash_spent,
               source, screenshot, ocr_json, role_levels, details)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (session_id, started_at, ended_at, parsed.get("xp"), parsed.get("cash_earned"),
             parsed.get("cash_spent"), source, screenshot,
             json.dumps(ocr) if ocr is not None else None,
             json.dumps(role_levels) if role_levels else None,
             json.dumps(details) if details else None)).lastrowid

    def update_match(self, mid, fields):
        # the result and the role you played, when the screen didn't say: set by hand
        if "result" in fields and (fields["result"] is None or fields["result"] in RESULTS):
            self.db.execute("UPDATE matches SET details = json_set(COALESCE(details, '{}'), '$.result', ?) WHERE id=?",
                            (fields["result"], mid))
        if "role" in fields and (fields["role"] is None or fields["role"] in ROLES):
            self.db.execute("UPDATE matches SET details = json_set(COALESCE(details, '{}'), '$.role', ?) WHERE id=?",
                            (fields["role"], mid))
        cols = {k: v for k, v in fields.items() if k in EDITABLE}
        if not cols:
            return
        sets = ", ".join(f"{k}=?" for k in cols)
        self.db.execute(f"UPDATE matches SET {sets} WHERE id=?", (*cols.values(), mid))

    def match_pictures(self, mid):
        """The results-screen pictures still kept for a match, the first screen first."""
        row = self.db.execute("SELECT screenshot, details FROM matches WHERE id=?", (mid,)).fetchone()
        if not row:
            return []
        det = json.loads(row[1]) if row[1] else {}
        return [p for p in [row[0], *det.get("end_shots", [])] if p and os.path.isfile(p)]

    def reread_match(self, mid, reads):
        """Apply a fresh read of a match's pictures, reads = [(path, lines)] with the
        first results screen first. What the screen says (winner, score, map, stats,
        money summary) is replaced by the new read; the result is only filled in when
        missing, since it may have been set by hand, and so is cash (the round's own
        figure may include a bonus the money screen lists apart, and "numbers checked"
        ones are yours). XP isn't touched: it comes from cards counting up, which one
        picture can't show. Returns the names of what changed."""
        row = self.db.execute("SELECT screenshot, cash_earned, cash_spent, reviewed, ocr_json, details "
                              "FROM matches WHERE id=?", (mid,)).fetchone()
        if not row or not reads:
            return []
        shot, earned, spent, reviewed, ocr, det = row
        det = json.loads(det) if det else {}
        reads = [(p, repair_lines(lines)) for p, lines in reads]  # for a PC with no English reader
        new = {}
        summ = {}
        for _, lines in reads:
            for k, v in details(lines).items():
                if v not in (None, {}, []) and new.get(k) in (None, {}, []):
                    new[k] = v
            for k, v in cash_summary(lines).items():
                summ.setdefault(k, v)
        if summ:
            new["cash_summary"] = summ
        changed = []
        for k, v in new.items():
            if k == "result" and det.get("result"):
                continue
            if det.get(k) != v:
                det[k] = v
                changed.append(k)
        if not reviewed and earned is None and spent is None and summ.get("gained") is not None:
            earned, spent = summ["gained"], summ.get("spent")
            changed.append("cash")
        if reads[0][0] == shot:
            text = [l for l in reads[0][1] if isinstance(l, dict)]
            if [l.get("text") for l in text] != [l.get("text") for l in json.loads(ocr or "[]") if isinstance(l, dict)]:
                det.setdefault("ocr_before_reread", json.loads(ocr or "[]"))
                ocr = json.dumps(text)
                changed.append("text")
        if changed:
            self.db.execute("UPDATE matches SET cash_earned=?, cash_spent=?, ocr_json=?, details=? WHERE id=?",
                            (earned, spent, ocr, json.dumps(det), mid))
        return changed

    def set_route(self, mid, route):
        """Keep the replay route the dashboard worked out, replacing an older one."""
        self.db.execute("UPDATE matches SET details = json_set(COALESCE(details, '{}'), '$.track_route', json(?)) WHERE id=?",
                        (json.dumps(route), mid))

    def add_end_shot(self, mid, path):
        """Keep a picture of another results tab (cash, XP) with the match."""
        row = self.db.execute("SELECT details FROM matches WHERE id=?", (mid,)).fetchone()
        if not row:
            return
        det = json.loads(row[0]) if row[0] else {}
        det.setdefault("end_shots", []).append(path)
        self.db.execute("UPDATE matches SET details=? WHERE id=?", (json.dumps(det), mid))

    def prune_shots(self, shots_dir, keep, grace=3600):
        """Delete the results-screen pictures in shots_dir except the newest `keep` matches',
        and forget them on those matches. Pictures anywhere else (Steam's F12 ones) stay. A
        picture no match lists is deleted once it's `grace` seconds old (its match may not be
        saved yet)."""
        root = os.path.normcase(os.path.abspath(shots_dir))
        ours = lambda p: bool(p) and os.path.normcase(os.path.dirname(os.path.abspath(p))) == root
        norm = lambda p: os.path.normcase(os.path.abspath(p))
        kept, dropped = set(), set()
        rows = self.db.execute("SELECT id, screenshot, details FROM matches ORDER BY ended_at DESC").fetchall()
        for i, (mid, shot, det) in enumerate(rows):
            det = json.loads(det) if det else {}
            ends = det.get("end_shots", [])
            if i < keep:
                kept.update(norm(p) for p in [shot, *ends] if ours(p))
                continue
            dropped.update(norm(p) for p in [shot, *ends] if ours(p))
            if ours(shot):
                self.db.execute("UPDATE matches SET screenshot=NULL WHERE id=?", (mid,))
            if any(ours(p) for p in ends):
                det["end_shots"] = [p for p in ends if not ours(p)]
                self.db.execute("UPDATE matches SET details=? WHERE id=?", (json.dumps(det), mid))
        for name in os.listdir(shots_dir) if os.path.isdir(shots_dir) else []:
            p = os.path.join(shots_dir, name)
            try:
                n = norm(p)
                if n in dropped or (n not in kept and time.time() - os.path.getmtime(p) > grace):
                    os.remove(p)
            except OSError:
                pass

    def fill_match(self, mid, parsed, info):
        """Fill what a match is still missing (XP, cash, detail keys) from another
        results screen of the same break; nothing already there is overwritten."""
        row = self.db.execute("SELECT xp, cash_earned, cash_spent, details FROM matches WHERE id=?",
                              (mid,)).fetchone()
        if not row:
            return
        xp, earned, spent, det = row
        det = json.loads(det) if det else {}
        roles_xp = xp is None or (det.get("xp_roles") and info.get("xp_roles"))
        for k, v in info.items():
            if v not in (None, {}, []) and det.get(k) in (None, {}, []):
                det[k] = v
        if info.get("role_xp"):  # the Progression tab fills in card by card
            det["role_xp"] = info["role_xp"]
        if roles_xp and parsed.get("xp") is not None:
            xp = parsed["xp"]
        if earned is None and spent is None:
            earned, spent = parsed.get("cash_earned"), parsed.get("cash_spent")
        elif info.get("cash_summary") and parsed.get("cash_earned") is not None and not det.get("summary_used"):
            # the Score tab's own spent/earned beat an estimate from the top bar
            earned, spent, det["summary_used"] = parsed["cash_earned"], parsed.get("cash_spent"), True
        self.db.execute("UPDATE matches SET xp=?, cash_earned=?, cash_spent=?, details=? WHERE id=?",
                        (xp, earned, spent, json.dumps(det), mid))

    def role_xp_known(self):
        """{role: XP} as the newest Progression tab left each role. Matches from before
        "next" was kept read the next-level target as XP, so they don't count."""
        out = {}
        for (det,) in self.db.execute("SELECT details FROM matches WHERE details LIKE '%\"next\"%' ORDER BY ended_at"):
            for r, v in (json.loads(det).get("role_xp") or {}).items():
                if isinstance(v, dict) and "next" in v and v.get("after") is not None:
                    out[r] = v["after"]
        return out

    def match_full(self, mid):
        """One match with everything kept for it: timeline, money changes, screen text."""
        row = self.db.execute("SELECT details, ocr_json FROM matches WHERE id=?", (mid,)).fetchone()
        if not row:
            return None
        det = json.loads(row[0]) if row[0] else {}
        ocr = json.loads(row[1]) if row[1] else []
        return {"details": det, "lines": [l.get("text", "") if isinstance(l, dict) else str(l) for l in ocr]}

    def match_text(self, mid):
        """Every line read from a match's results screen, for its detail view."""
        row = self.db.execute("SELECT ocr_json FROM matches WHERE id=?", (mid,)).fetchone()
        return [l.get("text", "") if isinstance(l, dict) else str(l)
                for l in json.loads(row[0])] if row and row[0] else []

    def last_match(self, session_id):
        row = self.db.execute("SELECT id FROM matches WHERE session_id=? ORDER BY ended_at DESC LIMIT 1",
                              (session_id,)).fetchone()
        return row[0] if row else None

    def delete_match(self, mid, forget_screen=False):
        """forget_screen also drops the end-screen template learned from this match,
        for when something that wasn't an end screen got logged."""
        self.db.execute("DELETE FROM matches WHERE id=?", (mid,))
        if forget_screen:
            self.db.execute("DELETE FROM templates WHERE match_id=?", (mid,))

    def last_match_end(self, session_id):
        row = self.db.execute("SELECT MAX(ended_at) FROM matches WHERE session_id=?",
                              (session_id,)).fetchone()
        if row[0] is None:  # no match yet: the first one began with the session
            row = self.db.execute("SELECT started_at FROM sessions WHERE id=?", (session_id,)).fetchone()
        return row[0] if row else None

    # snapshots
    def add_snapshot(self, stats, at=None):
        """Store an API stats snapshot; returns False if this sync was already stored."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO snapshots(taken_at, synced_at, data) VALUES (?,?,?)",
            (at or time.time(), stats.get("syncedAt"), json.dumps(stats)))
        return cur.rowcount == 1

    def has_snapshot(self, synced_at):
        return self.db.execute("SELECT 1 FROM snapshots WHERE synced_at=?",
                               (synced_at,)).fetchone() is not None

    # unlocks
    def unlock_count(self):
        return self.db.execute("SELECT COUNT(*) FROM unlocks").fetchone()[0]

    def sync_unlocks(self, unl, synced_at, at=None):
        """Record unlocks from an API pull. The very first pull is the baseline
        (things you had before tracking); after that, new items and level changes
        are stamped with when they were first seen. Returns their names."""
        at = at or time.time()
        baseline = self.unlock_count() == 0
        have = {r[0]: r[1] for r in self.db.execute("SELECT game_id, level FROM unlocks")}
        changes = []
        for u in unl["items"]:
            if u["game_id"] not in have:
                self.db.execute(
                    """INSERT INTO unlocks(game_id, name, grp, url, image, level, first_seen,
                       synced_at, baseline) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (u["game_id"], u["name"], u["grp"], u["url"], u["image"], u["level"],
                     at, synced_at, int(baseline)))
                if not baseline:
                    changes.append(u["name"])
            else:
                self.db.execute("UPDATE unlocks SET name=?, grp=?, url=?, image=? WHERE game_id=?",
                                (u["name"], u["grp"], u["url"], u["image"], u["game_id"]))
                if u["level"] is not None and u["level"] != have[u["game_id"]]:
                    self.db.execute("UPDATE unlocks SET level=?, level_seen=? WHERE game_id=?",
                                    (u["level"], at, u["game_id"]))
                    changes.append(f"{u['name']} (level {u['level']})")
        return changes

    def unlock_names(self, grp=None):
        if grp:
            return [r[0] for r in self.db.execute("SELECT name FROM unlocks WHERE grp=?", (grp,))]
        return [r[0] for r in self.db.execute("SELECT name FROM unlocks")]

    def gear_names(self):
        """Owned items you can hold that aren't guns (range finders, medical bags)."""
        return [r[0] for r in self.db.execute("SELECT name FROM unlocks WHERE grp='gear'")]

    # small JSON values: settings, server status, leaderboard neighbours
    def get_meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta(key, value, updated) VALUES (?,?,?)",
                        (key, json.dumps(value), time.time()))

    def switches(self):
        """The More menu's switches, each covering one kind of outside call:
        stats       ask wardogstracker.gg for your career totals, unlocks, rank, item prices
                    and server status. Off on a new install (it sends your Steam ID there);
                    on for anyone who already has stats from it
        updates     ask GitHub whether a newer version is out (sends nothing about you)
        auto_update install those new versions on its own, mid-round too (starts off)
        maps        download the full map pictures from wardogstracker.gg for the replay
                    map, once each (sends nothing about you)
        light       space the reads during a match out when they'd slow a slower PC down
                    (starts off: the full pace misses the fewest kills and rewards)
        The old single Online switch, once off, keeps stats, updates and maps off."""
        saved = self.get_meta("switches") or {}
        off = bool(self.get_meta("offline"))
        had_stats = self.db.execute("SELECT 1 FROM snapshots LIMIT 1").fetchone() is not None
        return {"stats": bool(saved.get("stats", had_stats and not off)),
                "updates": bool(saved.get("updates", not off)),
                "auto_update": bool(saved.get("auto_update", self.get_meta("auto_update"))),
                "maps": bool(saved.get("maps", not off)),
                "light": bool(saved.get("light", False))}

    def set_switch(self, name, on):
        if name not in self.switches():
            raise ValueError(name)
        saved = self.get_meta("switches") or {}  # only this one: the others keep their defaults
        saved[name] = bool(on)
        self.set_meta("switches", saved)

    def stats_on(self):
        return self.switches()["stats"]

    def maps_on(self):
        return self.switches()["maps"]

    def updates_on(self):
        return self.switches()["updates"]

    def auto_update(self):
        return self.switches()["auto_update"]

    def set_auto_update(self, on):
        self.set_switch("auto_update", on)

    def save_settings(self, fields):
        cur = self.get_meta("settings") or {}
        for k in SETTINGS:
            if k in fields:
                try:
                    v = int(float(fields[k]))
                except (TypeError, ValueError, OverflowError):  # "inf" too
                    v = None
                cur[k] = v if v and v > 0 else None
        self.set_meta("settings", cur)
        return cur

    # templates
    def add_template(self, sig, match_id=None):
        self.db.execute("INSERT INTO templates(created_at, sig, match_id) VALUES (?,?,?)",
                        (time.time(), sig, match_id))

    def templates(self):
        return [bytes(r[0]) for r in self.db.execute("SELECT sig FROM templates")]

    # dashboard
    def dump(self):
        rows = lambda q: [dict(r) for r in self.db.execute(q)]
        snaps = rows("SELECT taken_at, synced_at, data FROM snapshots ORDER BY synced_at")
        for s in snaps:
            s["data"] = json.loads(s["data"])
        matches = rows("SELECT * FROM matches ORDER BY ended_at")
        for m in matches:
            # only the lines that mention XP or cash, to keep live refreshes small
            ocr = json.loads(m.pop("ocr_json")) if m.get("ocr_json") else []
            m["ocr_hint"] = [l["text"] for l in ocr if HINT_RE.search(l.get("text", ""))][:8]
            m["role_levels"] = json.loads(m["role_levels"]) if m.get("role_levels") else None
            m["details"] = json.loads(m["details"]) if m.get("details") else None
            if m["details"]:
                # the results screen shows no round XP, so what it gave is a feed line left on
                # it ("49xp"): the kill feed's total stands in, unless the Progression tab gave the
                # round's gain or you checked the number yourself
                feed = sum(e.get("xp") or 0 for e in m["details"].get("feed") or [] if isinstance(e, dict))
                if feed and (m["xp"] is None or not (m["reviewed"] or m["details"].get("xp_roles")) and m["xp"] < feed):
                    m["xp"], m["xp_from"] = feed, "feed"
            if m["details"]:  # the full timeline is fetched per match
                m["details"].pop("timeline", None)
                m["details"].pop("cash_events", None)
                m["details"].pop("feed", None)
                m["details"].pop("kill_log", None)
                m["details"].pop("downs_log", None)
                m["details"].pop("revives_log", None)
                m["details"].pop("respawns_log", None)
        return {
            "sessions": rows("SELECT * FROM sessions ORDER BY started_at"),
            "matches": matches,
            "snapshots": snaps,
            "unlocks": rows("SELECT * FROM unlocks ORDER BY first_seen DESC, name"),
            "meta": {r["key"]: dict(json.loads(r["value"]) if r["value"].startswith("{") else {"value": json.loads(r["value"])}, _updated=r["updated"])
                     for r in self.db.execute("SELECT key, value, updated FROM meta")},
            "templates": self.db.execute("SELECT COUNT(*) FROM templates").fetchone()[0],
        }
