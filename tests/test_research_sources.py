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


if __name__ == "__main__":
    unittest.main()
