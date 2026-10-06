"""
GitHub Pages용 사이트 폴더를 만든다.

  python scripts/build_site.py <results 폴더> <site 폴더> [--keep 30]

- results 의 가장 최근 ma20_turn_YYYYMMDD.html 을 site/index.html 로 복사
- 같은 파일을 site/reports/YYYYMMDD.html 로 보관 (최근 --keep 개만 유지)
- site/archive.html 에 보관된 리포트 목록 작성
site 폴더에 이전 배포본(gh-pages 브랜치)이 들어 있으면 그 보관본을 이어서 쓴다.
"""
import argparse
import html
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ARCHIVE_ATTR = "class='archive' href='archive.html'"

PAGE = """<!doctype html><html lang='ko'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>지난 리포트</title><style>
:root{{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
--grid:#e1e0d9;--border:rgba(11,11,11,.10)}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{color-scheme:dark;--page:#0d0d0d;
--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--grid:#2c2c2a;--border:rgba(255,255,255,.10)}}}}
:root[data-theme="dark"]{{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;
--grid:#2c2c2a;--border:rgba(255,255,255,.10)}}
body{{margin:0;background:var(--page);color:var(--ink);font:14px/1.5 system-ui,-apple-system,"Segoe UI","Malgun Gothic",sans-serif}}
main{{max-width:640px;margin:0 auto;padding:24px 16px 64px}}h1{{font-size:22px;margin:0 0 4px}}
.meta{{color:var(--ink2);margin:0 0 16px}}a{{color:var(--ink)}}
ul{{list-style:none;margin:0;padding:0;background:var(--surface);border:1px solid var(--border);border-radius:10px}}
li{{display:flex;justify-content:space-between;padding:10px 16px;border-bottom:1px solid var(--grid)}}
li:last-child{{border-bottom:0}}li span{{color:var(--muted);font-size:12px}}
</style></head><body><main>
<h1>지난 리포트</h1><p class='meta'><a href='index.html'>최신 리포트로</a> · 최근 {n}개 보관</p>
<ul>{items}</ul></main></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("site")
    ap.add_argument("--keep", type=int, default=30)
    args = ap.parse_args()

    results, site = Path(args.results), Path(args.site)
    reports = sorted(results.glob("ma20_turn_*.html"))
    if not reports:
        sys.exit("results 폴더에 리포트가 없습니다.")
    latest = reports[-1]
    day = re.search(r"(\d{8})", latest.name).group(1)

    (site / "reports").mkdir(parents=True, exist_ok=True)
    text = latest.read_text(encoding="utf-8")
    (site / "index.html").write_text(text, encoding="utf-8")
    (site / "reports" / f"{day}.html").write_text(
        text.replace(ARCHIVE_ATTR, "class='archive' href='../archive.html'"), encoding="utf-8")
    (site / ".nojekyll").write_text("", encoding="utf-8")

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
    (site / "archive.html").write_text(PAGE.format(n=len(kept), items="".join(items)), encoding="utf-8")
    print(f"사이트 생성: 기준일 {day}, 보관 {len(kept)}개 ({datetime.now():%Y-%m-%d %H:%M})")


if __name__ == "__main__":
    main()
