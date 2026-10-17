"""
ui_check.py - Automated UI Test Suite using Playwright.
Drives http://127.0.0.1:8000 with headless Chromium to verify:
(a) 4 1-click examples and their outcome badges.
(b) Ring A investigation, feedback confirmation, memory recall on o547.
(c) Multi-analyst tenant isolation via "Start as a new analyst".
(d) Memory, Detected Rings, and Join vs Graph (u193 @ 2 vs 12 hops).
(e) Browser console errors and failed network requests inspection.
(f) Mobile responsiveness at 390px viewport width.
Saves required full-page screenshots to docs/.
"""

import os
import subprocess
import sys
import time
import urllib.request
from playwright.sync_api import sync_playwright

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

DOCS_DIR = os.path.abspath("docs")
os.makedirs(DOCS_DIR, exist_ok=True)



def stop_port_8000():
    """Kill any leftover process listening on port 8000."""
    try:
        output = subprocess.check_output("netstat -ano | findstr :8000", shell=True).decode()
        for line in output.splitlines():
            parts = line.strip().split()
            if len(parts) >= 5 and ":8000" in parts[1]:
                pid = parts[-1]
                if pid and pid != "0":
                    subprocess.run(f"taskkill /F /PID {pid}", shell=True, capture_output=True)
        time.sleep(1)
    except Exception:
        pass


