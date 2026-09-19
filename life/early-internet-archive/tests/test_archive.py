from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "app"))

import build_library
from archive_web import main as archive_web
from fastapi.testclient import TestClient


class CatalogBuilderTests(unittest.TestCase):
    def test_clean_text_and_dates(self) -> None:
        self.assertEqual(
            build_library.clean_text(
                "[b]Hello[/b]<br>[color=red]world[/color] &amp; friends[list][*]one[/list]"
            ),
            "Hello\nworld & friends\n• one",
        )
        self.assertEqual(
            build_library.normalize_date("", 1192125964),
            "2007-10-11T18:06:04+00:00",
        )
        self.assertEqual(
            build_library.normalize_date("20100203040506"),
            "2010-02-03T04:05:06+00:00",
        )

    def test_duplicate_asset_keeps_all_item_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "catalog.sqlite3"
            db = build_library.create_catalog(catalog)
            for item_id in ("source:1", "source:2"):
                build_library.add_item(
                    db,
                    item_id=item_id,
                    source="source",
                    kind="post",
                    external_id=item_id.rsplit(":", 1)[1],
                    title=item_id,
                    body="body",
                    author="author",
                    published_at="2020-01-01T00:00:00+00:00",
                    canonical_url="https://example.com",
                )
            for item_id in ("source:1", "source:2"):
                build_library.add_asset(
                    db,
                    asset_id="source:digest",
                    source="source",
                    original_url="https://example.com/image.png",
                    media_path="source/media/image.png",
                    mime_type="image/png",
                    captured_at="20200101000000",
                    sha256="digest",
                    byte_length=3,
                    item_ids=[item_id],
                )
            linked = json.loads(
                db.execute("SELECT item_ids_json FROM assets").fetchone()[0]
            )
            db.close()
            self.assertEqual(linked, ["source:1", "source:2"])

    def test_build_refuses_an_empty_source_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "library.sqlite3"
            with self.assertRaisesRegex(RuntimeError, "no supported archive"):
                build_library.build(root, output)
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".library.sqlite3.*.tmp")), [])


class FrontendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.archive_root = root / "archive"
        self.archive_root.mkdir()
        media = self.archive_root / "fixture" / "media"
        media.mkdir(parents=True)
        (media / "image.png").write_bytes(b"a recovered image")

        self.catalog = root / "library.sqlite3"
        db = build_library.create_catalog(self.catalog)
        build_library.add_item(
            db,
            item_id="fixture:1",
            source="fixture",
            kind="forum-post",
            external_id="1",
            title="Mario's archive post",
            body="A searchable recovered conversation.",
            author="StarFoxA",
            published_at="2010-02-03T04:05:06+00:00",
            canonical_url="javascript:alert(1)",
            section="Test forum",
            tags=["mario", "archive"],
        )
        db.execute(
            """INSERT INTO context_messages(
                   item_id,sequence,external_id,author,published_at,body,is_owner
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                "fixture:1",
                1,
                "context-1",
                "Friend",
                "2010-02-03T04:00:00+00:00",
                "Earlier context",
                0,
            ),
        )
        build_library.add_asset(
            db,
            asset_id="fixture:image",
            source="fixture",
            original_url="https://example.com/image.png",
            media_path="fixture/media/image.png",
            mime_type="image/png",
            captured_at="20100203040506",
            sha256="digest",
            byte_length=17,
            item_ids=["fixture:1"],
        )
        build_library.add_asset(
            db,
            asset_id="fixture:escape",
            source="fixture",
            original_url="https://example.com/escape",
            media_path="../../etc/passwd",
            mime_type="text/plain",
            captured_at="",
            sha256="bad",
            byte_length=0,
        )
        build_library.build_stats(db)
        db.executemany(
            "INSERT INTO metadata(key,value) VALUES(?,?)",
            [
                ("schema_version", "1"),
                ("generated_at", json.dumps("2020-01-01T00:00:00+00:00")),
                ("sources", json.dumps(["fixture"])),
            ],
        )
        db.commit()
        db.close()

        archive_web.CATALOG = self.catalog
        archive_web.ARCHIVE_ROOT = self.archive_root
        self.client = TestClient(archive_web.app)

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()

    def test_health_home_search_item_gallery_and_asset(self) -> None:
        health = self.client.get("/healthz")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "ok"})

        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("Mario&#39;s archive post", home.text)
        self.assertEqual(home.headers["x-content-type-options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", home.headers["content-security-policy"])

        search = self.client.get("/search", params={"q": "searchable", "source": "fixture"})
        self.assertEqual(search.status_code, 200)
        self.assertIn("1 result", search.text)

        api = self.client.get("/api/v1/search", params={"q": "Mario's"})
        self.assertEqual(api.status_code, 200)
        self.assertEqual(api.json()["total"], 1)
        self.assertEqual(api.json()["pages"], 1)
        self.assertEqual(api.json()["items"][0]["canonical_url"], "")

        item = self.client.get("/item/fixture/1")
        self.assertEqual(item.status_code, 200)
        self.assertIn("Earlier context", item.text)
        self.assertNotIn("javascript:alert", item.text)

        gallery = self.client.get("/gallery")
        self.assertEqual(gallery.status_code, 200)
        self.assertIn("2010-02-03", gallery.text)

        asset = self.client.get("/asset/fixture:image")
        self.assertEqual(asset.status_code, 200)
        self.assertEqual(asset.content, b"a recovered image")
        self.assertEqual(asset.headers["content-type"], "image/png")

    def test_rejects_bad_pages_and_out_of_root_assets(self) -> None:
        self.assertEqual(self.client.get("/search?page=2").status_code, 404)
        self.assertEqual(self.client.get("/gallery?page=2").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/search?page=2").status_code, 404)
        self.assertEqual(self.client.get("/asset/fixture:escape").status_code, 404)

    def test_health_reports_missing_catalog(self) -> None:
        archive_web.CATALOG = self.catalog.with_name("missing.sqlite3")
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "catalog-missing"})


if __name__ == "__main__":
    unittest.main()
