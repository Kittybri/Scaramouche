"""Offline checks for search-result parsing, against saved DuckDuckGo / Wikipedia response shapes."""

import logging
import unittest

import grounded_search as search


def setUpModule():
    logging.disable(logging.WARNING)  # the fallback tests deliberately provoke warnings


def tearDownModule():
    logging.disable(logging.NOTSET)


HTML_RESULTS = '''<div id="links" class="results">
<div class="result results_links results_links_deep web-result ">
  <div class="links_main links_deep result__body">
    <h2 class="result__title">
      <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fgenshin.fandom.com%2Fwiki%2FScaramouche_%28disambiguation%29&amp;rut=abc">Scaramouche | <b>Genshin</b> Impact Wiki</a>
    </h2>
    <div class="result__extras"><a class="result__url" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fgenshin.fandom.com">genshin.fandom.com</a></div>
    <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fgenshin.fandom.com">The Sixth of the <b>Eleven Fatui Harbingers</b> &amp; more.</a>
  </div>
</div>
<div class="result results_links web-result result--ad">
  <h2 class="result__title"><a rel="nofollow" class="result__a" href="//duckduckgo.com/y.js?ad_provider=x">An advert</a></h2>
  <a class="result__snippet" href="//duckduckgo.com/y.js?x">buy now</a>
</div>
<div class="result"><h2 class="result__title"><a class="result__a" href="https://example.org/direct">Direct link</a></h2><a class="result__snippet" href="x">No redirect.</a></div>
<div class="result"><h2 class="result__title"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fthird.example%2F">Third</a></h2><a class="result__snippet" href="x">third snippet</a></div>
</div>'''

LITE_RESULTS = '''<table><tr><td valign="top">1.&nbsp;</td><td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2F&amp;rut=1" class='result-link'>Example Domain</a></td></tr>
<tr><td>&nbsp;</td><td class='result-snippet'>This domain is for use in <b>illustrative</b> examples.</td></tr>
<tr><td valign="top">2.&nbsp;</td><td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.net%2F" class='result-link'>Second</a></td></tr>
<tr><td></td><td class='result-snippet'>Second snippet.</td></tr></table>'''


class ParserTests(unittest.TestCase):
    def test_html_results_are_unwrapped_and_ads_skipped(self):
        results = search.parse_ddg(HTML_RESULTS, 5)
        self.assertEqual([r["url"] for r in results], [
            "https://genshin.fandom.com/wiki/Scaramouche_(disambiguation)", "https://example.org/direct", "https://third.example/"])
        self.assertEqual(results[0]["title"], "Scaramouche | Genshin Impact Wiki")
        self.assertEqual(results[0]["snippet"], "The Sixth of the Eleven Fatui Harbingers & more.")
        self.assertEqual(results[1]["snippet"], "No redirect.")

    def test_max_results_is_respected(self):
        self.assertEqual(len(search.parse_ddg(HTML_RESULTS, 2)), 2)

    def test_lite_results(self):
        results = search.parse_ddg(LITE_RESULTS, 5)
        self.assertEqual([(r["title"], r["url"], r["snippet"]) for r in results], [
            ("Example Domain", "https://example.com/", "This domain is for use in illustrative examples."),
            ("Second", "https://example.net/", "Second snippet."),
        ])

    def test_unwrap_rejects_unsafe_links(self):
        self.assertEqual(search.unwrap_ddg_url("javascript:alert(1)"), "")
        self.assertEqual(search.unwrap_ddg_url("//duckduckgo.com/l/?uddg=ftp%3A%2F%2Fx"), "")
        self.assertEqual(search.unwrap_ddg_url("//duckduckgo.com/y.js?x=1"), "")

    def test_bot_check_page_is_recognised(self):
        self.assertTrue(search.looks_blocked('<div class="anomaly-modal__modal">Unfortunately, bots use DuckDuckGo too.</div>'))
        self.assertFalse(search.looks_blocked(HTML_RESULTS))

    def test_hostile_pages_cannot_stall_the_parser(self):
        import time

        pages = [
            "<a class='x' " * 33_000,
            '<div class="result__snippet">' * 14_000,
            "<a " * 130_000,
            '<a class="result__a" href="x">' * 13_000,
            "<" * 400_000,
            "<p>hello world</p>" * 170_000,
        ]
        started = time.perf_counter()
        for page in pages:
            self.assertEqual(search.parse_ddg(page, 5), [])
        self.assertLess(time.perf_counter() - started, 3.0)

    def test_formatting(self):
        results = search.parse_ddg(HTML_RESULTS, 2)
        self.assertTrue(search.format_search_context(results).startswith("[1] Scaramouche | Genshin Impact Wiki | https://genshin"))
        sources = search.format_search_sources(results)
        self.assertIn("<https://example.org/direct>", sources)  # angle brackets stop Discord unfurling every link
        self.assertEqual(search.format_search_context([]), "")


import importlib.util  # noqa: E402

HAVE_AIOHTTP = importlib.util.find_spec("aiohttp") is not None


@unittest.skipUnless(HAVE_AIOHTTP, "aiohttp is not installed")
class SearchTests(unittest.IsolatedAsyncioTestCase):
    """search_web against a local server standing in for DuckDuckGo."""

    async def asyncSetUp(self):
        from aiohttp import web
        from aiohttp.test_utils import TestServer

        self.hits = []
        self.mode = {"html": HTML_RESULTS}

        async def html(request):
            self.hits.append("html")
            body = self.mode["html"]
            return web.Response(text=body) if body is not None else web.Response(status=503)

        app = web.Application()
        app.router.add_get("/html/", html)
        self.server = TestServer(app)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        original = search.DDG_HTML_URL
        search.DDG_HTML_URL = f"http://{self.server.host}:{self.server.port}/html/"
        self.addCleanup(setattr, search, "DDG_HTML_URL", original)

    async def test_results_are_unwrapped(self):
        results = await search.search_web("scaramouche genshin", 3)
        self.assertEqual(self.hits, ["html"])
        self.assertEqual(results[0]["url"], "https://genshin.fandom.com/wiki/Scaramouche_(disambiguation)")

    async def test_a_bot_check_page_gives_no_results(self):
        self.mode["html"] = '<div class="anomaly-modal">Unfortunately, bots use DuckDuckGo too.</div>'
        self.assertEqual(await search.search_web("example domain"), [])

    async def test_failure_returns_nothing_instead_of_raising(self):
        self.mode["html"] = None
        self.assertEqual(await search.search_web("nothing at all"), [])
        self.assertEqual(await search.search_web("   "), [])


if __name__ == "__main__":
    unittest.main()
