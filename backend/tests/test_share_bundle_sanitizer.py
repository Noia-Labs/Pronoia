"""Regression coverage for the staged share-bundle sanitizer."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SANITIZER = PROJECT_ROOT / "scripts" / "sanitize_share_bundle.py"


class TestShareBundleSanitizer(unittest.TestCase):
    def test_removes_model_addresses_and_sender_values_without_touching_data_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            stage = root / "Pronoia-team-share"
            (stage / "backend" / "app").mkdir(parents=True)
            (stage / "frontend" / "src").mkdir(parents=True)
            (stage / "frontend" / "dist").mkdir(parents=True)
            (stage / "scripts").mkdir(parents=True)
            (stage / "docs").mkdir(parents=True)

            provider_url = "https://models.vendor.example/v1"
            sender_url = "https://private-gateway.example/v2"
            sender_key = "sender-key-that-must-not-ship"
            (stage / ".env.example").write_text(
                f"ARK_API_URL={provider_url}\nARK_API_KEY=your-api-key-here\n",
                encoding="utf-8",
            )
            (stage / ".env.share.example").write_text(
                f"MAAS_API_URL={sender_url}\nMAAS_API_KEY=\n",
                encoding="utf-8",
            )
            (stage / "backend" / "app" / "config.py").write_text(
                f'URL = os.getenv("ARK_API_URL", "{provider_url}")\n',
                encoding="utf-8",
            )
            (stage / "frontend" / "src" / "providers.ts").write_text(
                f'const preset = {{ baseUrl: "{provider_url}" }};\n',
                encoding="utf-8",
            )
            (stage / "frontend" / "dist" / "app.js").write_text(
                f'const u="{provider_url}",p="{sender_url}";\n',
                encoding="utf-8",
            )
            (stage / "scripts" / "setup.sh").write_text(
                f"default_url='{provider_url}'\n",
                encoding="utf-8",
            )
            (stage / "docs" / "plan.md").write_text(
                f"LLM: ARK_API_URL={provider_url}\n",
                encoding="utf-8",
            )
            data_source = "https://public-data.example/query"
            (stage / "backend" / "app" / "market.py").write_text(
                f'DATA_SOURCE = "{data_source}"\n', encoding="utf-8"
            )
            (stage / "notes.txt").write_text(
                f"sender endpoint={sender_url}\ncredential={sender_key}\n",
                encoding="utf-8",
            )
            source_env = root / ".env"
            source_env.write_text(
                f"ARK_API_URL={sender_url}\nARK_API_KEY={sender_key}\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SANITIZER),
                    str(stage),
                    "--source-env",
                    str(source_env),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertNotIn(sender_url, completed.stdout + completed.stderr)
            self.assertNotIn(sender_key, completed.stdout + completed.stderr)
            combined = "\n".join(
                path.read_text(encoding="utf-8")
                for path in stage.rglob("*")
                if path.is_file()
            )
            self.assertNotIn(provider_url, combined)
            self.assertNotIn(sender_url, combined)
            self.assertNotIn(sender_key, combined)
            self.assertIn("ARK_API_URL=", combined)
            self.assertIn('baseUrl: ""', combined)
            self.assertIn(data_source, combined)
            self.assertTrue((stage / "SHARE_SANITIZATION.md").is_file())


if __name__ == "__main__":
    unittest.main()
