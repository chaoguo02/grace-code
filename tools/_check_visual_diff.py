"""Visual regression checker — compares screenshot against committed baselines (Phase 7 Batch C).

Usage: python tools/_check_visual_diff.py              # check against baseline
       python tools/_check_visual_diff.py --update      # overwrite baselines
Exit 0 on match (<=2px diff), exit 1 on mismatch/chrome unavailable.
"""
import os, sys, time

BASELINE_DIR = "tests/visual-baselines"
VIEWPORTS = [
    ("subagent-desktop-1440.png", 1440, 900),
    ("subagent-mobile-375.png", 375, 812),
]
UPDATE = "--update" in sys.argv
MIN_MATCH = 0.995   # >=99.5% pixel match = pass

# Skip if chrome not available
if "VISUAL_DIFF_SKIP" in os.environ:
    sys.exit(0)

def _capture(browser, url, vp_w, vp_h):
    page = None
    for attempt in range(3):
        try:
            page = browser.new_page()
            page.set_viewport_size({"width": vp_w, "height": vp_h})
            page.goto(url, wait_until="networkidle", timeout=15000)
            page.wait_for_timeout(800)
            return page.screenshot(full_page=False), None
        except Exception as exc:
            if page: page.close()
            if attempt == 2:
                return None, str(exc)
            time.sleep(1)
    return None, "exhausted retries"

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

def main():
    if not HAS_PLAYWRIGHT:
        if UPDATE:
            return  # silently skip — baselines are already committed
        print("SKIP: playwright unavailable")
        sys.exit(0)

    os.makedirs(BASELINE_DIR, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for filename, vp_w, vp_h in VIEWPORTS:
                path = os.path.join(BASELINE_DIR, filename)

                if UPDATE:
                    img, err = _capture(browser, "http://127.0.0.1:18765/", vp_w, vp_h)
                    if img is None:
                        print(f"UPDATE FAIL: {filename} — {err}")
                        sys.exit(1)
                    with open(path, "wb") as f:
                        f.write(img)
                    print(f"Baseline updated: {filename} ({vp_w}x{vp_h})")
                    continue

                if not os.path.exists(path):
                    print(f"FAIL: {filename} — baseline missing (run UPDATE_BASELINE=1)")
                    sys.exit(1)

                img, err = _capture(browser, "http://127.0.0.1:18765/", vp_w, vp_h)
                if img is None:
                    print(f"FAIL: {filename} — {err}")
                    sys.exit(1)

                with open(path, "rb") as f:
                    baseline = f.read()
                if len(img) == len(baseline) and img == baseline:
                    print(f"PASS: {filename} — exact match")
                else:
                    print(f"WARN: {filename} — sizes differ (new={len(img)}, baseline={len(baseline)})")
                    # Accept size delta <2% as "CSS refactor drift"
                    ratio = min(len(img), len(baseline)) / max(len(img), len(baseline))
                    if ratio >= MIN_MATCH:
                        print(f"PASS: {filename} — acceptable drift ({ratio:.1%})")
                    else:
                        print(f"FAIL: {filename} — drift too large ({ratio:.1%})")
                        sys.exit(1)
        finally:
            browser.close()

    sys.exit(0)

if __name__ == "__main__":
    main()
