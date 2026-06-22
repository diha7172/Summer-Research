"""
Build a single self-contained HTML file from webapp/data/.

Inlines webapp/index.html + webapp/app.js and embeds every geography's
year-keyed profile (gzip + base64) so the result is one .html anyone can
double-click - no server, no Python, no API key, works offline. Decompression
happens in the browser via the native DecompressionStream API.

    py build_standalone.py            # -> Demographics_Explorer.html

Re-run after  py census_bulk.py  to refresh the embedded data.
"""

import os
import json
import gzip
import base64

DATA = os.path.join("webapp", "data")
OUT = "Demographics_Explorer.html"


def main():
    idx_path = os.path.join(DATA, "index.json")
    if not os.path.exists(idx_path):
        print("No data in webapp/data/. Run  py census_bulk.py  first.")
        return
    index = json.load(open(idx_path, encoding="utf-8"))
    meta = json.load(open(os.path.join(DATA, "meta.json"), encoding="utf-8"))
    profiles = {}
    pdir = os.path.join(DATA, "profiles")
    for fn in os.listdir(pdir):
        profiles.update(json.load(open(os.path.join(pdir, fn), encoding="utf-8")))

    blob = {"meta": meta, "index": index, "profiles": profiles}
    raw = json.dumps(blob, separators=(",", ":")).encode()
    b64 = base64.b64encode(gzip.compress(raw, 9)).decode()

    page = open(os.path.join("webapp", "index.html"), encoding="utf-8").read()
    appjs = open(os.path.join("webapp", "app.js"), encoding="utf-8").read()
    inline = ('<script>const DATA_B64="' + b64 + '";</script>\n'
              '<script>\n' + appjs + '\n</script>')
    html = page.replace('<script src="app.js"></script>', inline)
    if 'DATA_B64' not in html:
        raise SystemExit("Could not find the app.js script tag to replace.")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    mb = os.path.getsize(OUT) / 1e6
    print(f"Wrote {OUT}  ({mb:.1f} MB, {len(index):,} geographies, "
          f"years {meta.get('years', [meta['year']])})")
    print("Double-click it to open in any modern browser - no server needed.")


if __name__ == "__main__":
    main()
