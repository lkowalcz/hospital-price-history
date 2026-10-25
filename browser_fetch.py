"""Optional browser transport for Yale's JavaScript challenge.

Requires requirements-local.txt plus Chromium and its matching chromedriver.
Sessions use a disposable profile; cookies never leave the browser or enter Git.
"""

import base64
import hashlib
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from scrape import SAMPLE_BYTES, fingerprint_offsets


# All requests execute in the browser that passed the challenge. Limit each
# WebDriver response to one sample/chunk, even for a multi-gigabyte download.
ENCODE = """
function encode(bytes) {
  let s = '';
  for (let i = 0; i < bytes.length; i += 32768)
    s += String.fromCharCode(...bytes.subarray(i, i + 32768));
  return btoa(s);
}
"""
RANGE = ENCODE + """
const [url, offset, count, validator] = args;
const headers = {Range: `bytes=${offset}-${offset + count - 1}`};
if (validator) headers['If-Range'] = validator;
const r = await fetch(url, {headers, cache: 'no-store', signal: AbortSignal.timeout(60000)});
if (r.status === 200) { await r.body.cancel(); return null; }
if (r.status !== 206) throw new Error(`Range request returned HTTP ${r.status}`);
const reader = r.body.getReader(), chunks = [];
let length = 0;
while (true) {
  const chunk = await reader.read();
  if (chunk.done) break;
  length += chunk.value.length;
  if (length > count) { await reader.cancel(); throw new Error('Oversized browser range'); }
  chunks.push(chunk.value);
}
const bytes = new Uint8Array(length);
let pos = 0;
for (const chunk of chunks) { bytes.set(chunk, pos); pos += chunk.length; }
return {range: r.headers.get('content-range'), etag: r.headers.get('etag'),
        modified: r.headers.get('last-modified'), data: encode(bytes)};
"""
BEGIN_DOWNLOAD = """
const r = await fetch(args[0], {cache: 'no-store', signal: AbortSignal.timeout(7200000)});
if (r.status !== 200) { await r.body.cancel(); throw new Error(`Download returned HTTP ${r.status}`); }
if ((r.headers.get('content-type') || '').includes('text/html')) {
  await r.body.cancel(); throw new Error('Received HTML instead of an MRF');
}
window.hphDownload = {reader: r.body.getReader(), pending: new Uint8Array(0)};
return {length: r.headers.get('content-encoding') ? null : r.headers.get('content-length')};
"""
READ_CHUNK = ENCODE + """
const d = window.hphDownload;
if (!d.pending.length) {
  const next = await d.reader.read();
  if (next.done) return {done: true};
  d.pending = next.value;
}
const bytes = d.pending.subarray(0, args[0]);
d.pending = d.pending.subarray(bytes.length);
return {done: false, data: encode(bytes)};
"""


class BrowserFetch:
    def __init__(self, page_url, file_url, scratch):
        self.page_url, self.file_url, self.scratch = page_url, file_url, scratch
        self.driver = self.profile = None

    def __enter__(self):
        # Imported only on fallback; CI and the ordinary scraper need no browser.
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.support.ui import WebDriverWait

        driver_path = os.environ.get("CHROMEDRIVER") or shutil.which("chromedriver")
        if not driver_path:
            raise RuntimeError("Install chromium-driver or set CHROMEDRIVER (docs/OPERATIONS.md)")
        self.profile = tempfile.TemporaryDirectory(prefix="browser-", dir=self.scratch)
        try:
            options = webdriver.ChromeOptions()
            binary = os.environ.get("BROWSER_BINARY") or shutil.which("chromium")
            if binary:
                options.binary_location = binary
            for arg in ("--headless=new", "--no-first-run", "--no-default-browser-check",
                        "--user-data-dir=" + self.profile.name):
                options.add_argument(arg)
            options.page_load_strategy = "eager"
            self.driver = webdriver.Chrome(service=Service(driver_path), options=options)
            self.driver.set_page_load_timeout(60)
            self.driver.set_script_timeout(75)
            self.driver.get(self.page_url)
            WebDriverWait(self.driver, 60).until(lambda d: d.execute_script(
                "return Array.from(document.links).some(a => a.href === arguments[0]);", self.file_url))
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *exc):
        try:
            if self.driver:
                self.driver.quit()
        finally:
            if self.profile:
                self.profile.cleanup()

    def _execute(self, script, *args):
        result = self.driver.execute_async_script(
            "const args = Array.from(arguments), done = args.pop();\n"
            "(async () => {\n" + script + "\n})().then(done, e => done({error: String(e)}));", *args)
        if isinstance(result, dict) and result.get("error"):
            raise RuntimeError(result["error"])
        return result

    def fingerprint(self):
        def read(offset, validator=None):
            result = self._execute(RANGE, self.file_url, offset, SAMPLE_BYTES, validator)
            if result is None:  # Range unsupported, or If-Range reports a new version.
                return None
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", result.get("range") or "")
            if not match:
                raise ValueError("Invalid browser Content-Range")
            start, end, total = map(int, match.groups())
            data = base64.b64decode(result["data"], validate=True)
            if (total <= 0 or start != offset or end != min(offset + SAMPLE_BYTES, total) - 1
                    or len(data) != end - start + 1):
                raise ValueError("Incomplete or incorrect browser range")
            return result, data, total

        first = read(0)
        if first is None:
            return None
        headers, data, total = first
        etag = headers.get("etag") or ""
        validator = etag if etag and not etag.startswith("W/") else headers.get("modified")
        digest = hashlib.sha256(str(total).encode())
        digest.update(data)
        for offset in fingerprint_offsets(total)[1:]:
            sample = read(offset, validator)
            if sample is None or sample[2] != total:
                return None
            other, data, _ = sample
            if any(headers.get(k) != other.get(k) for k in ("etag", "modified")):
                return None
            digest.update(data)
        return digest.hexdigest()

    def download(self, dest):
        """Stream one GET to disk. Never publish an empty or partial download."""
        dest = Path(dest)
        partial = dest.with_name(dest.name + ".part")
        try:
            response = self._execute(BEGIN_DOWNLOAD, self.file_url)
            expected = int(response["length"]) if response["length"] is not None else None
            deadline = time.monotonic() + 7200
            size = 0
            with partial.open("wb") as handle:
                while True:
                    if time.monotonic() > deadline:
                        raise TimeoutError("Browser download exceeded two hours")
                    chunk = self._execute(READ_CHUNK, SAMPLE_BYTES)
                    if chunk["done"]:
                        break
                    data = base64.b64decode(chunk["data"], validate=True)
                    if not data or len(data) > SAMPLE_BYTES:
                        raise ValueError("Invalid browser download chunk")
                    handle.write(data)
                    size += len(data)
            if not size or (expected is not None and size != expected):
                raise ValueError("Empty or incomplete browser download")
            with partial.open("rb") as handle:
                prefix = handle.read(512).lstrip(b"\xef\xbb\xbf").lstrip().lower()
            if prefix.startswith((b"<!doctype", b"<html", b"<head")):
                raise ValueError("Received HTML instead of an MRF")
            partial.replace(dest)
        finally:
            partial.unlink(missing_ok=True)
            self.driver.execute_script(
                "if (window.hphDownload) { window.hphDownload.reader.cancel().catch(() => {});"
                " delete window.hphDownload; }")
