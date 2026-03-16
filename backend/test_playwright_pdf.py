from playwright.sync_api import sync_playwright
import os

html_content = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <script type="module">
    import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
    mermaid.initialize({ startOnLoad: true, securityLevel: "loose", theme: "default" });
  </script>
</head>
<body>
  <h1>Test PDF</h1>
  <pre class="mermaid diagram">
flowchart LR
    A["Test"] --> B["PDF"]
  </pre>
</body>
</html>
"""

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html_content)
        # Wait for SVG to be added by mermaid
        page.wait_for_selector('svg')
        page.pdf(path="test_out.pdf")
        browser.close()
        print("PDF generated successfully.")

run()
