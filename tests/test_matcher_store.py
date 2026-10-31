import os
import tempfile
import unittest

from wdt.matcher import EndScreenMatcher, distance
from wdt.store import Store

END = bytes([200] * 50 + [20] * 50)
GAME = bytes([90] * 100)


class RoleXpFixTests(unittest.TestCase):
    def test_old_misread_role_gains_are_dropped_once(self):
        # 2026-10-29: older versions took the next-level target (43,750) as XP after the round
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.db")
            st = Store(path)
            sid = st.start_session(0)
            old = st.add_match(sid, 0, 100, "auto", parsed={"xp": 3869},
                               details={"role_xp": {"Assault": {"before": 39881, "after": 43750, "gain": 3869}}})
            new = st.add_match(sid, 200, 300, "auto", parsed={"xp": 1700},
                               details={"role_xp": {"Assault": {"before": 39881, "after": 41581, "next": 43750, "gain": 1700}}})
            st.set_meta("role_xp_fixed", 0)
            st.db.close()
            st = Store(path)
            ms = {m["id"]: m for m in st.dump()["matches"]}
            self.assertIsNone(ms[old]["xp"])
            self.assertEqual(ms[old]["details"]["role_xp"], {"Assault": {"seen": 39881, "gain": None}})
            self.assertEqual((ms[new]["xp"], ms[new]["details"]["role_xp"]["Assault"]["gain"]), (1700, 1700))
            self.assertEqual(st.role_xp_known(), {"Assault": 41581})
            st.db.close()


class FeedXpTests(unittest.TestCase):
    def test_kill_feed_xp_stands_in_for_a_misread_round_xp(self):
        # 2026-10-30: the results screen shows no round XP; a "49xp" feed line left on it was
        # taken for the round's XP while the kill feed added up to far more
        with tempfile.TemporaryDirectory() as d:
            st = Store(os.path.join(d, "t.db"))
            sid = st.start_session(0)
            feed = {"feed": [{"label": "Kill", "xp": 100}, {"label": "Tip", "cash": 25, "xp": None}, {"label": "Other", "xp": 49}]}
            misread = st.add_match(sid, 0, 100, "auto", parsed={"xp": 49}, details=feed)
            none = st.add_match(sid, 200, 300, "auto", details=feed)
            roles = st.add_match(sid, 400, 500, "auto", parsed={"xp": 1700}, details=dict(feed, xp_roles=True))
            checked = st.add_match(sid, 600, 700, "auto", parsed={"xp": 60}, details=feed)
            st.update_match(checked, {"reviewed": 1})
            blank = st.add_match(sid, 800, 850, "auto", details=feed)  # checked with no XP filled in
            st.update_match(blank, {"reviewed": 1})
            empty = st.add_match(sid, 900, 1000, "auto")
            ms = {m["id"]: m for m in st.dump()["matches"]}
            self.assertEqual([(ms[i]["xp"], ms[i].get("xp_from")) for i in (misread, none, roles, checked, blank, empty)],
                             [(149, "feed"), (149, "feed"), (1700, None), (60, None), (149, "feed"), (None, None)])
            st.db.close()


def _l(text, x, y):
    return {"text": text, "x": x, "y": y}


# a results screen read with a Cyrillic recognizer (issue #28, 2026-10-30): no $ on amounts,
# I read as 1, and "TOTAL PROFIT" read as "ТОПЕ ПОЮТ"; the chat line really is Russian
CYRILLIC_SCREEN = [_l("32,543", 1660, 30), _l("366,147", 1790, 30), _l("• RNS]SthsRy: а ты где", 44, 132),
                   _l("DEFEAT", 960, 400), _l("LONESTAR W1NS", 960, 470),
                   _l("2ND + CONTRIBUTION", 599, 808), _l("62,400", 1298, 808), _l("SPENT", 599, 863),
                   _l("-610,464", 1288, 863), _l("EARNED", 599, 918), _l("613,700", 1304, 918),
                   _l("REFUNDED", 599, 973), _l("63,901", 1300, 973), _l("ТОПЕ ПОЮТ", 599, 1028),
                   _l("69,537", 1297, 1028), _l("NEXT ROUND 1N 00:32", 880, 1100)]
ENGLISH_SCREEN = [_l("$13,000", 1588, 25), _l("VALKYRA WINS", 960, 470), _l("CASH SPENT", 599, 863),
                  _l("-$12,823", 1288, 863)]


class CyrillicFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        self.st = Store(self.path)
        self.sid = self.st.start_session(0)

    def tearDown(self):
        self.st.db.close()
        self.tmp.cleanup()

    def reopen(self):
        self.st.set_meta("cyrillic_fixed", 0)
        self.st.db.close()
        self.st = Store(self.path)
        return {m["id"]: m for m in self.st.dump()["matches"]}

    def test_a_misread_screen_is_repaired_and_read_again(self):
        mid = self.st.add_match(self.sid, 0, 100, "auto", ocr=CYRILLIC_SCREEN, details={"result": "defeat"})
        m = self.reopen()[mid]
        d = m["details"]
        self.assertEqual((d["winner"], d["result"]), ("LONESTAR", "defeat"))
        self.assertEqual((d["cash_summary"]["spent"], d["cash_summary"]["gained"]), (610464, 613700))
        self.assertEqual((m["cash_earned"], m["cash_spent"]), (613700, 610464))
        text = self.st.match_text(mid)
        self.assertIn("TOTAL PROFIT", text)
        self.assertIn("$69,537", text)
        self.assertIn("• RNS]SthsRy: а ты где", text)  # chat stays as written
        self.assertEqual(d["ocr_before_repair"], CYRILLIC_SCREEN)

    def test_nothing_already_set_changes(self):
        mid = self.st.add_match(self.sid, 0, 100, "auto", ocr=CYRILLIC_SCREEN,
                                parsed={"cash_earned": 13000, "cash_spent": 4000},
                                details={"result": "victory", "winner": "VIPER"})  # set by hand
        m = self.reopen()[mid]
        self.assertEqual((m["cash_earned"], m["cash_spent"]), (13000, 4000))
        self.assertEqual((m["details"]["result"], m["details"]["winner"]), ("victory", "VIPER"))

    def test_screens_read_in_english_are_not_touched(self):
        mid = self.st.add_match(self.sid, 0, 100, "auto", ocr=ENGLISH_SCREEN, details={"result": "defeat"})
        m = self.reopen()[mid]
        self.assertEqual(m["details"], {"result": "defeat"})
        self.assertEqual(self.st.match_text(mid), [l["text"] for l in ENGLISH_SCREEN])
        self.assertIsNone(m["cash_earned"])

    def test_it_runs_once(self):
        self.reopen()
        mid = self.st.add_match(self.sid, 0, 100, "auto", ocr=CYRILLIC_SCREEN)
        self.st.db.close()
        self.st = Store(self.path)
        self.assertNotIn("TOTAL PROFIT", self.st.match_text(mid))


class ReadAgainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Store(os.path.join(self.tmp.name, "t.db"))
        self.sid = self.st.start_session(0)
        self.shot = os.path.join(self.tmp.name, "end.png")
        open(self.shot, "wb").close()

    def tearDown(self):
        self.st.db.close()
        self.tmp.cleanup()

    def match(self, mid):
        return {m["id"]: m for m in self.st.dump()["matches"]}[mid]

    def test_what_the_screen_says_comes_from_the_new_read(self):
        old = [_l("VIPER W1NS", 960, 470)]  # an older reader's mistakes
        mid = self.st.add_match(self.sid, 0, 100, "auto", screenshot=self.shot, ocr=old,
                                details={"winner": "VIPER"})
        changed = self.st.reread_match(mid, [(self.shot, CYRILLIC_SCREEN)])
        m = self.match(mid)
        self.assertEqual(m["details"]["winner"], "LONESTAR")
        self.assertEqual(m["details"]["cash_summary"]["spent"], 610464)
        self.assertEqual((m["cash_earned"], m["cash_spent"]), (613700, 610464))
        self.assertIn("TOTAL PROFIT", self.st.match_text(mid))
        self.assertEqual(m["details"]["ocr_before_reread"], old)
        self.assertTrue({"winner", "cash_summary", "cash", "text"} <= set(changed))

    def test_your_own_changes_stay(self):
        mid = self.st.add_match(self.sid, 0, 100, "auto", screenshot=self.shot,
                                parsed={"cash_earned": 13000, "cash_spent": 4000, "xp": 900},
                                details={"result": "victory"})
        self.st.reread_match(mid, [(self.shot, CYRILLIC_SCREEN)])
        m = self.match(mid)
        self.assertEqual((m["cash_earned"], m["cash_spent"], m["xp"]), (13000, 4000, 900))
        self.assertEqual(m["details"]["result"], "victory")

    def test_reading_the_same_thing_changes_nothing(self):
        mid = self.st.add_match(self.sid, 0, 100, "auto", screenshot=self.shot, ocr=ENGLISH_SCREEN)
        self.st.reread_match(mid, [(self.shot, ENGLISH_SCREEN)])
        self.assertEqual(self.st.reread_match(mid, [(self.shot, ENGLISH_SCREEN)]), [])

    def test_only_pictures_still_on_disk_are_offered(self):
        mid = self.st.add_match(self.sid, 0, 100, "auto", screenshot=self.shot,
                                details={"end_shots": [os.path.join(self.tmp.name, "gone.png")]})
        self.assertEqual(self.st.match_pictures(mid), [self.shot])
        self.assertEqual(self.st.match_pictures(mid + 1), [])


