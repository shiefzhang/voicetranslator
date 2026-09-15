"""Resumable, streaming download. Only HTTPS; existing partials are never trusted."""
import hashlib
import os
import time
import urllib.request
from pathlib import Path

def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()

def download(url, path, expected=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (not expected or sha256(path) == expected):
        return path
    part = path.with_name(path.name + '.part')
    for attempt in range(5):
        try:
            offset = part.stat().st_size if part.exists() else 0
            headers = {'User-Agent': 'VoiceTranslator-model-preparer/1.0'}
            if 'api.github.com/repos/' in url and '/releases/assets/' in url:
                headers['Accept'] = 'application/octet-stream'
            if offset:
                headers['Range'] = f'bytes={offset}-'
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=45) as r:
                append = offset > 0 and r.status == 206
                if append and not r.headers.get('Content-Range', '').startswith(f'bytes {offset}-'):
                    raise ValueError('Server returned wrong download offset')
                with open(part, 'ab' if append else 'wb') as f:
                    while True:
                        b = r.read(1024 * 1024)
                        if not b: break
                        f.write(b)
            if expected and sha256(part) != expected:
                part.unlink()
                raise ValueError('SHA-256 mismatch')
            os.replace(part, path)
            print(f'Downloaded {path.name}: {path.stat().st_size} bytes', flush=True)
            return path
        except Exception as e:
            print(f'Download retry {attempt + 1}: {path.name}: {e}', flush=True)
            if attempt == 4: raise
            time.sleep(2)

if __name__ == '__main__':
    import sys
    download(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv)>3 else None)
