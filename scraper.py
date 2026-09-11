import asyncio
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

from playwright.async_api import async_playwright


OUTPUT_DIR = Path("scraped_site")


def safe_filename(url: str, index: int) -> str:
    parsed = urlparse(url)

    name = Path(parsed.path).name

    if not name or not name.endswith(".css"):
        digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
        name = f"stylesheet_{digest}.css"

    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)

    return f"{index:03d}_{name}"


async def auto_scroll(page) -> None:
    """
    Scroll through the page so lazy-loaded images/content
    have a chance to render before we snapshot the DOM.
    """

    await page.evaluate(
        """
        async () => {
            await new Promise((resolve) => {
                let totalHeight = 0;
                const distance = 500;

                const timer = setInterval(() => {
                    const scrollHeight = document.body.scrollHeight;

                    window.scrollBy(0, distance);
                    totalHeight += distance;

                    if (totalHeight >= scrollHeight) {
                        clearInterval(timer);

                        window.scrollTo(0, 0);

                        setTimeout(resolve, 500);
                    }
                }, 100);
            });
        }
        """
    )


async def scrape(url: str) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    css_dir = OUTPUT_DIR / "css"
    css_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True
        )

        context = await browser.new_context(
            viewport={
                "width": 1440,
                "height": 1200,
            },
            user_agent=(
                "Mozilla/5.0 "
                "(Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0 Safari/537.36"
            ),
        )

        page = await context.new_page()

        print(f"[OPEN] {url}")

        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=90_000,
        )

        try:
            await page.wait_for_load_state(
                "networkidle",
                timeout=20_000,
            )
        except Exception:
            print("[INFO] Network never became fully idle, continuing.")

        # Let Shoptet/client scripts finish initialization.
        await page.wait_for_timeout(2000)

        print("[SCROLL] Triggering lazy-loaded content...")

        await auto_scroll(page)

        await page.wait_for_timeout(1500)

        final_url = page.url

        print(f"[FINAL URL] {final_url}")

        # ---------------------------------------------------------
        # Screenshot the ORIGINAL for visual comparison
        # ---------------------------------------------------------

        print("[SCREENSHOT] Saving original page...")

        await page.screenshot(
            path=str(OUTPUT_DIR / "original.png"),
            full_page=True,
        )

        # ---------------------------------------------------------
        # Get every stylesheet URL currently used by the browser
        # ---------------------------------------------------------

        stylesheet_result = await page.locator(
            'link[rel~="stylesheet"]'
        ).evaluate_all(
            """
            (links) => links
                .map(link => link.href)
                .filter(Boolean)
            """
        )

        stylesheet_urls = cast(
            list[str],
            stylesheet_result,
        )

        print(
            f"[CSS] Found {len(stylesheet_urls)} "
            "external stylesheets."
        )

        # ---------------------------------------------------------
        # Download CSS COPIES for inspection.
        #
        # IMPORTANT:
        # We do NOT replace the page's original CSS references.
        # The local clone will continue loading the real CSS URLs,
        # because that gives much higher visual fidelity.
        # ---------------------------------------------------------

        css_manifest: list[dict[str, str]] = []

        for index, css_url in enumerate(
            stylesheet_urls,
            start=1,
        ):
            filename = safe_filename(
                css_url,
                index,
            )

            output_path = css_dir / filename

            print(f"[CSS] {css_url}")

            try:
                response = await context.request.get(
                    css_url,
                    timeout=30_000,
                )

                if response.ok:
                    css_text = await response.text()

                    output_path.write_text(
                        css_text,
                        encoding="utf-8",
                    )

                    css_manifest.append(
                        {
                            "original_url": css_url,
                            "local_file": f"css/{filename}",
                        }
                    )

                else:
                    print(
                        f"      HTTP {response.status}"
                    )

            except Exception as exc:
                print(
                    f"      Could not download: {exc}"
                )

        # ---------------------------------------------------------
        # Save inline <style> blocks separately too
        # ---------------------------------------------------------

        inline_styles_result = await page.locator(
            "style"
        ).evaluate_all(
            """
            (styles) => styles.map(style => style.textContent || "")
            """
        )

        inline_styles = cast(
            list[str],
            inline_styles_result,
        )

        (css_dir / "inline-styles.css").write_text(
            "\n\n/* ---------- NEXT STYLE BLOCK ---------- */\n\n".join(
                inline_styles
            ),
            encoding="utf-8",
        )

        # ---------------------------------------------------------
        # Build snapshot of RENDERED DOM
        #
        # Critical difference from previous scraper:
        #
        # - we preserve classes
        # - preserve inline styles
        # - preserve stylesheets
        # - preserve rendered JS-created DOM
        # - make URLs absolute
        # - remove scripts only AFTER they already rendered page
        #
        # This keeps the snapshot visually stable.
        # ---------------------------------------------------------

        print("[HTML] Creating rendered DOM snapshot...")

        rendered_html_result = await page.evaluate(
            """
            () => {
                const clone = document.documentElement.cloneNode(true);

                const originalBase = document.baseURI;

                // ---------------------------------------------
                // Utility
                // ---------------------------------------------

                function absoluteURL(value) {
                    if (!value) {
                        return value;
                    }

                    const trimmed = value.trim();

                    if (
                        trimmed.startsWith("data:") ||
                        trimmed.startsWith("blob:") ||
                        trimmed.startsWith("javascript:") ||
                        trimmed.startsWith("mailto:") ||
                        trimmed.startsWith("tel:") ||
                        trimmed.startsWith("#")
                    ) {
                        return value;
                    }

                    try {
                        return new URL(
                            value,
                            originalBase
                        ).href;
                    } catch {
                        return value;
                    }
                }


                // ---------------------------------------------
                // Convert standard URL attributes
                // ---------------------------------------------

                const attributeMap = {
                    "a": ["href"],
                    "link": ["href"],
                    "img": ["src"],
                    "script": ["src"],
                    "iframe": ["src"],
                    "source": ["src"],
                    "video": ["src", "poster"],
                    "audio": ["src"],
                    "form": ["action"],
                    "input": ["src"]
                };

                for (
                    const [selector, attributes]
                    of Object.entries(attributeMap)
                ) {
                    clone
                        .querySelectorAll(selector)
                        .forEach(element => {

                            for (const attribute of attributes) {
                                if (
                                    element.hasAttribute(attribute)
                                ) {
                                    element.setAttribute(
                                        attribute,
                                        absoluteURL(
                                            element.getAttribute(attribute)
                                        )
                                    );
                                }
                            }
                        });
                }


                // ---------------------------------------------
                // srcset
                // ---------------------------------------------

                clone
                    .querySelectorAll("[srcset]")
                    .forEach(element => {

                        const srcset =
                            element.getAttribute("srcset");

                        if (!srcset) {
                            return;
                        }

                        const rewritten = srcset
                            .split(",")
                            .map(entry => {
                                const parts =
                                    entry.trim().split(/\\s+/);

                                if (!parts[0]) {
                                    return entry;
                                }

                                parts[0] =
                                    absoluteURL(parts[0]);

                                return parts.join(" ");
                            })
                            .join(", ");

                        element.setAttribute(
                            "srcset",
                            rewritten
                        );
                    });


                // ---------------------------------------------
                // CSS url(...) inside style=""
                // ---------------------------------------------

                clone
                    .querySelectorAll("[style]")
                    .forEach(element => {

                        let style =
                            element.getAttribute("style");

                        if (!style) {
                            return;
                        }

                        style = style.replace(
                            /url\\((['"]?)(.*?)\\1\\)/gi,
                            (whole, quote, rawUrl) => {

                                const absolute =
                                    absoluteURL(rawUrl);

                                return `url("${absolute}")`;
                            }
                        );

                        element.setAttribute(
                            "style",
                            style
                        );
                    });


                // ---------------------------------------------
                // Preserve current form values
                // ---------------------------------------------

                const originalInputs =
                    document.querySelectorAll(
                        "input, textarea, select"
                    );

                const clonedInputs =
                    clone.querySelectorAll(
                        "input, textarea, select"
                    );

                originalInputs.forEach(
                    (original, index) => {

                        const cloned =
                            clonedInputs[index];

                        if (!cloned) {
                            return;
                        }

                        if (
                            original instanceof HTMLInputElement
                        ) {
                            cloned.setAttribute(
                                "value",
                                original.value
                            );

                            if (original.checked) {
                                cloned.setAttribute(
                                    "checked",
                                    ""
                                );
                            }
                        }

                        if (
                            original instanceof HTMLTextAreaElement
                        ) {
                            cloned.textContent =
                                original.value;
                        }

                        if (
                            original instanceof HTMLSelectElement
                        ) {
                            const options =
                                cloned.querySelectorAll(
                                    "option"
                                );

                            options.forEach(
                                (option, optionIndex) => {

                                    if (
                                        optionIndex ===
                                        original.selectedIndex
                                    ) {
                                        option.setAttribute(
                                            "selected",
                                            ""
                                        );
                                    } else {
                                        option.removeAttribute(
                                            "selected"
                                        );
                                    }
                                }
                            );
                        }
                    }
                );


                // ---------------------------------------------
                // Scripts already did their job.
                //
                // Remove them from snapshot so opening local
                // page doesn't re-run Shoptet initialization
                // against localhost and destroy the layout.
                // ---------------------------------------------

                clone
                    .querySelectorAll("script")
                    .forEach(script => script.remove());


                // ---------------------------------------------
                // Add base tag as final fallback
                // ---------------------------------------------

                let head =
                    clone.querySelector("head");

                if (head) {
                    head
                        .querySelectorAll("base")
                        .forEach(base => base.remove());

                    const base =
                        document.createElement("base");

                    base.setAttribute(
                        "href",
                        originalBase
                    );

                    head.insertBefore(
                        base,
                        head.firstChild
                    );
                }


                return (
                    "<!DOCTYPE html>\\n" +
                    clone.outerHTML
                );
            }
            """
        )

        rendered_html = cast(
            str,
            rendered_html_result,
        )

        html_path = OUTPUT_DIR / "index.html"

        html_path.write_text(
            rendered_html,
            encoding="utf-8",
        )

        # ---------------------------------------------------------
        # Save metadata
        # ---------------------------------------------------------

        manifest = {
            "requested_url": url,
            "final_url": final_url,
            "stylesheets": css_manifest,
        }

        (OUTPUT_DIR / "manifest.json").write_text(
            json.dumps(
                manifest,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        await browser.close()

    print()
    print("=" * 60)
    print("DONE")
    print("=" * 60)
    print()
    print(f"HTML:       {OUTPUT_DIR / 'index.html'}")
    print(f"Original:   {OUTPUT_DIR / 'original.png'}")
    print(f"CSS copies: {OUTPUT_DIR / 'css'}")
    print(f"Manifest:   {OUTPUT_DIR / 'manifest.json'}")
    print()
    print("Preview:")
    print()
    print("    cd scraped_site")
    print("    python3 -m http.server 8000")
    print()
    print("Then open:")
    print()
    print("    http://localhost:8000")
    print()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(
            "Usage:\n\n"
            'python3 scraper.py "https://example.cz/"'
        )

        raise SystemExit(1)

    asyncio.run(
        scrape(sys.argv[1])
    )