def wait_for_server(url="http://127.0.0.1:8000/api/health", timeout=15):
    """Poll health endpoint until server is ready."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def run_ui_checks():
    console_errors = []
    failed_requests = []

    print("=" * 60)
    print("STEP 2: UI VERIFICATION IN REAL BROWSER (PLAYWRIGHT)")
    print("=" * 60)

    # 1. Start server
    print("[1/7] Cleaning port 8000 and starting FastAPI server...")
    stop_port_8000()

    server_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api:app", "--host", "127.0.0.1", "--port", "8000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        if not wait_for_server():
            stdout, stderr = server_proc.communicate(timeout=2)
            raise RuntimeError(f"Server failed to start on 127.0.0.1:8000.\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}")
        print("[OK] Server is up and healthy on http://127.0.0.1:8000")

        # 2. Launch Playwright
        print("[2/7] Launching Playwright Chromium...")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()

            # Listen for console errors & failed requests
            def on_console(msg):
                if msg.type == "error":
                    console_errors.append(f"CONSOLE ERROR: {msg.text}")

            def on_request_failed(req):
                failed_requests.append(f"FAILED REQUEST: {req.method} {req.url} - {req.failure}")

            def on_response(res):
                if res.status >= 400:
                    failed_requests.append(f"HTTP {res.status}: {res.request.method} {res.url}")

            page.on("console", on_console)
            page.on("requestfailed", on_request_failed)
            page.on("response", on_response)

            print("[3/7] Navigating to http://127.0.0.1:8000 and configuring settings...")
            page.goto("http://127.0.0.1:8000", wait_until="networkidle")

            # Turn "Use Gemini" switch OFF
            page.evaluate("document.getElementById('useGeminiToggle').checked = false;")
            page.locator("#useGeminiToggle").uncheck(force=True)
            is_checked = page.evaluate("document.getElementById('useGeminiToggle').checked;")
            assert not is_checked, "Failed to toggle 'Use Gemini' switch OFF!"
            print("[OK] 'Use Gemini' switch set to OFF (deterministic template mode)")

            # Helper for waiting for investigation result
            def wait_investigation(timeout=15000):
                page.wait_for_selector("#btnInvestigate:not([disabled])", timeout=timeout)
                page.wait_for_selector("#resultCard:not(.hidden)", timeout=timeout)
                # Brief wait for Cytoscape DOM rendering
                page.wait_for_timeout(500)
                badge = page.locator("#outcomeBadge").inner_text().strip().upper()
                return badge

            # -------------------------------------------------------------
            # (a) Click each of the 4 examples and assert outcome badge
            # -------------------------------------------------------------
            print("[4/7] (a) Testing 4 1-click examples...")

            examples_to_test = [
                ("ring_a", "button[onclick=\"loadExample('ring_a')\"]", "PATH FOUND"),
                ("ring_c", "button[onclick=\"loadExample('ring_c')\"]", "PATH FOUND"),
                ("isolated", "button[onclick=\"loadExample('isolated')\"]", "NOT LINKED"),
                ("unknown", "button[onclick=\"loadExample('unknown')\"]", "UNKNOWN ENTITY"),
            ]

            for key, selector, expected_badge in examples_to_test:
                with page.expect_response(lambda r: "/api/investigate" in r.url and r.status == 200, timeout=15000):
                    page.click(selector)
                badge = wait_investigation()
                assert badge == expected_badge, f"Example '{key}' expected badge '{expected_badge}', got '{badge}'"
                print(f"  [OK] Example '{key}' -> outcome badge: {badge}")

            # -------------------------------------------------------------
            # (b) Ring A -> Confirm fraud -> investigate o547 -> answered_from_memory
            # -------------------------------------------------------------
            print("[5/7] (b) Investigating Ring A, confirming fraud, and testing memory recall on o547...")

            # Run Ring A again to establish baseline for this analyst
            with page.expect_response(lambda r: "/api/investigate" in r.url and r.status == 200, timeout=15000):
                page.click("button[onclick=\"loadExample('ring_a')\"]")
            badge_a = wait_investigation()
            assert badge_a == "PATH FOUND", f"Expected PATH FOUND for Ring A, got {badge_a}"

            # Verify template mode label and absence of rejected note on ring_a
            source_tag_a = page.locator("#sentenceSourceTag").inner_text().strip()
            assert source_tag_a == "template", f"Expected sentence source 'template', got '{source_tag_a}'"
            assert page.is_hidden("#sentenceRejectedNote"), "Sentence rejected note should be hidden in template mode"

            # Save docs/ring-a.png (Ring A result with graph visible at 1440x900 viewport, not full page)
            ring_a_path = os.path.join(DOCS_DIR, "ring-a.png")
            page.screenshot(path=ring_a_path, full_page=False)
            assert os.path.exists(ring_a_path) and os.path.getsize(ring_a_path) > 1000, "ring-a.png screenshot failed!"
            print(f"  [OK] Saved {ring_a_path} ({os.path.getsize(ring_a_path)} bytes)")

            # Click Confirm fraud
            with page.expect_response(lambda r: "/api/feedback" in r.url and r.status == 200, timeout=10000):
                page.click("#btnConfirmFraud")
            page.wait_for_selector("#infoBanner:not(.hidden)", timeout=5000)
            print("  [OK] Feedback recorded: CONFIRMED as fraud")

            # Investigate o547 (should recall the confirmed Ring A case)
            page.fill("#orderIdInput", "o547")
            page.evaluate("document.getElementById('useGeminiToggle').checked = false;")
            with page.expect_response(lambda r: "/api/investigate" in r.url and r.status == 200, timeout=15000):
                page.click("#btnInvestigate")
            badge_mem = wait_investigation()
            assert badge_mem == "ANSWERED FROM MEMORY", f"Expected ANSWERED FROM MEMORY for o547, got {badge_mem}"
            print(f"  [OK] Memory short-circuit confirmed on o547: outcome badge is {badge_mem}")

            # Check score equals sum of listed reasons
            score_disp = page.locator("#riskScoreDisplay").inner_text().strip()
            reasons_texts = page.locator("#reasonsList li").all_inner_texts()
            import re
            points = []
            for r in reasons_texts:
                m = re.search(r"\(([+-]?\d+)\)", r)
                if m:
                    points.append(int(m.group(1)))
            raw_sum = sum(points)
            if raw_sum > 100:
                expected_disp = f"100 (raw {raw_sum}, capped)"
            else:
                expected_disp = str(raw_sum)
            assert score_disp == expected_disp, f"Displayed score '{score_disp}' does not equal reasons sum '{expected_disp}'"
            print(f"  [OK] Displayed score '{score_disp}' equals sum of reasons ({raw_sum})")

            # Check non-traversal note
            assert any("full ring not re-traversed in memory mode" in r for r in reasons_texts), (
                f"Expected 'full ring not re-traversed in memory mode' in reasons: {reasons_texts}"
            )
            print("  [OK] Found 'full ring not re-traversed in memory mode' in reasons")

            # Check template label and no rejected note
            source_tag_mem = page.locator("#sentenceSourceTag").inner_text().strip()
            assert source_tag_mem in ("template", "written by Gemini", "memory"), f"Unexpected source tag: {source_tag_mem}"
            assert page.is_hidden("#sentenceRejectedNote"), "Sentence rejected note should be hidden in template mode"

            # Check date format in reasons or sentence (e.g. 17 Oct 2026, 08:26 UTC)
            sentence_text = page.locator("#sentenceText").inner_text().strip()
            date_pattern = r"\d{1,2} [A-Z][a-z]{2} \d{4}, \d{2}:\d{2} UTC"
            assert re.search(date_pattern, sentence_text) or any(re.search(date_pattern, r) for r in reasons_texts), (
                f"Expected date formatted as '17 Oct 2026, 08:26 UTC' in sentence/reasons. Sentence: {sentence_text}"
            )
            print("  [OK] Date formatting matches '17 Oct 2026, 08:26 UTC'")

            # Save docs/memory.png (1440x900 viewport screenshot, not full page)
            mem_path = os.path.join(DOCS_DIR, "memory.png")
            page.screenshot(path=mem_path, full_page=False)
            assert os.path.exists(mem_path) and os.path.getsize(mem_path) > 1000, "memory.png screenshot failed!"
            print(f"  [OK] Saved {mem_path} ({os.path.getsize(mem_path)} bytes)")

            # -------------------------------------------------------------
            # (c) "Start as a new analyst" -> investigate o547 -> assert no memory
            # -------------------------------------------------------------
            print("[6/7] (c) Starting as fresh analyst and testing tenant memory isolation on o547...")
            old_analyst = page.locator("#analystIdInput").input_value().strip()

            page.click("#btnNewAnalyst")
            page.wait_for_timeout(300)
            new_analyst = page.locator("#analystIdInput").input_value().strip()
            assert new_analyst != old_analyst, f"Analyst ID did not change after clicking new analyst! ({old_analyst})"
            print(f"  [OK] Switched from analyst '{old_analyst}' to '{new_analyst}'")

            # Investigate o547 with the fresh analyst
            page.fill("#orderIdInput", "o547")
            page.evaluate("document.getElementById('useGeminiToggle').checked = false;")
            with page.expect_response(lambda r: "/api/investigate" in r.url and r.status == 200, timeout=15000):
                page.click("#btnInvestigate")
            badge_isolated = wait_investigation()
            assert badge_isolated != "ANSWERED FROM MEMORY", f"Expected NO memory for new analyst, but got {badge_isolated}"
            assert badge_isolated == "PATH FOUND", f"Expected PATH FOUND for new analyst on o547, got {badge_isolated}"
            print(f"  [OK] Memory isolation verified: new analyst sees cold investigation '{badge_isolated}' (not ANSWERED FROM MEMORY)")

            # -------------------------------------------------------------
            # (d) Tabs: Memory, Detected Rings, Join vs Graph
            # -------------------------------------------------------------
            print("[7/7] (d) Testing tabs and Join vs Graph hop counts for u193...")

            # Memory Tab
            with page.expect_response(lambda r: "/api/memory/" in r.url and r.status == 200, timeout=10000):
                page.click("#tabBtnMemory")
            page.wait_for_selector("#tabMemory:not(.hidden)", timeout=5000)
            print("  [OK] Memory tab loaded successfully")

            # Detected Rings Tab (asserting bug fix for tools.find_rings min_users and array members)
            with page.expect_response(lambda r: "/api/rings" in r.url and r.status == 200, timeout=15000) as rings_resp_info:
                page.click("#tabBtnRings")
            rings_data = rings_resp_info.value.json()
            assert rings_data["count"] >= 3, f"Expected >= 3 detected rings, got {rings_data['count']}"
            assert isinstance(rings_data["rings"][0]["members"], list), "Expected ring members to be a list"
            assert rings_data["rings"][0]["size"] == len(rings_data["rings"][0]["members"]), "Expected size to equal members count"
            page.wait_for_selector("#tabRings:not(.hidden)", timeout=5000)
            page.wait_for_selector("#detectedRingsContainer div", timeout=10000)
            print("  [OK] Detected rings (WCC) tab loaded successfully (count >= 3 verified)")

            # Join vs Graph Tab
            with page.expect_response(lambda r: "/api/join-vs-graph" in r.url and r.status == 200, timeout=10000):
                page.click("#tabBtnJoin")
            page.wait_for_selector("#tabJoin:not(.hidden)", timeout=5000)

            # Ensure user is u193
            page.fill("#joinUserSelect", "u193")

            # Set slider to 2 hops
            with page.expect_response(lambda r: "/api/join-vs-graph" in r.url and "max_hops=2" in r.url and r.status == 200, timeout=10000):
                page.evaluate("""
                    const slider = document.getElementById('maxHopsSlider');
                    slider.value = '2';
                    slider.dispatchEvent(new Event('input'));
                """)
            page.wait_for_timeout(300)
            count_2 = page.locator("#graphCountDisplay").inner_text().strip()
            assert count_2 == "1", f"Expected graph count 1 for 2 hops, got '{count_2}'"
            print("  [OK] Join vs graph @ 2 hops -> graphCountDisplay: 1")

            # Set slider to 12 hops
            with page.expect_response(lambda r: "/api/join-vs-graph" in r.url and "max_hops=12" in r.url and r.status == 200, timeout=10000):
                page.evaluate("""
                    const slider = document.getElementById('maxHopsSlider');
                    slider.value = '12';
                    slider.dispatchEvent(new Event('input'));
                """)
            page.wait_for_timeout(500)
            count_12 = page.locator("#graphCountDisplay").inner_text().strip()
            assert count_12 == "6", f"Expected graph count 6 for 12 hops, got '{count_12}'"
            print("  [OK] Join vs graph @ 12 hops -> graphCountDisplay: 6")

            # Save docs/ring-c.png (Join vs graph at 12 hops for u193, 1440x900 viewport, not full page)
            ring_c_path = os.path.join(DOCS_DIR, "ring-c.png")
            page.screenshot(path=ring_c_path, full_page=False)
            assert os.path.exists(ring_c_path) and os.path.getsize(ring_c_path) > 1000, "ring-c.png screenshot failed!"
            print(f"  [OK] Saved {ring_c_path} ({os.path.getsize(ring_c_path)} bytes)")

            # -------------------------------------------------------------
            # (f) Mobile viewport check at 390px (390x844 viewport, not full page)
            # -------------------------------------------------------------
            print("  Testing mobile viewport (390px)...")
            page.set_viewport_size({"width": 390, "height": 844})
            page.click("#tabBtnInvestigate")
            page.wait_for_selector("#tabInvestigate:not(.hidden)", timeout=5000)

            page.evaluate("document.getElementById('useGeminiToggle').checked = false;")
            with page.expect_response(lambda r: "/api/investigate" in r.url and r.status == 200, timeout=15000):
                page.click("button[onclick=\"loadExample('ring_a')\"]")
            badge_mobile = wait_investigation()
            assert badge_mobile == "PATH FOUND", f"Expected PATH FOUND in mobile view, got {badge_mobile}"

            # Verify template mode label and absence of rejected note on mobile
            source_tag_mob = page.locator("#sentenceSourceTag").inner_text().strip()
            assert source_tag_mob == "template", f"Expected sentence source 'template', got '{source_tag_mob}'"
            assert page.is_hidden("#sentenceRejectedNote"), "Sentence rejected note should be hidden in template mode"

            # Save docs/mobile.png (390x844 viewport screenshot, not full page)
            mobile_path = os.path.join(DOCS_DIR, "mobile.png")
            page.screenshot(path=mobile_path, full_page=False)
            assert os.path.exists(mobile_path) and os.path.getsize(mobile_path) > 1000, "mobile.png screenshot failed!"
            print(f"  [OK] Saved {mobile_path} ({os.path.getsize(mobile_path)} bytes)")

            # -------------------------------------------------------------
            # (e) Console errors & failed requests report
            # -------------------------------------------------------------
            print("=" * 60)
            print("BROWSER CONSOLE ERRORS & NETWORK REQUESTS AUDIT")
            print("=" * 60)
            if console_errors:
                print(f"Found {len(console_errors)} console error(s):")
                for err in console_errors:
                    print(f"  [ERROR] {err}")
            else:
                print("[OK] Zero browser console errors recorded.")

            if failed_requests:
                print(f"Found {len(failed_requests)} failed network request(s):")
                for req in failed_requests:
                    print(f"  [FAILED REQUEST] {req}")
            else:
                print("[OK] Zero failed network requests recorded.")

            context.close()
            browser.close()

    finally:
        print("\nStopping FastAPI server process...")
        if server_proc:
            try:
                server_proc.terminate()
                server_proc.wait(timeout=3)
            except Exception:
                try:
                    server_proc.kill()
                except Exception:
                    pass
        stop_port_8000()
        print("[OK] Server stopped cleanly.")


if __name__ == "__main__":
    run_ui_checks()
