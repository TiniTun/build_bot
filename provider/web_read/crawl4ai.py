"""Crawl4AI provider for web page reading."""

import asyncio
from typing import TYPE_CHECKING

from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

from .base import WebReadProvider, ReadResult

if TYPE_CHECKING:
    from utils.config import Crawl4AIWebReadConfig

DEFAULT_PAGE_TIMEOUT_SECONDS = 20
DEFAULT_MAX_CONTENT_CHARS = 10_000

EXCLUDED_TAGS = [
    "script",
    "style",
    "noscript",
    "svg",
    "canvas",
    "video",
    "audio",
    "iframe",
    "form",
]


def _browser_config() -> BrowserConfig:
    return BrowserConfig(
        headless=True,
        text_mode=True,
        light_mode=True,
        memory_saving_mode=True,
        avoid_ads=True,
        avoid_css=True,
        viewport_width=1024,
        viewport_height=768,
        extra_args=[
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-images",
            "--blink-settings=imagesEnabled=false",
            "--disable-background-networking",
            "--disable-extensions",
            "--disable-sync",
            "--mute-audio",
            "--js-flags=--max-old-space-size=128",
        ],
    )


def _run_config(page_timeout_seconds: int) -> CrawlerRunConfig:
    return CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        wait_until="domcontentloaded",
        page_timeout=page_timeout_seconds * 1000,
        only_text=True,
        excluded_tags=EXCLUDED_TAGS,
        remove_forms=True,
        remove_overlay_elements=True,
        remove_consent_popups=True,
        scan_full_page=False,
        process_iframes=False,
        wait_for_images=False,
        exclude_external_images=True,
        exclude_all_images=True,
        verbose=False,
    )


def _truncate_content(content: str, max_chars: int) -> str:
    if len(content) <= max_chars:
        return content
    return f"{content[:max_chars]}\n\n[Truncated - original size: {len(content)} chars]"


class Crawl4AIProvider(WebReadProvider):
    """Web read provider using Crawl4AI."""

    def __init__(
        self,
        config: "Crawl4AIWebReadConfig | None" = None,
        *,
        max_content_chars: int | None = None,
    ):
        """Initialize Crawl4AI provider."""
        self.page_timeout_seconds = (
            config.page_timeout_seconds if config else DEFAULT_PAGE_TIMEOUT_SECONDS
        )
        self.max_content_chars = (
            max_content_chars
            if max_content_chars is not None
            else (config.max_content_chars if config else DEFAULT_MAX_CONTENT_CHARS)
        )

    async def read(self, url: str) -> ReadResult:
        """Read a web page using Crawl4AI."""
        try:
            browser_config = _browser_config()
            run_config = _run_config(self.page_timeout_seconds)
            async with AsyncWebCrawler(verbose=False, config=browser_config) as crawler:
                result = await asyncio.wait_for(
                    crawler.arun(url=url, config=run_config),
                    timeout=self.page_timeout_seconds + 5,
                )

                if not result.success:
                    raise Exception(result.error_message or "Failed to crawl page")

                content = str(result.markdown or "")
                return ReadResult(
                    url=url,
                    title=(result.metadata.get("title", "") if result.metadata else ""),
                    content=_truncate_content(content, self.max_content_chars),
                    error=None,
                )
        except Exception as e:
            return ReadResult(
                url=url,
                title="",
                content="",
                error=str(e),
            )