class MatcherTests(unittest.TestCase):
    def test_needs_consecutive_hits_then_cools_down(self):
        m = EndScreenMatcher([END], threshold=10, confirm=2, cooldown=60)
        self.assertFalse(m.feed(END, 0))
        self.assertTrue(m.feed(END, 2))
        self.assertFalse(m.feed(END, 4))       # same end screen still showing
        self.assertFalse(m.feed(GAME, 30))
        self.assertFalse(m.feed(END, 70))
        self.assertTrue(m.feed(END, 72))       # next match

    def test_miss_resets_streak(self):
        m = EndScreenMatcher([END], threshold=10, confirm=2)
        m.feed(END, 0)
        m.feed(GAME, 2)
        self.assertFalse(m.feed(END, 4))

    def test_no_templates_never_matches(self):
        self.assertFalse(EndScreenMatcher([]).feed(END, 0))

    def test_small_changes_still_match(self):
        noisy = bytes(min(255, b + 5) for b in END)
        self.assertLess(distance(END, noisy), 10)

    def test_manual_end_suppresses_auto(self):
        m = EndScreenMatcher([END], threshold=10, confirm=1, cooldown=60)
        m.mark_end(0)
        self.assertFalse(m.feed(END, 5))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.s = Store(os.path.join(self.dir.name, "t.db"))

    def tearDown(self):
        self.s.db.close()
        self.dir.cleanup()

    def test_match_roundtrip_and_edit(self):
        sid = self.s.start_session(100)
        mid = self.s.add_match(sid, 100, 900, "auto", parsed={"xp": 500, "cash_earned": 9000})
        self.s.update_match(mid, {"xp": 650, "reviewed": 1, "session_id": 99})
        m = self.s.dump()["matches"][0]
        self.assertEqual((m["xp"], m["cash_earned"], m["reviewed"], m["session_id"]),
                         (650, 9000, 1, sid))
        self.assertEqual(self.s.last_match_end(sid), 900)

    def test_misread_team_names_are_cleared_on_open(self):
        sid = self.s.start_session(100)
        for team in ("MATE", "mates", "Viper"):
            self.s.add_match(sid, 100, 900, "auto", details={"hud": {"reads": 3, "team": team}})
        self.s.db.close()
        self.s = Store(os.path.join(self.dir.name, "t.db"))
        teams = [m["details"]["hud"]["team"] for m in self.s.dump()["matches"]]
        self.assertEqual(sorted(teams, key=str), sorted([None, None, "Viper"], key=str))

    def test_misread_and_equipment_gun_names_are_fixed_on_open(self):
        sid = self.s.start_session(100)
        self.s.add_match(sid, 100, 900, "auto", details={"hud": {"reads": 9, "guns": {
            "MPS": 9, "MP5": 2, "Infrared Range Finder": 16, "MSOO": 8, "Defibrillator": 1}},
            "timeline": [{"t": 1, "gun": "MPS"}, {"t": 2, "gun": "Infrared Range Finder"}],
            "rewards": {"Target Spotted": {"n": 2, "cash": 0, "xp": 50}, "Targetspotted": {"n": 1, "cash": 0, "xp": 25},
                        "Lennayyyy": {"n": 1, "cash": 15, "xp": 0}},
            "kill_log": [{"t": 3, "kind": "kill", "other": "1220m) Hydra"}]})
        self.s.db.execute("DELETE FROM meta WHERE key='names_fixed'")  # a database from before the fix
        self.s.db.close()
        self.s = Store(os.path.join(self.dir.name, "t.db"))
        self.assertEqual(self.s.dump()["matches"][0]["details"]["hud"]["guns"], {"MP5": 11, "M500": 8})
        row = self.s.db.execute("SELECT details FROM matches").fetchone()[0]
        self.assertEqual([e["gun"] for e in __import__("json").loads(row)["timeline"]], ["MP5", None])
        det = __import__("json").loads(row)
        self.assertEqual(det["rewards"], {"Target Spotted": {"n": 3, "cash": 0, "xp": 75}, "Tip": {"n": 1, "cash": 15, "xp": 0}})
        self.assertEqual(det["kill_log"][0]["other"], "Hydra")

    def test_result_and_role_can_be_set_by_hand(self):
        sid = self.s.start_session(100)
        mid = self.s.add_match(sid, 100, 900, "auto", details={"map": "Bakurani"})
        self.s.update_match(mid, {"result": "victory", "role": "Medic"})
        d = self.s.dump()["matches"][0]["details"]
        self.assertEqual((d["result"], d["role"], d["map"]), ("victory", "Medic", "Bakurani"))
        self.s.update_match(mid, {"result": None, "role": "Wizard"})  # cleared; unknown roles ignored
        d = self.s.dump()["matches"][0]["details"]
        self.assertEqual((d["result"], d["role"]), (None, "Medic"))
        self.s.update_match(mid, {"result": "<script>"})
        self.assertIsNone(self.s.dump()["matches"][0]["details"]["result"])

    def test_snapshot_dedupes_by_sync_time(self):
        self.assertTrue(self.s.add_snapshot({"syncedAt": "a", "careerXp": 1}))
        self.assertFalse(self.s.add_snapshot({"syncedAt": "a", "careerXp": 1}))
        self.assertTrue(self.s.add_snapshot({"syncedAt": "b", "careerXp": 2}))

    def test_crashed_session_closed_at_last_match(self):
        sid = self.s.start_session(100)
        self.s.add_match(sid, 100, 500, "auto")
        self.s.close_open_sessions()
        self.assertEqual(self.s.dump()["sessions"][0]["ended_at"], 500)


if __name__ == "__main__":
    unittest.main()
