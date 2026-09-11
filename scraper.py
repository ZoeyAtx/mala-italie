import asyncio
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright


OUTPUT_DIR = Path("scraped_site")


def make_absolute(base_url: str, value: str) -> str:
    if not value:
        return value

    value = value.strip()

    if value.startswith((
        "data:",
        "blob:",
        "javascript:",
        "mailto:",
        "tel:",
        "#"
    )):
        return value

    return urljoin(base_url, value)


def rewrite_css_urls(css: str, css_url: str) -> str:
    """
    Convert relative url(...) references inside CSS
    into absolute URLs.
    """

    def replace_url(match):
        quote = match.group(1) or ""
        raw_url = match.group(2).strip()

        if raw_url.startswith((
            "data:",
            "blob:",
            "http://",
            "https://",
            "//",
            "#"
        )):
            return match.group(0)

        absolute = urljoin(css_url, raw_url)

        return f"url({quote}{absolute}{quote})"

    return re.sub(
        r"url\(\s*(['\"]?)(.*?)\1\s*\)",
        replace_url,
        css,
        flags=re.IGNORECASE
    )


def download_css(url: str) -> str:
    try:
        response = requests.get(
            url,
            timeout=20,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 Chrome/120 Safari/537.36"
                )
            }
        )

        response.raise_for_status()

        css = response.text

        return rewrite_css_urls(css, url)

    except Exception as exc:
        print(f"[CSS ERROR] {url}")
        print(exc)
        return ""


async def scrape(url: str):
    OUTPUT_DIR.mkdir(exist_ok=True)

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True
        )

        page = await browser.new_page(
            viewport={
                "width": 1920,
                "height": 1080
            }
        )

        print(f"[OPEN] {url}")

        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000
        )

        # Give JS-heavy pages time to finish
        try:
            await page.wait_for_load_state(
                "networkidle",
                timeout=15000
            )
        except Exception:
            pass

        await page.wait_for_timeout(2000)

        final_url = page.url

        print(f"[FINAL URL] {final_url}")

        html = await page.content()

        soup = BeautifulSoup(html, "html.parser")

        # --------------------------------------------------
        # Rewrite HTML URLs
        # --------------------------------------------------

        url_attributes = {
            "img": ["src"],
            "script": ["src"],
            "iframe": ["src"],
            "source": ["src"],
            "video": ["src", "poster"],
            "audio": ["src"],
            "form": ["action"],
            "a": ["href"]
        }

        for tag_name, attributes in url_attributes.items():

            for tag in soup.find_all(tag_name):

                for attr in attributes:

                    if tag.has_attr(attr):

                        tag[attr] = make_absolute(
                            final_url,
                            tag[attr]
                        )

        # srcset
        for tag in soup.find_all(srcset=True):

            entries = []

            for entry in tag["srcset"].split(","):

                parts = entry.strip().split()

                if not parts:
                    continue

                parts[0] = make_absolute(
                    final_url,
                    parts[0]
                )

                entries.append(" ".join(parts))

            tag["srcset"] = ", ".join(entries)

        # --------------------------------------------------
        # Collect CSS
        # --------------------------------------------------

        all_css = []

        # Inline <style>
        for style_tag in soup.find_all("style"):

            if style_tag.string:

                all_css.append(
                    rewrite_css_urls(
                        style_tag.string,
                        final_url
                    )
                )

            # Remove because we'll inject everything later
            style_tag.decompose()

        # External stylesheets
        stylesheets = []

        for link in soup.find_all(
            "link",
            rel=lambda value: (
                value
                and "stylesheet" in value
            )
        ):

            href = link.get("href")

            if not href:
                continue

            css_url = urljoin(
                final_url,
                href
            )

            stylesheets.append(css_url)

            css = download_css(css_url)

            if css:
                all_css.append(
                    f"\n/* SOURCE: {css_url} */\n"
                    + css
                )

            link.decompose()

        # --------------------------------------------------
        # Rewrite inline style="" URLs
        # --------------------------------------------------

        for tag in soup.find_all(style=True):

            tag["style"] = rewrite_css_urls(
                tag["style"],
                final_url
            )

        # --------------------------------------------------
        # Save CSS
        # --------------------------------------------------

        css_file = OUTPUT_DIR / "styles.css"

        css_file.write_text(
            "\n\n".join(all_css),
            encoding="utf-8"
        )

        # --------------------------------------------------
        # Inject our combined CSS
        # --------------------------------------------------

        css_link = soup.new_tag(
            "link",
            rel="stylesheet",
            href="styles.css"
        )

        if soup.head:
            soup.head.append(css_link)

        else:
            head = soup.new_tag("head")
            head.append(css_link)

            if soup.html:
                soup.html.insert(0, head)

        # --------------------------------------------------
        # Insert <base>
        #
        # Makes any URL we didn't catch resolve against
        # the original website.
        # --------------------------------------------------

        if soup.head:

            base = soup.new_tag(
                "base",
                href=final_url
            )

            soup.head.insert(
                0,
                base
            )

        # --------------------------------------------------
        # Save HTML
        # --------------------------------------------------

        html_file = OUTPUT_DIR / "index.html"

        html_file.write_text(
            str(soup),
            encoding="utf-8"
        )

        # Screenshot for comparison
        await page.screenshot(
            path=str(
                OUTPUT_DIR / "original.png"
            ),
            full_page=True
        )

        await browser.close()

    print()
    print("DONE")
    print(f"HTML:       {html_file}")
    print(f"CSS:        {css_file}")
    print(
        f"Screenshot: "
        f"{OUTPUT_DIR / 'original.png'}"
    )

    print()
    print(f"Stylesheets found: {len(stylesheets)}")

    for stylesheet in stylesheets:
        print(f" - {stylesheet}")


if __name__ == "__main__":

    if len(sys.argv) < 2:

        print(
            "Usage:\n"
            "python3 scraper.py "
            "https://example.com/page"
        )

        sys.exit(1)

    target_url = sys.argv[1]

    asyncio.run(
        scrape(target_url)
    )