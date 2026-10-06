"""
GitHub Pages용 사이트 폴더를 만든다. 리포트는 비밀번호로 암호화해서 올린다.

  SITE_PASSWORD=... python scripts/build_site.py <results 폴더> <site 폴더> [--keep 30]

- results 의 가장 최근 ma20_turn_YYYYMMDD.html 을 site/index.html 로 (암호화)
- 같은 리포트를 site/reports/YYYYMMDD.html 로 보관 (최근 --keep 개만 유지, 암호화)
- site/archive.html 에 보관된 리포트 날짜 목록 작성
- 모든 페이지에 검색엔진 수집 금지(noindex) 표시

리포트 본문은 AES-256-GCM(키: 비밀번호 + PBKDF2-SHA256)으로 암호화되고,
브라우저에서 비밀번호를 입력해야 풀린다. 비밀번호가 없으면 사이트를 만들지 않고 실패한다.
site 폴더에 이전 배포본(gh-pages 브랜치)이 들어 있으면 그 보관본을 이어서 쓴다.
"""
import argparse
import base64
import html
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITERATIONS = 310_000
ARCHIVE_ATTR = "class='archive' href='archive.html'"
ROBOTS = "<meta name='robots' content='noindex,nofollow,noarchive,nosnippet,noimageindex'>"

BASE_CSS = """
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
--grid:#e1e0d9;--border:rgba(11,11,11,.10);--bad:#d03b3b}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--page:#0d0d0d;
--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--grid:#2c2c2a;--border:rgba(255,255,255,.10);--bad:#e66767}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;
--grid:#2c2c2a;--border:rgba(255,255,255,.10);--bad:#e66767}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.5 system-ui,-apple-system,"Segoe UI","Malgun Gothic",sans-serif}
main{max-width:640px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}
.meta{color:var(--ink2);margin:0 0 16px}a{color:var(--ink)}
"""

LOCK_PAGE = """<!doctype html><html lang='ko'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>{robots}
<title>MA20 전환 리포트</title><style>{css}
form{{display:flex;gap:8px;margin-top:16px}}
input{{flex:1;min-width:0;height:40px;padding:0 12px;border:1px solid var(--border);border-radius:8px;
background:var(--surface);color:var(--ink);font:inherit}}
button{{height:40px;padding:0 16px;border:0;border-radius:8px;background:var(--ink);color:var(--surface);
font:inherit;font-weight:600;cursor:pointer}}button:disabled{{opacity:.5;cursor:default}}
#msg{{min-height:20px;margin:8px 0 0;font-size:13px;color:var(--ink2)}}#msg.bad{{color:var(--bad)}}
</style></head><body><main>
<h1>MA20 전환 리포트</h1><p class='meta'>비밀번호를 입력하면 리포트가 열립니다.</p>
<form id='f'><input id='pw' type='password' autocomplete='current-password' placeholder='비밀번호' required autofocus>
<button id='go' type='submit'>열기</button></form><p id='msg'></p>
</main>
<script id='payload' type='application/json'>{payload}</script>
<script>
const P=JSON.parse(document.getElementById('payload').textContent);
const KEY='krx-ma20-pw';
const b64=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
const msg=document.getElementById('msg'),go=document.getElementById('go');
function store(v){{try{{v?localStorage.setItem(KEY,v):localStorage.removeItem(KEY);}}catch(e){{}}}}
function saved(){{try{{return localStorage.getItem(KEY);}}catch(e){{return null;}}}}
async function unlock(pw){{
 const base=await crypto.subtle.importKey('raw',new TextEncoder().encode(pw),'PBKDF2',false,['deriveKey']);
 const key=await crypto.subtle.deriveKey({{name:'PBKDF2',salt:b64(P.s),iterations:P.i,hash:'SHA-256'}},
   base,{{name:'AES-GCM',length:256}},false,['decrypt']);
 const pt=await crypto.subtle.decrypt({{name:'AES-GCM',iv:b64(P.v)}},key,b64(P.c));
 return new TextDecoder().decode(pt);}}
async function attempt(pw,remember){{
 go.disabled=true;msg.className='';msg.textContent='여는 중…';
 try{{const h=await unlock(pw);if(remember)store(pw);document.open();document.write(h);document.close();}}
 catch(e){{store(null);go.disabled=false;msg.className='bad';msg.textContent='비밀번호가 맞지 않습니다.';}}}}
document.getElementById('f').addEventListener('submit',e=>{{e.preventDefault();
 attempt(document.getElementById('pw').value,true);}});
const s=saved();if(s)attempt(s,false);
</script></body></html>"""

