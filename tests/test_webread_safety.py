import unittest
from types import SimpleNamespace
from unittest.mock import patch

from provider.web_read.crawl4ai import Crawl4AIProvider


class _FakeCrawler:
    instances = []

    def __init__(self, *, verbose=False, config=None):
        self.verbose = verbose
        self.config = config
        self.arun_calls = []
        _FakeCrawler.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def arun(self, *, url, config=None):
        self.arun_calls.append((url, config))
        return SimpleNamespace(
            success=True,
            error_message=None,
            metadata={"title": "Example"},
            markdown="x" * 120,
        )


class Crawl4AIProviderSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_uses_lightweight_limited_crawler_config(self):
        _FakeCrawler.instances = []

        with patch("provider.web_read.crawl4ai.AsyncWebCrawler", _FakeCrawler):
            result = await Crawl4AIProvider(max_content_chars=100).read(
                "https://example.com"
            )

        self.assertIsNone(result.error)
        self.assertEqual(result.title, "Example")
        self.assertTrue(result.content.startswith("x" * 100))
        self.assertIn("[Truncated - original size: 120 chars]", result.content)

        crawler = _FakeCrawler.instances[0]
        self.assertTrue(crawler.config.text_mode)
        self.assertTrue(crawler.config.light_mode)
        self.assertTrue(crawler.config.memory_saving_mode)
        self.assertTrue(crawler.config.avoid_ads)
        self.assertTrue(crawler.config.avoid_css)

        _, run_config = crawler.arun_calls[0]
        self.assertEqual(run_config.page_timeout, 20000)
        self.assertEqual(run_config.wait_until, "domcontentloaded")
        self.assertTrue(run_config.only_text)
        self.assertFalse(run_config.scan_full_page)
        self.assertTrue(run_config.exclude_external_images)
        self.assertIn("script", run_config.excluded_tags)


if __name__ == "__main__":
    unittest.main()
