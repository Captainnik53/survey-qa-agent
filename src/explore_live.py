"""One-off: open the live survey and dump the first page's structure to design the runner."""
import sys
from playwright.sync_api import sync_playwright

URL = "https://singapore.decipherinc.com/survey/selfserve/9c7/2510702"


def dump(page, tag):
    print(f"\n===== {tag} =====")
    print("URL:", page.url)
    print("TITLE:", page.title())
    # Decipher wraps each question; try common selectors.
    for sel in ["div.question", ".survey_question", "[id^=q_]", "fieldset", "form"]:
        n = page.locator(sel).count()
        if n:
            print(f"selector {sel!r}: {n}")
    # visible text of the main content
    body = page.locator("body").inner_text()
    print("---- BODY TEXT (first 1500) ----")
    print(body[:1500])
    # inputs
    print("---- INPUTS ----")
    for typ in ["radio", "checkbox", "text", "number"]:
        print(f"  input[type={typ}]:", page.locator(f"input[type={typ}]").count())
    print("  select:", page.locator("select").count())
    print("  textarea:", page.locator("textarea").count())
    # next / continue buttons
    print("---- BUTTONS ----")
    for b in page.locator("button, input[type=submit], a.btn, .next").all()[:10]:
        try:
            print("  btn:", repr(b.inner_text() or b.get_attribute("value")), "| name=",
                  b.get_attribute("name"), "id=", b.get_attribute("id"))
        except Exception:
            pass


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 1200})
    page.goto(URL, wait_until="networkidle", timeout=60000)
    dump(page, "PAGE 1")
    page.screenshot(path="output/explore_page1.png", full_page=True)
    # dump raw HTML of the question area for structure
    html = page.content()
    open("output/explore_page1.html", "w").write(html)
    print("\nsaved output/explore_page1.png and .html  (html chars:", len(html), ")")
    browser.close()
