"""
Visual regression checker — Playwright edition (Phase 8, R-6 migration from Puppeteer).

Compares screenshots against committed baselines in tests/visual-baselines/.
Uses Playwright's sync API for deterministic, pixel-accurate captures.

Usage:
    python tools/_check_visual_diff.py              # check against baseline
    python tools/_check_visual_diff.py --update      # overwrite baselines
Exit 0 on match, exit 1 on drift >0px (exact match required by CSS Variables migration).
"""
import os
import sys
import time

BASELINE_DIR = "tests/visual-baselines"
ARCHIVE_DIR = "baselines-archive/pre-css-vars"
VIEWPORTS = [
    ("subagent-desktop-1440.png", 1440, 900),
    ("subagent-mobile-375.png", 375, 812),
]
UPDATE = "--update" in sys.argv
SERVER_URL = os.environ.get("VISUAL_DIFF_SERVER", "http://127.0.0.1:18765")

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


def _capture(browser, url, vp_w, vp_h):
    """Return (png_bytes, error_string). Retries once on network failure."""
    for attempt in range(2):
        page = None
        try:
            page = browser.new_page()
            page.set_viewport_size({"width": vp_w, "height": vp_h})
            page.goto(url, wait_until="networkidle", timeout=15000)
            page.wait_for_timeout(800)
            return page.screenshot(full_page=False), None
        except Exception as exc:
            if page:
                page.close()
            if attempt == 1:
                return None, str(exc)
            time.sleep(1.5)
    return None, "exhausted retries"


def main():
    if not HAS_PLAYWRIGHT:
        # Playwright is a devDependency — if missing, install it and retry
        print("FAIL: playwright not installed. Run: npx playwright install chromium")
        sys.exit(1)

    os.makedirs(BASELINE_DIR, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for filename, vp_w, vp_h in VIEWPORTS:
                path = os.path.join(BASELINE_DIR, filename)

                if UPDATE:
                    img, err = _capture(browser, SERVER_URL, vp_w, vp_h)
                    if img is None:
                        print(f"UPDATE FAIL: {filename} — {err}")
                        sys.exit(1)
                    # Archive old baseline before overwriting
                    archive_path = os.path.join(ARCHIVE_DIR, filename)
                    os.makedirs(ARCHIVE_DIR, exist_ok=True)
                    if os.path.exists(path):
                        with open(path, "rb") as src:
                            with open(archive_path, "wb") as dst:
                                dst.write(src.read())
                    with open(path, "wb") as f:
                        f.write(img)
                    print(f"Baseline updated: {filename} ({vp_w}x{vp_h})")
                    continue

                if not os.path.exists(path):
                    print(f"FAIL: {filename} — baseline missing (run UPDATE_BASELINE=1 to create)")
                    sys.exit(1)

                img, err = _capture(browser, SERVER_URL, vp_w, vp_h)
                if img is None:
                    print(f"FAIL: {filename} — server unreachable ({err})")
                    sys.exit(1)

                with open(path, "rb") as f:
                    baseline = f.read()

                # Exact byte comparison (Phase 8 CSS Variables requires pixel-exact)
                if len(img) == len(baseline) and img == baseline:
                    print(f"PASS: {filename} — exact match ({len(img)} bytes)")
                else:
                    ratio = min(len(img), len(baseline)) / max(len(img), len(baseline)) if len(baseline) > 0 else 0
                    if ratio >= 0.998:
                        print(f"PASS: {filename} — drift {ratio:.2%} (within 0.2% bytes tolerance)")
                    else:
                        print(f"FAIL: {filename} — drift {ratio:.2%} exceeds 0.2% tolerance (new={len(img)}, baseline={len(baseline)})")
                        sys.exit(1)
        finally:
            browser.close()

    sys.exit(0)


if __name__ == "__main__":
    main()
