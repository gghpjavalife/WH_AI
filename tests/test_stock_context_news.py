import unittest

from services.stock_context_news import parse_news_rss


class NewsParserTests(unittest.TestCase):
    def test_rss_and_atom_entries(self):
        rss = """<rss><channel><item><title>A</title><link>https://example.com/a</link>
        <pubDate>Mon, 05 Jan 2026 10:00:00 GMT</pubDate></item>
        <item><title>B</title></item></channel></rss>"""
        result = parse_news_rss(rss, source="Example")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["timestamp"], "2026-01-05T10:00:00+00:00")
        self.assertIsNone(result[1]["raw_sentiment_score"])
        atom = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>C</title>
        <link href="https://example.com/c"/><published>2026-01-05T10:00:00Z</published></entry></feed>"""
        self.assertEqual(parse_news_rss(atom, source="Example")[0]["url"], "https://example.com/c")
        with self.assertRaises(ValueError):
            parse_news_rss("<rss><channel><item><pubDate>bad</pubDate></item></channel></rss>", source="Example")


if __name__ == "__main__":
    unittest.main()
