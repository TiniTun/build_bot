"""Crawl4AI provider for web page reading."""

from crawl4ai import AsyncWebCrawler, BrowserConfig

from .base import WebReadProvider, ReadResult


browser_config = BrowserConfig(
    headless=True,
    extra_args=[
        "--disable-dev-shm-usage",
        "--disable-gpu", 
        "--no-sandbox",
        "--disable-images",
        "--blink-settings=imagesEnabled=false",
        "--js-flags=--max-old-space-size=256",
    ]
)


class Crawl4AIProvider(WebReadProvider):
    """Web read provider using Crawl4AI."""

    def __init__(self):
        """Initialize Crawl4AI provider."""
        pass

    async def read(self, url: str) -> ReadResult:
        """Read a web page using Crawl4AI."""
        try:
            async with AsyncWebCrawler(verbose=False, config=browser_config) as crawler:
                result = await crawler.arun(url=url)

                if not result.success:
                    raise Exception(result.error_message or "Failed to crawl page")

                return ReadResult(
                    url=url,
                    title=(result.metadata.get("title", "") if result.metadata else ""),
                    content=result.markdowm or "",
                    error=None,
                )
        except Exception as e:
            return ReadResult(
                url=url,
                title="",
                content="",
                error=str(e),
            )