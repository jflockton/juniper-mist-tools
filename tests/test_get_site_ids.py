import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import get_site_ids


class PagedClient:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []

    def get_json(self, path, *, params=None):
        self.calls.append((path, params))
        return next(self.pages)


class SiteIdTests(unittest.TestCase):
    def test_make_env_key_normalises_punctuation(self):
        self.assertEqual(
            get_site_ids.make_env_key("Milton Keynes / SOC"),
            "site_Milton_Keynes_SOC",
        )

    def test_get_all_sites_pages_and_deduplicates(self):
        client = PagedClient(
            [
                [{"id": "1", "name": "One"}, {"id": "2", "name": "Two"}],
                [{"id": "2", "name": "Two"}, {"id": "3", "name": "Three"}],
                [],
            ]
        )
        with patch.object(get_site_ids, "PAGE_LIMIT", 2):
            sites = get_site_ids.get_all_sites(client, "org-id")

        self.assertEqual([site["id"] for site in sites], ["1", "2", "3"])
        self.assertEqual(len(client.calls), 3)

    def test_write_site_codes_suffixes_colliding_names(self):
        sites = [
            {"id": "1", "name": "A-B"},
            {"id": "2", "name": "A B"},
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "site_codes.env"
            with patch.object(get_site_ids, "OUTPUT_FILE", output):
                count = get_site_ids.write_site_codes(sites)
            content = output.read_text(encoding="utf-8")

        self.assertEqual(count, 2)
        self.assertIn("site_A_B=2", content)
        self.assertIn("site_A_B_2=1", content)


if __name__ == "__main__":
    unittest.main()
