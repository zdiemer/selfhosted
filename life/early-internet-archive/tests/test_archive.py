from __future__ import annotations

import gzip
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "app"))

import build_library
import scrape_backloggd
import scrape_giantbomb
import scrape_official_nsider
import scrape_source_sweep
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


class OfficialNsiderParserTests(unittest.TestCase):
    def test_profile_identity_and_thread_posts(self) -> None:
        profile = """
        <title>View Profile for STARFOXA - Nintendo NSider Forums</title>
        <tr><td><span class="title">Rank</span></td><td>Mr. Saturn</td></tr>
        <tr><td><span class="title">Date Registered</span></td>
        <td><span class=date_text>06-05-2005</span> <span class=time_text>05:05 PM</span></td></tr>
        <tr><td><span class="title">Date Last Visited</span></td>
        <td><span class=date_text>08-21-2007</span> <span class=time_text>07:17 PM</span></td></tr>
        <tr><td><span class="title">Total Posts</span></td><td>10138</td></tr>
        """
        parsed_profile = scrape_official_nsider.parse_profile(profile)
        self.assertEqual(parsed_profile["username"], "STARFOXA")
        self.assertEqual(parsed_profile["registered_at"], "2005-06-05T17:05:00")
        self.assertEqual(parsed_profile["total_posts"], 10138)

        page = """
        <title>A preserved thread - Power On - Nintendo NSider Forums</title>
        <tr id="M123"><td colspan=1>
          <td class="subjectbar" width="100%">A preserved thread</td>
          <td class="msg_user_cell">
            <a href='/nintendo/view_profile?user.id=106819' class="auth_text">
              <span>STARFOXA</span></a>
            Reply <a href="/nintendo/board/message?board.id=np_po&amp;message.id=123#M123">1</a> of 2
          </td>
          <td class="msg_text_cell"><p>Target &amp; body</p></td></tr><tr>
          <td class="msg_date_cell"><span class=date_text>06-06-2005</span>
            <span class=time_text>01:02 PM</span></td>
        </tr>
        <tr id="M124"><td colspan=1>
          <td class="subjectbar" width="100%">Re: A preserved thread</td>
          <td class="msg_user_cell">
            <a href='/nintendo/view_profile?user.id=42' class="auth_text">Friend</a>
            Reply <a href="/nintendo/board/message?board.id=np_po&amp;message.id=124#M124">2</a> of 2
          </td>
          <td class="msg_text_cell"><p>Context reply</p></td></tr><tr>
          <td class="msg_date_cell"><span class=date_text>06-06-2005</span>
            <span class=time_text>01:03 PM</span></td>
        </tr>
        """
        parsed_page = scrape_official_nsider.parse_page(
            page,
            "http://forums.nintendo.com/nintendo/board/message?board.id=np_po&message.id=123",
        )
        self.assertEqual(parsed_page["thread_title"], "A preserved thread")
        self.assertEqual(len(parsed_page["posts"]), 2)
        self.assertEqual(parsed_page["posts"][0]["author_id"], "106819")
        self.assertEqual(parsed_page["posts"][0]["content_text"], "Target & body")
        self.assertEqual(parsed_page["posts"][1]["post_id"], "124")

    def test_recovered_post_is_imported_into_catalog(self) -> None:
        page = b"""
        <title>Recovered title - Power On - Nintendo NSider Forums</title>
        <tr id="M456"><td colspan=1>
          <td class="subjectbar" width="100%">Recovered title</td>
          <td class="msg_user_cell">
            <a href='/nintendo/view_profile?user.id=106819' class="auth_text">STARFOXA</a>
            Reply <a href="/nintendo/board/message?board.id=np_po&amp;message.id=456#M456">1</a> of 1
          </td>
          <td class="msg_text_cell"><p>Recovered words</p></td></tr><tr>
          <td class="msg_date_cell"><span class=date_text>07-01-2005</span>
            <span class=time_text>02:03 PM</span></td>
        </tr>
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "official_nsider"
            db = scrape_official_nsider.connect(source_root / "official_nsider.sqlite3")
            scrape_official_nsider.save_page(
                db,
                source_root,
                {
                    "timestamp": "20050702000000",
                    "original": (
                        "http://forums.nintendo.com/nintendo/board/message?"
                        "board.id=np_po&message.id=456"
                    ),
                    "digest": "fixture",
                    "length": str(len(page)),
                },
                page,
            )
            db.close()
            catalog = root / "library.sqlite3"
            build_library.build(root, catalog)
            result = sqlite3.connect(catalog)
            item = result.execute(
                "SELECT source,title,body FROM items WHERE id='official_nsider:456'"
            ).fetchone()
            stats = result.execute(
                "SELECT label,item_count FROM source_stats WHERE source='official_nsider'"
            ).fetchone()
            result.close()
            self.assertEqual(item, ("official_nsider", "Recovered title", "Recovered words"))
            self.assertEqual(stats, ("Official NSider", 1))


class SourceSweepTests(unittest.TestCase):
    def test_seed_inventory_and_url_variants(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = scrape_source_sweep.connect(Path(directory) / "sweep.sqlite3")
            scrape_source_sweep.load_seeds(
                db, PROJECT_ROOT / "source_sweep_seeds.json"
            )
            identities = db.execute("SELECT COUNT(*) FROM identities").fetchone()[0]
            targets = db.execute("SELECT COUNT(*) FROM targets").fetchone()[0]
            giantbomb = db.execute(
                "SELECT confidence FROM identities WHERE source='giantbomb'"
            ).fetchone()[0]
            acc = db.execute(
                "SELECT handle,confidence FROM identities "
                "WHERE source='animal_crossing_community'"
            ).fetchone()
            gog = db.execute(
                "SELECT confidence FROM identities WHERE source='gog'"
            ).fetchone()[0]
            db.close()
            self.assertEqual(identities, 23)
            self.assertEqual(targets, 50)
            self.assertEqual(giantbomb, "confirmed")
            self.assertEqual(tuple(acc), ("Irock", "confirmed"))
            self.assertEqual(gog, "confirmed")
        self.assertEqual(
            scrape_source_sweep.query_urls("https://www.example.com/path/"),
            ["example.com/path", "example.com/path/", "www.example.com/path", "www.example.com/path/"],
        )
        self.assertEqual(
            scrape_source_sweep.query_urls(
                "http://www.example.com/profile.asp?UserName=Irock"
            ),
            [
                "example.com/profile.asp/?UserName=Irock",
                "example.com/profile.asp?UserName=Irock",
                "www.example.com/profile.asp/?UserName=Irock",
                "www.example.com/profile.asp?UserName=Irock",
            ],
        )

    def test_common_crawl_json_and_warc_payload_parsing(self) -> None:
        rows = scrape_source_sweep.parse_json_lines(
            b'{"url":"https://example.com","status":"200"}\nnot-json\n'
        )
        self.assertEqual(rows[0]["status"], "200")
        response = b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n<body>saved</body>"
        record = b"WARC/1.0\r\nContent-Length: 74\r\n\r\n" + response
        buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode="wb") as output:
            output.write(record)
        self.assertEqual(
            scrape_source_sweep.extract_warc_payload(buffer.getvalue()),
            b"<body>saved</body>",
        )

    def test_wayback_results_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seeds = root / "seeds.json"
            seeds.write_text(
                json.dumps(
                    {
                        "identities": [
                            {
                                "source": "fixture",
                                "handle": "StarFoxA",
                                "confidence": "confirmed",
                                "evidence": "fixture",
                                "profile_url": "https://example.com/starfoxa",
                            }
                        ],
                        "targets": [
                            {
                                "id": "fixture-profile",
                                "source": "fixture",
                                "kind": "profile",
                                "url": "https://example.com/starfoxa",
                                "attribution": "confirmed",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            db = scrape_source_sweep.connect(root / "sweep.sqlite3")
            scrape_source_sweep.load_seeds(db, seeds)
            rows = [
                {
                    "timestamp": "20080102030405",
                    "original": "https://example.com/starfoxa",
                    "statuscode": "200",
                    "mimetype": "text/html",
                    "digest": "fixture-digest",
                }
            ]
            with mock.patch.object(scrape_source_sweep, "wayback_query", return_value=rows):
                scrape_source_sweep.collect_wayback(db, delay=0)
            capture = db.execute(
                "SELECT target_id,timestamp,digest FROM captures"
            ).fetchone()
            check = db.execute(
                "SELECT result_count,error FROM index_checks WHERE provider='wayback'"
            ).fetchone()
            self.assertEqual(
                tuple(capture), ("fixture-profile", "20080102030405", "fixture-digest")
            )
            self.assertEqual(tuple(check), (1, None))
            with mock.patch.object(
                scrape_source_sweep, "request_bytes", return_value=b"<p>archived</p>"
            ) as request:
                scrape_source_sweep.download_wayback(db, root / "raw", delay=0)
            saved = db.execute(
                "SELECT raw_path FROM captures WHERE provider='wayback'"
            ).fetchone()[0]
            db.close()
            request.assert_called_once_with(
                "https://web.archive.org/web/20080102030405id_/"
                "https://example.com/starfoxa",
                timeout=60,
                retries=3,
            )
            with gzip.open(root / "raw" / saved, "rb") as archived:
                self.assertEqual(archived.read(), b"<p>archived</p>")


class GiantBombParserTests(unittest.TestCase):
    def test_profile_artifacts_and_reviews(self) -> None:
        source = """
        <a href="/profile/StarFoxA/hey-everyone/30-4615/">Hey, everyone</a>
        <a href="/profile/starfoxa/lists/every-game-ive-ever-finished/32782/">
          Every Game I've Ever Finished</a>
        <a href="/profile/starfoxa/sample_image/51-123/">Image</a>
        <div id="div_shout_review_3541">
          <table class="review"><tr><td class="va-t">
          <a href="/professor-layton/61-11865/"><img></a></td></tr></table>
          <span class="f-11 lh-8">July 27, 2008</span>
          <span class="f-14 bold">An excellent &amp; thoughtful game</span>
          <img src="/star-9.png"><div class="pb-20">First line.<br>Second line.</div>
        </div>
        """
        artifacts = scrape_giantbomb.parse_artifacts(source)
        self.assertEqual(
            {(row["kind"], row["legacy_id"]) for row in artifacts},
            {("blog", "4615"), ("list", "32782"), ("image", "123")},
        )
        self.assertEqual(
            next(row for row in artifacts if row["kind"] == "blog")["urls"],
            ["https://www.giantbomb.com/profile/starfoxa/hey-everyone/30-4615/"],
        )
        review = scrape_giantbomb.parse_reviews(source)[0]
        self.assertEqual(review["review_id"], "3541")
        self.assertEqual(review["reviewed_at"], "2008-07-27")
        self.assertEqual(review["rating"], 9)
        self.assertEqual(review["body"], "First line.\nSecond line.")

    def test_review_index_and_dedicated_page(self) -> None:
        index = """
        <a href="/burnout-paradise/61-5648/user-reviews/?review_id=8043">
          Welcome to Paradise City</a>
        (<span class="platform X360">X360</span>)
        <span class="author">Reviewed by
          <a href="/profile/starfoxa/">StarFoxA</a> on April 21, 2009</span>
        <img src="/icons/star-9.png">
        <a href="/burnout-paradise/3030-5648/user-reviews/2200-8043/">
          Welcome to Paradise City</a>
        """
        link = scrape_giantbomb.parse_review_links(index)[0]
        self.assertEqual(link["review_id"], "8043")
        self.assertEqual(link["rating"], 9)
        self.assertEqual(link["reviewed_at"], "2009-04-21")
        self.assertEqual(len(link["urls"]), 2)

        page = """
        <h1><a href="/burnout-paradise/3030-5648/" class="wiki-title">
          Burnout Paradise</a></h1>
        <h3 class="header-border"><a href="/profile/starfoxa/">starfoxa's</a>
          Burnout Paradise (Xbox 360) review</h3>
        <time datetime="2009-04-21T16:59:00-0800">April 21, 2009</time>
        Score: <span class="score score-5"></span>
        <article class="content-body"><h2>Welcome to Paradise City</h2>
          <div class="user-review-body"><p>First paragraph with enough text to
          make this a genuine recovered review body.</p><div><p>Nested second
          paragraph that must not truncate the body.</p></div></div>
        </article>
        """
        review = scrape_giantbomb.parse_review_page(
            page,
            "https://www.giantbomb.com/burnout-paradise/3030-5648/"
            "user-reviews/2200-8043/",
            "8043",
        )
        self.assertIsNotNone(review)
        self.assertEqual(review["review_id"], "8043")
        self.assertEqual(review["game_title"], "Burnout Paradise")
        self.assertEqual(review["rating"], 10)
        self.assertIn("Nested second", review["body"])


class BackloggdParserTests(unittest.TestCase):
    def test_review_and_profile_parsing(self) -> None:
        source = """
        <span id="bio-title">Bio</span>
        <span id="bio-body">Software engineer &amp; gamer.</span>
        <h1>1,606</h1></a><h4>Games Played</h4>
        <div class="row mb-1 game-name"><a href="/games/chrono-trigger/">
        <h3 class="mb-0">Chrono &amp; Trigger</h3></a>
        <p class="text-color-secondary game-date mb-0">1995</p>
        <div class="stars-top" style="width:80%"></div>
        <p class="mb-0 play-type completed">Completed</p>
        <a class="review-platform"><p>Nintendo DS</p></a>
        <time datetime="2012-10-05T00:00:00Z"></time>
        <div class="row review-body" review_id="24164">
        <div class="mb-0 card-text" id="collapseReview24164">First line.<br>Second line.</div>
        <a class="open-review-link" href="/u/starfoxa/review/24164/">Open review</a>
        """
        profile = scrape_backloggd.parse_profile(source)
        reviews = scrape_backloggd.parse_reviews(source, 1)
        self.assertEqual(profile["total_games"], 1606)
        self.assertIn("Software engineer", profile["bio"])
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["title"], "Chrono & Trigger")
        self.assertEqual(reviews[0]["rating"], 4)
        self.assertEqual(reviews[0]["body"], "First line.\nSecond line.")

    def test_recovered_review_is_imported_into_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "backloggd"
            source = scrape_backloggd.connect(source_root / "backloggd.sqlite3")
            source.execute(
                """INSERT INTO pages(page,url,review_count,fetched_at,error)
                   VALUES(1,'https://backloggd.com/u/starfoxa/reviews/',1,'now',NULL)"""
            )
            source.execute(
                """INSERT INTO reviews(
                       review_id,title,game_url,release_year,rating,play_status,
                       platform,reviewed_at,body,canonical_url,source_page,parsed_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "123", "Chrono Trigger", "https://backloggd.com/games/chrono-trigger/",
                    1995, 5.0, "Completed", "Super Nintendo",
                    "2021-02-05T18:14:57Z", "Still wonderful.",
                    "https://backloggd.com/u/starfoxa/review/123/", 1, "now",
                ),
            )
            source.commit()
            source.close()
            catalog = root / "library.sqlite3"
            build_library.build(root, catalog)
            result = sqlite3.connect(catalog)
            item = result.execute(
                "SELECT source,title,body FROM items WHERE id='backloggd:123'"
            ).fetchone()
            stats = result.execute(
                "SELECT label,item_count FROM source_stats WHERE source='backloggd'"
            ).fetchone()
            result.close()
            self.assertEqual(item, ("backloggd", "Chrono Trigger", "Still wonderful."))
            self.assertEqual(stats, ("Backloggd", 1))


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
        self.assertIn('class="skip-link"', home.text)
        self.assertIn("Archive register", home.text)
        self.assertIn('href="/static/archive.css"', home.text)
        self.assertNotIn('href="http://', home.text)
        self.assertEqual(home.headers["x-content-type-options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", home.headers["content-security-policy"])

        search = self.client.get("/search", params={"q": "searchable", "source": "fixture"})
        self.assertEqual(search.status_code, 200)
        self.assertIn("<strong>1</strong> result", search.text)
        self.assertIn('aria-current="page"', search.text)
        self.assertIn("All search words must match", search.text)

        api = self.client.get("/api/v1/search", params={"q": "Mario's"})
        self.assertEqual(api.status_code, 200)
        self.assertEqual(api.json()["total"], 1)
        self.assertEqual(api.json()["pages"], 1)
        self.assertEqual(api.json()["items"][0]["canonical_url"], "")

        item = self.client.get("/item/fixture/1")
        self.assertEqual(item.status_code, 200)
        self.assertIn("Earlier context", item.text)
        self.assertIn("Around this post", item.text)
        self.assertIn("Record details", item.text)
        self.assertNotIn("javascript:alert", item.text)

        gallery = self.client.get("/gallery")
        self.assertEqual(gallery.status_code, 200)
        self.assertIn("2010-02-03", gallery.text)
        self.assertIn("Linked record", gallery.text)

        asset = self.client.get("/asset/fixture:image")
        self.assertEqual(asset.status_code, 200)
        self.assertEqual(asset.content, b"a recovered image")
        self.assertEqual(asset.headers["content-type"], "image/png")

    def test_rejects_bad_pages_and_out_of_root_assets(self) -> None:
        bad_page = self.client.get("/search?page=2")
        self.assertEqual(bad_page.status_code, 404)
        self.assertIn("Record not found", bad_page.text)
        self.assertIn("Return home", bad_page.text)
        self.assertEqual(self.client.get("/gallery?page=2").status_code, 404)
        bad_api_page = self.client.get("/api/v1/search?page=2")
        self.assertEqual(bad_api_page.status_code, 404)
        self.assertEqual(bad_api_page.json(), {"detail": "page does not exist"})
        self.assertEqual(self.client.get("/asset/fixture:escape").status_code, 404)

    def test_health_reports_missing_catalog(self) -> None:
        archive_web.CATALOG = self.catalog.with_name("missing.sqlite3")
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "catalog-missing"})


if __name__ == "__main__":
    unittest.main()
