from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config, db
from app.main import app


class SimulationRoutesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_db_path = config.DB_PATH
        if db._conn is not None:
            db._conn.close()
            db._conn = None
        config.DB_PATH = str(Path(self.temporary.name) / "pronoia.db")
        db.init_db()
        self.case = db.create_case("simulation route test")
        self.graph = db.add_artifact(
            self.case["id"],
            None,
            "graph",
            "evidence graph",
            {
                "question": "监管调查后各参与方会如何行动？",
                "nodes": [
                    {
                        "id": "E1",
                        "kind": "evidence",
                        "title": "监管调查",
                        "source_kind": "official",
                        "source_ref": "https://example.com/notice",
                    }
                ],
                "edges": [],
            },
        )
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        if db._conn is not None:
            db._conn.close()
            db._conn = None
        self.temporary.cleanup()
        config.DB_PATH = self.previous_db_path

    def test_simulation_and_prospective_state_survive_reinitialization_together(self):
        job = db.create_simulation_job(
            self.case["id"], self.graph["id"],
            {"job_id": "migration-test", "status": "running"}, {"horizon_days": 7},
        )
        run = db.create_prospective_run(
            name="coexistence", capture_at="2026-09-12T00:00:00+00:00", settle_after_days=3,
        )
        db.init_db()
        self.assertEqual(db.get_simulation_job(job["id"])["status"], "running")
        self.assertEqual(db.get_prospective_run(run["id"])["name"], "coexistence")
        response = self.client.get("/api/prospective/runs")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["id"], run["id"])
        db.delete_case(self.case["id"])
        self.assertIsNone(db.get_simulation_job(job["id"]))
        self.assertIsNotNone(db.get_prospective_run(run["id"]))

    def test_rerun_and_observation_window_reach_gateway(self):
        from app.routes.simulations import StartSimulationRequest, _build_gateway_payload
        payload, _ = _build_gateway_payload(self.case["id"], StartSimulationRequest(
            source_graph_artifact_id=self.graph["id"], horizon_days=7, max_actors=10, rerun=True,
        ))
        self.assertTrue(payload["rerun"])
        self.assertEqual(payload["horizon_days"], 7)
        self.assertEqual(payload["max_actors"], 10)

    def test_long_window_and_compiler_choice_reach_gateway(self):
        from app.routes.simulations import StartSimulationRequest, _build_gateway_payload
        payload, _ = _build_gateway_payload(self.case["id"], StartSimulationRequest(
            source_graph_artifact_id=self.graph["id"], horizon_days=180, product_version="v12",
        ))
        self.assertEqual(payload["horizon_days"], 180)
        self.assertEqual(payload["product_version"], "v12")

    def test_historical_sample_loading_is_local_and_preserves_cutoff_on_refresh(self):
        from app.routes.simulations import StartSimulationRequest, _build_gateway_payload
        with patch("app.routes.simulations._gateway") as remote:
            listed = self.client.get("/api/simulation-samples")
            self.assertEqual(len(listed.json()), 6)
            response = self.client.post("/api/simulation-samples/historical-06/open", json={})
            self.assertEqual(response.status_code, 201)
            remote.assert_not_called()
        sample = response.json()
        self.assertTrue(sample["case"]["title"].startswith("试用 · "))
        self.assertEqual(db.list_simulation_jobs(sample["case"]["id"]), [])
        for _ in range(2):
            payload, graph = _build_gateway_payload(sample["case"]["id"], StartSimulationRequest(
                source_graph_artifact_id=sample["graph_artifact_id"], horizon_days=180, product_version="v12"))
            self.assertEqual(payload["as_of"], "2025-08-30T23:59:59+08:00")
            self.assertEqual(payload["horizon_days"], 180)
            self.assertEqual(payload["market"]["instruments"][0]["symbol"], "002352")
            self.assertNotEqual(payload["as_of"], graph["created_at"])

    def test_invalid_sample_does_not_create_a_case(self):
        before = len(db.list_cases())
        self.assertEqual(self.client.post("/api/simulation-samples/not-a-sample/open", json={}).status_code, 404)
        self.assertEqual(len(db.list_cases()), before)

    def test_followup_routes_resolve_local_job_before_proxying(self):
        job = db.create_simulation_job(self.case["id"], self.graph["id"],
            {"job_id": "simjob_local_test", "status": "completed"}, {})
        with patch("app.routes.simulations._gateway", return_value={"snapshot": None, "journal": []}) as proxy:
            self.assertEqual(self.client.get("/api/simulations/missing/followup").status_code, 404)
            proxy.assert_not_called()
            self.assertEqual(self.client.get(f"/api/simulations/{job['id']}/followup").status_code, 200)
            proxy.assert_called_with("GET", "/v1/simulations/simjob_local_test/followup")
            self.assertEqual(self.client.post(f"/api/simulations/{job['id']}/followup", json={}).status_code, 200)
            proxy.assert_called_with("POST", "/v1/simulations/simjob_local_test/followup")
            body = {"expected_revision": 0, "observation_id": "W1", "state": "pending"}
            self.assertEqual(self.client.post(f"/api/simulations/{job['id']}/followup/entries", json=body).status_code, 200)
            proxy.assert_called_with("POST", "/v1/simulations/simjob_local_test/followup/entries", json=body)

    @staticmethod
    def completed_gateway(method, path, **kwargs):
        if method == "POST":
            return {
                "job_id": "simjob_fixed",
                "status": "queued",
                "stage": "queued",
                "progress": 0,
            }
        return {
            "job_id": "simjob_fixed",
            "status": "completed",
            "stage": "completed",
            "progress": 1,
            "finished_at": "2026-08-06T09:00:00+08:00",
            "error": None,
            "result": {
                "schema_version": "0.1.0",
                "execution": {"configured_actor_count": 4},
                "scenarios": [{"id": "branch-1", "label": "test branch"}],
                "warnings": ["test fixture"],
            },
        }

    def test_completed_job_writes_one_idempotent_simulation_artifact(self):
        body = {
            "source_graph_artifact_id": self.graph["id"],
            "mode": "quick",
        }
        with patch(
            "app.routes.simulations._gateway",
            side_effect=self.completed_gateway,
        ):
            first = self.client.post(
                f"/api/cases/{self.case['id']}/simulations", json=body
            )
            second = self.client.post(
                f"/api/cases/{self.case['id']}/simulations", json=body
            )
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json()["id"], second.json()["id"])
        artifacts = db.list_artifacts(self.case["id"])
        simulations = [item for item in artifacts if item["kind"] == "simulation"]
        self.assertEqual(len(simulations), 1)
        self.assertEqual(simulations[0]["title"], "单次多智能体情景推演")
        self.assertEqual(simulations[0]["payload"]["scenarios"][0]["id"], "branch-1")

    def test_cancel_updates_persisted_job(self):
        remote_status = {"value": "running"}

        def gateway(method, path, **kwargs):
            if path.endswith("/cancel"):
                remote_status["value"] = "cancelling"
                return {
                    "job_id": "simjob_cancel",
                    "status": "cancelling",
                    "stage": "cancel_requested",
                    "progress": 0.5,
                }
            if method == "POST":
                return {
                    "job_id": "simjob_cancel",
                    "status": "queued",
                    "stage": "queued",
                    "progress": 0,
                }
            return {
                "job_id": "simjob_cancel",
                "status": remote_status["value"],
                "stage": "simulating",
                "progress": 0.5,
                "result": None,
            }

        with patch("app.routes.simulations._gateway", side_effect=gateway):
            started = self.client.post(
                f"/api/cases/{self.case['id']}/simulations",
                json={"source_graph_artifact_id": self.graph["id"]},
            )
            cancelled = self.client.post(
                f"/api/simulations/{started.json()['id']}/cancel", json={}
            )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["status"], "cancelling")
        self.assertEqual(cancelled.json()["stage"], "cancel_requested")

    def test_preview_passes_auto_actor_budget_without_creating_artifact(self):
        def gateway(method, path, **kwargs):
            self.assertEqual(method, "POST")
            self.assertEqual(path, "/v1/simulations/preview")
            self.assertIsNone(kwargs["json"]["max_actors"])
            return {
                "actor_selection": {
                    "mode": "auto",
                    "recommended_count": 4,
                    "applied_limit": 4,
                    "configured_count": 4,
                    "rationale": "test",
                },
                "actors": [
                    {
                        "id": "actor_issuer",
                        "label": "事件主体",
                        "kind": "issuer",
                        "selection_reason": "事件研究主体",
                    }
                ],
            }

        with patch("app.routes.simulations._gateway", side_effect=gateway):
            response = self.client.post(
                f"/api/cases/{self.case['id']}/simulations/preview",
                json={"source_graph_artifact_id": self.graph["id"]},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["actor_selection"]["mode"], "auto")
        simulations = [
            item for item in db.list_artifacts(self.case["id"])
            if item["kind"] == "simulation"
        ]
        self.assertEqual(simulations, [])

    def test_failed_job_can_resume_without_creating_a_second_job(self):
        remote_status = {"value": "failed"}

        def gateway(method, path, **kwargs):
            if path.endswith("/resume"):
                remote_status["value"] = "queued"
                return {
                    "job_id": "simjob_resume",
                    "status": "queued",
                    "stage": "resuming",
                    "progress": 0.2,
                    "error": None,
                    "finished_at": None,
                }
            if method == "POST":
                return {
                    "job_id": "simjob_resume",
                    "status": "queued",
                    "stage": "queued",
                    "progress": 0,
                }
            return {
                "job_id": "simjob_resume",
                "status": remote_status["value"],
                "stage": "failed",
                "progress": 0.2,
                "error": "temporary read quota",
                "finished_at": "2026-08-29T17:42:00+08:00",
                "result": None,
            }

        with patch("app.routes.simulations._gateway", side_effect=gateway):
            started = self.client.post(
                f"/api/cases/{self.case['id']}/simulations",
                json={"source_graph_artifact_id": self.graph["id"]},
            )
            resumed = self.client.post(
                f"/api/simulations/{started.json()['id']}/resume", json={}
            )

        self.assertEqual(resumed.status_code, 202)
        self.assertEqual(resumed.json()["id"], started.json()["id"])
        self.assertEqual(resumed.json()["status"], "queued")
        self.assertEqual(resumed.json()["stage"], "resuming")
        self.assertIsNone(resumed.json()["error"])


if __name__ == "__main__":
    unittest.main()
