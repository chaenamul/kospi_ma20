# KRX MA20 전환 리포트

KOSPI 계열 지수와 주식형·원자재 ETF 중에서 20일 이동평균(주가·거래량)이 하락하다가 상승으로 돌아선 종목을 찾아 리포트로 보여줍니다. GitHub Actions가 매일 실행하고, 결과는 GitHub Pages 사이트에 올라갑니다.

- 최신 리포트: `https://chaenamul.github.io/kospi_ma20/`
- 지난 리포트: `https://chaenamul.github.io/kospi_ma20/archive.html` (최근 30개 보관)

모든 페이지에 검색엔진 수집 금지(noindex)가 표시되어 있고 robots.txt로 크롤러를 막습니다.

기술적 조건으로 걸러낸 결과일 뿐이며 투자 권유가 아닙니다.

## 데이터

KRX OPEN API 이용약관에 따라 리포트에 “한국거래소 통계정보”를 사용했다는 문구를 표시하고, 비상업적 용도로만 사용합니다. 약관 제11조 2항은 받은 정보를 제3자에게 제공하지 못하게 하고 있으니 사이트 주소는 필요한 사람에게만 공유하세요.

KRX Open API의 두 서비스를 사용합니다. 호출이 실패하면 실행이 실패로 끝나고 사이트는 이전 리포트를 유지합니다.

- KOSPI 시리즈 일별시세정보 `idx/kospi_dd_trd`
- ETF 일별매매정보 `etp/etf_bydd_trd`

KRX는 전 거래일 데이터를 다음 영업일 아침 8시쯤 반영합니다. 그래서 자동 실행은 평일(월~금) 한국 시간 08:30, 09:30, 10:00에 돌고, 리포트 상단에 데이터 기준일이 표시됩니다. GitHub 예약 실행은 늦게 시작되거나 빠질 수 있어 세 번 실행합니다.

## 처음 설정

1. **인증키 등록**: Settings → Secrets and variables → Actions → **Secrets** 탭 → New repository secret
   이름 `KRX_API_KEY`, 값에 KRX Open API 인증키
2. **Pages 켜기**: Settings → Pages → Build and deployment → Source를 **GitHub Actions**로
3. **첫 실행**: Actions 탭 → Daily MA20 report → Run workflow

## 내 컴퓨터에서 실행

`실행.bat`을 더블클릭하면 됩니다(Windows, Python 3.8 이상). 처음 실행할 때 필요한 패키지를 설치하고 인증키를 물어본 뒤 `.env`에 저장합니다. 결과는 `results` 폴더에 저장되고 브라우저로 열립니다.

터미널에서는 다음과 같이 실행합니다.

```
pip install -r requirements.txt
python krx_ma20_turn.py                    # 지수 + 주식형/원자재 ETF
python krx_ma20_turn.py --target etf --etf-types commodity   # 원자재 ETF만
python krx_ma20_turn.py --help             # 옵션 전체
```

## 파일

| 파일 | 역할 |
|---|---|
| `krx_ma20_turn.py` | 데이터 수신, 판정, CSV·HTML 리포트 생성 |
| `scripts/build_site.py` | 사이트 폴더 구성 (최신 리포트, 지난 리포트 목록) |
| `.github/workflows/daily.yml` | 매일 자동 실행과 배포 |
| `실행.bat` | Windows에서 더블클릭 실행 |