ARCHIVE_PAGE = """<!doctype html><html lang='ko'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>{robots}
<title>지난 리포트</title><style>{css}
ul{{list-style:none;margin:0;padding:0;background:var(--surface);border:1px solid var(--border);border-radius:10px}}
li{{display:flex;justify-content:space-between;padding:10px 16px;border-bottom:1px solid var(--grid)}}
li:last-child{{border-bottom:0}}li span{{color:var(--muted);font-size:12px}}
</style></head><body><main>
<h1>지난 리포트</h1><p class='meta'><a href='index.html'>최신 리포트로</a> · 최근 {n}개 보관</p>
<ul>{items}</ul></main></body></html>"""


def encrypt_page(text, password):
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERATIONS).derive(
        password.encode("utf-8"))
    ct = AESGCM(key).encrypt(iv, text.encode("utf-8"), None)
    payload = json.dumps({"s": base64.b64encode(salt).decode(), "v": base64.b64encode(iv).decode(),
                          "i": ITERATIONS, "c": base64.b64encode(ct).decode()})
    return LOCK_PAGE.format(robots=ROBOTS, css=BASE_CSS, payload=payload)


def add_robots(text):
    """복호화된 리포트에도 noindex 표시를 넣는다."""
    return text if "name='robots'" in text else text.replace("<meta charset='utf-8'>",
                                                             "<meta charset='utf-8'>" + ROBOTS, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("site")
    ap.add_argument("--keep", type=int, default=30)
    args = ap.parse_args()

    password = os.environ.get("SITE_PASSWORD", "")
    if len(password) < 4:
        sys.exit("SITE_PASSWORD 가 없거나 너무 짧습니다. 암호화 없이 공개하지 않도록 사이트 생성을 중단합니다.")

    results, site = Path(args.results), Path(args.site)
    reports = sorted(results.glob("ma20_turn_*.html"))
    if not reports:
        sys.exit("results 폴더에 리포트가 없습니다.")
    latest = reports[-1]
    day = re.search(r"(\d{8})", latest.name).group(1)

    (site / "reports").mkdir(parents=True, exist_ok=True)
    text = add_robots(latest.read_text(encoding="utf-8"))
    (site / "index.html").write_text(encrypt_page(text, password), encoding="utf-8")
    (site / "reports" / f"{day}.html").write_text(
        encrypt_page(text.replace(ARCHIVE_ATTR, "class='archive' href='../archive.html'"), password),
        encoding="utf-8")
    (site / ".nojekyll").write_text("", encoding="utf-8")
    (site / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")

    kept = sorted((site / "reports").glob("*.html"), reverse=True)
    for old in kept[args.keep:]:
        old.unlink()
    kept = kept[:args.keep]

    items = []
    for f in kept:
        d = f.stem
        label = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        tag = "최신" if d == day else ""
        items.append(f"<li><a href='reports/{html.escape(f.name)}'>{label} 기준</a><span>{tag}</span></li>")
    (site / "archive.html").write_text(
        ARCHIVE_PAGE.format(robots=ROBOTS, css=BASE_CSS, n=len(kept), items="".join(items)), encoding="utf-8")
    print(f"사이트 생성: 기준일 {day}, 보관 {len(kept)}개 ({datetime.now():%Y-%m-%d %H:%M})")


if __name__ == "__main__":
    main()
