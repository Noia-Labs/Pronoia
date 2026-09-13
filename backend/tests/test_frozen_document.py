import unittest
from unittest.mock import patch
import importlib.util
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

from backend.app.agents import team as team_mod
from backend.app.agents.roster import AGENTS
from backend.app.skills.frozen_document import frozen_announcement_fetch
from backend.app.skills.registry import ensure_skills_loaded, tools_for_agent


class _Response:
    def __init__(self, payload=None, *, text="", url="https://www.sec.gov/test.htm"):
        self._payload = payload
        self.text = text
        self.content = text.encode()
        self.url = url
        self.headers = {"content-type": "text/html"}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class TestFrozenAnnouncementFetch(unittest.IsolatedAsyncioTestCase):
    def test_event_scout_exposes_frozen_fetch(self):
        ensure_skills_loaded()
        names = [tool["function"]["name"] for tool in tools_for_agent("event_scout")]
        self.assertIn("frozen_announcement_fetch", names)
        self.assertIn("frozen_announcement_fetch", AGENTS["event_scout"]["skills"])

    def test_event_metadata_fills_fetch_identity(self):
        result = team_mod._inject_event_skill_defaults(
            "frozen_announcement_fetch",
            {},
            {
                "source_url": "https://data.eastmoney.com/notices/detail/000001/AN123.html",
                "source_key": "AN123",
                "market": "CN",
                "symbol": "000001",
                "issuer_name": "平安银行",
                "event_time": "2026-09-01T08:00:00+08:00",
            },
        )
        self.assertEqual(result["source_key"], "AN123")
        self.assertEqual(result["symbol"], "000001")
        self.assertEqual(result["event_date"], "2026-09-01")

    async def test_rejects_non_allowlisted_host(self):
        result = await frozen_announcement_fetch(
            source_url="https://example.com/AN123.html", source_key="AN123",
            market="CN", symbol="000001", issuer_name="平安银行", event_date="2026-09-01",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "source_host_not_allowlisted")

    async def test_fetches_and_validates_eastmoney_pages(self):
        def fake_get(url, *, params, headers, timeout):
            page = params["page_index"]
            payload = {
                "data": {
                    "notice_content": (
                        "证券代码000001 平安银行公告。" + "本次公告包含可核验交易条款。" * 20
                        if page == 1 else "第二页补充金额、比例和生效条件。" * 20
                    ),
                    "page_size": 2,
                    "notice_title": "平安银行测试公告",
                    "notice_date": "2026-09-01",
                }
            }
            return _Response(payload)

        with patch("backend.app.skills.frozen_document.requests.get", side_effect=fake_get):
            result = await frozen_announcement_fetch(
                source_url="https://data.eastmoney.com/notices/detail/000001/AN123.html",
                source_key="AN123", market="CN", symbol="000001", issuer_name="平安银行",
                event_date="2026-09-01", max_chars=1000,
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["identity_ok"])
        self.assertTrue(result["data"]["as_of_ok"])
        self.assertEqual(result["data"]["pages"], 2)
        self.assertTrue(result["data"]["content_sha256"])

    async def test_online_runner_prefetches_once_and_replaces_metadata_capsule(self):
        path = Path(__file__).resolve().parents[2] / "pipelines" / "online" / "run_agent_prompt_ab.py"
        spec = importlib.util.spec_from_file_location("online_prompt_ab_for_test", path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)

        async def fake_execute(name, args):
            self.assertEqual(name, "frozen_announcement_fetch")
            self.assertEqual(args["source_key"], "AN123")
            return {
                "ok": True,
                "data": {
                    "content": "证券代码000001 平安银行冻结公告正文" * 30,
                    "content_chars": 600,
                    "content_sha256": "abc",
                    "identity_ok": True,
                    "as_of_ok": True,
                    "source_published_at": "2026-09-01 08:00:00",
                },
            }

        event = {
            "event_id": "e1", "market": "CN", "symbol": "000001", "issuer_name": "平安银行",
            "event_time": "2026-09-01", "source_url": "https://data.eastmoney.com/x",
            "source_key": "AN123", "event_text": "[METADATA_ONLY_PACKET]",
            "event_text_kind": "metadata_capsule_not_announcement_body", "research_required": True,
        }
        with patch.object(module, "execute_skill", side_effect=fake_execute):
            prepared, trace = await module._prepare_frozen_event(event)
        self.assertEqual(prepared["event_text_kind"], "frozen_announcement_body")
        self.assertFalse(prepared["research_required"])
        self.assertEqual(prepared["source_document_hash"], "abc")
        self.assertTrue(trace["ok"])
        self.assertEqual(trace["agent"], "event_scout")

    def test_precise_publication_time_drives_effective_session(self):
        path = Path(__file__).resolve().parents[2] / "pipelines" / "online" / "build_precise_t1_t3_cohort.py"
        spec = importlib.util.spec_from_file_location("precise_cohort_for_test", path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        days = [dt.date(2026, 8, 31), dt.date(2026, 9, 1), dt.date(2026, 9, 2)]
        tz = ZoneInfo("Asia/Shanghai")
        self.assertEqual(
            module.effective_session(dt.datetime(2026, 9, 1, 8, 30, tzinfo=tz), days),
            dt.date(2026, 9, 1),
        )
        self.assertEqual(
            module.effective_session(dt.datetime(2026, 8, 31, 18, 30, tzinfo=tz), days),
            dt.date(2026, 9, 1),
        )
        self.assertEqual(
            module.effective_session(dt.datetime(2026, 9, 1, 10, 0, tzinfo=tz), days),
            dt.date(2026, 9, 2),
        )


if __name__ == "__main__":
    unittest.main()
