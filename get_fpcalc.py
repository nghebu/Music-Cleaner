#!/usr/bin/env python3
"""
Download Chromaprint's fpcalc.exe and drop it next to this script, so the
Song De-duplicator can do audio fingerprinting. Safe to re-run.
"""
import os
import sys
import shutil
import zipfile
import tempfile
import urllib.request

URL = ("https://github.com/acoustid/chromaprint/releases/download/"
       "v1.5.1/chromaprint-fpcalc-1.5.1-windows-x86_64.zip")
HERE = os.path.dirname(os.path.abspath(__file__))
DEST = os.path.join(HERE, "fpcalc.exe")


def main():
    if os.path.isfile(DEST):
        print("fpcalc.exe already present:", DEST)
        return 0
    print("Downloading fpcalc.exe ...")
    print(" ", URL)
    tmpzip = os.path.join(tempfile.gettempdir(), "chromaprint-fpcalc.zip")
    try:
        req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=90) as r, open(tmpzip, "wb") as f:
            shutil.copyfileobj(r, f)
        with zipfile.ZipFile(tmpzip) as z:
            member = next((n for n in z.namelist()
                           if n.lower().endswith("fpcalc.exe")), None)
            if not member:
                print("ERROR: fpcalc.exe not found inside the zip.")
                return 1
            with z.open(member) as src, open(DEST, "wb") as out:
                shutil.copyfileobj(src, out)
        print("Installed:", DEST)
        return 0
    except Exception as e:
        print("Download failed:", e)
        print("Manual fix: open this URL in a browser:")
        print(" ", URL)
        print("unzip it, and put fpcalc.exe in this folder:")
        print(" ", HERE)
        return 1
    finally:
        try:
            os.remove(tmpzip)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
