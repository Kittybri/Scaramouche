"""Offline tests for the read-only research adapter."""
import unittest
from research_sources import (
    ResearchError, _ArticleText, parse_feed, parse_repo_spec,
    validate_public_url, video_id_from_url,
)


class ResearchParsingTests(unittest.TestCase):
    def test_reject_private_targets(self):
        blocked = [
            "http://example.com", "https://localhost/private",
            "https://127.0.0.1/", "https://169.254.169.254/latest",
            "https://10.0.0.4/", "https://example.com:8080/",
            "https://user:password@example.com/",
            "https://printer.local/", "file:///etc/passwd",
            "https://[::1]/",
            "https://example.org/\n@everyone",
            "https://example.org/\rX-Test:evil",
        ]
        for url in blocked:
            with self.subTest(url=url), self.assertRaises(ResearchError):
                validate_public_url(url)

    def test_allow_public_https(self):
        self.assertEqual(validate_public_url("https://example.org/post"), "https://example.org/post")

    def test_youtube_id(self):
        ident = "dQw4w9WgXcQ"
        self.assertEqual(video_id_from_url("https://youtu.be/" + ident), ident)
        self.assertEqual(video_id_from_url("https://www.youtube.com/watch?v=" + ident + "&t=5"), ident)
        self.assertEqual(video_id_from_url("https://youtube.com/shorts/" + ident), ident)
        for other in ["https://evil.youtube.com/watch?v=" + ident, "https://youtube.com/watch?v=abc", "not-a-url"]:
            with self.subTest(other=other), self.assertRaises(ResearchError):
                video_id_from_url(other)

    def test_github_repo_file_spec(self):
        self.assertEqual(parse_repo_spec("octocat/hello-world:README.md"), ("octocat/hello-world", "README.md"))
        for other in ["other", "./repo", "../repo", "octocat/repo:../config", "octocat/repo:/abs", "octocat/repo:dir//file"]:
            with self.subTest(other=other), self.assertRaises(ResearchError):
                parse_repo_spec(other)

    def test_atom_rss(self):
        rss = b"<rss><channel><item><title>Hello</title><link>https://example.org/a</link><guid>a</guid></item></channel></rss>"
        self.assertEqual(parse_feed(rss)[0]["id"], "a")
        atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>New</title><id>id1</id><link href="https://example.org/a"/></entry></feed>'
        self.assertEqual(parse_feed(atom)[0]["title"], "New")

    def test_reject_xml_entities(self):
        malicious = b'<!DOCTYPE x [<!ENTITY exploit SYSTEM "file:///etc/passwd">]><rss><item><title>&exploit;</title></item></rss>'
        with self.assertRaises(ResearchError):
            parse_feed(malicious)

    def test_ignore_scripts(self):
        p = _ArticleText()
        p.feed("<html><body><nav>MENU</nav><article><h1>Title</h1><p>Readable</p><script>EVIL</script></article></body></html>")
        text = "".join(p.parts)
        self.assertIn("Readable", text)
        self.assertNotIn("EVIL", text)
        self.assertNotIn("MENU", text)




class NaturalResearchRoutingTests(unittest.TestCase):
    def test_explicit_video_request(self):
        from natural_research import detect_research_request
        link = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        self.assertEqual(detect_research_request("Wanderer, explain " + link), ("youtube", link))

    def test_web_and_github(self):
        from natural_research import detect_research_request
        self.assertEqual(detect_research_request("Scara, read https://example.org/article"),
                         ("web", "https://example.org/article"))
        self.assertEqual(detect_research_request("Wanderer, review repo octocat/Hello-World"),
                         ("github", "octocat/Hello-World"))
        self.assertEqual(detect_research_request("Wanderer, check https://github.com/octocat/Hello-World"),
                         ("github", "octocat/Hello-World"))

    def test_search_query(self):
        from natural_research import detect_research_request
        self.assertEqual(detect_research_request("Scara, search for Genshin patch notes"),
                         ("search", "Genshin patch notes"))

    def test_avoid_unrequested_or_unsafe_routing(self):
        from natural_research import detect_research_request
        for phrase in (
            "I watched https://youtube.com/watch?v=dQw4w9WgXcQ",
            "do not summarize https://example.org",
            "!wanresearch web https://example.org",
            "Scara, read https://127.0.0.1/secrets",
            "Wanderer, review https://github.com/../secret",
            "I am jealous of the bot",
        ):
            with self.subTest(phrase=phrase):
                self.assertIsNone(detect_research_request(phrase))

    def test_addressing(self):
        from natural_research import is_named_directly
        self.assertTrue(is_named_directly("Wanderer, explain this URL", "wanderer"))
        self.assertTrue(is_named_directly("hey Scara, research this", "scaramouche"))
        self.assertFalse(is_named_directly("Scaramouche said something about Wanderer", "wanderer"))
        self.assertFalse(is_named_directly("Wanderer, do something", "scaramouche"))

if __name__ == "__main__":
    unittest.main()
