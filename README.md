# WAYVI — 외국인 관광객을 위한 음성 기반 스마트 교통 안내 키오스크

제4회 문화체육관광 AI·데이터 활용 공모전 제출작

## 기술 스택

| 구분 | 기술 |
|---|---|
| 백엔드 | Python 3.11 · FastAPI |
| 음성인식 | 브라우저 녹음 → STT 엔진 연동 (현재 비활성화, 텍스트 입력으로 대체 운영) |
| NLU/목적지 추출 | Google Gemini 2.5 Flash (문화 맥락 기반 목적지 추출) |
| 길찾기(경로 계산) | **ODsay 대중교통 길찾기 API** (서울역 → 목적지 실제 최적 경로) |
| 경로 설명 생성 | Google Gemini 2.5 Flash (ODsay 실데이터를 사용자 언어로 번역·서술, 경로 자체는 생성하지 않음) |
| 관광지 POI | 한국관광공사 다국어 관광정보 API (목적지 좌표 변환) |
| 주변 추천 | 한국관광공사 위치기반 관광정보 API (목적지 반경 내 실제 장소, Gemini는 추천 문구만 작성) |
| 지하철 실시간 | 서울시 지하철 실시간 도착정보 API (보조 정보) |
| QR 생성 | qrcode (PIL) |
| 프론트엔드 | HTML · CSS · Vanilla JS |

## AI·데이터 파이프라인

```
[사용자 발화 텍스트]
      ↓
① NLU (Gemini) — 문화 맥락 기반 목적지 추출 (예: "BTS 뮤비 찍은 고궁" → 경복궁)
      ↓
② POI 정규화 (한국관광공사 API) — 목적지명 → 실제 좌표(위경도)
      ↓
③ 길찾기 (ODsay API) — 서울역(출발) → 목적지 좌표 실제 최적 경로 계산
      ↓                  (노선, 환승역, 방향, 다음 정차역, 소요시간 — 전부 API 원본)
④ 경로 설명 생성 (Gemini) — ③의 사실 데이터를 사용자 언어로 번역·서술 (사실 생성 X)
      ↓
⑤ 주변 추천 (한국관광공사 위치기반 API) — 목적지 반경 내 실제 장소 5곳 + Gemini 추천 문구
      ↓
⑥ QR 코드 생성 + 다국어 출력
```

**설계 원칙**: 노선명·환승역·진행방향·다음 정차역 같은 "사실(fact)"은 전부 ODsay/관광공사
공공 API 원본에서만 가져오고, AI(Gemini)는 그 사실을 사용자 언어로 자연스럽게 번역·서술하는
역할만 합니다. AI가 경로나 장소를 추정·생성하지 않도록 분리했습니다.

> ODsay는 호차 번호·세부 출구 번호를 제공하지 않아 해당 항목은 현재 버전에서 표시하지
> 않습니다 (추정값을 보여주는 대신 생략).

## 설치 및 실행

```bash
# 1. 의존성 설치
pip install -r requirements.txt

# 2. 환경변수 설정
cp .env.example .env
# .env 파일에 API 키 입력 (GEMINI_API_KEY, TOUR_API_KEY, SEOUL_METRO_KEY, ODSAY_API_KEY)
# ODsay API 키는 https://lab.odsay.com 에서 발급 (서비스 플랫폼: Server)

# 3. 실행
python -m uvicorn main:app --reload --port 8000

# 4. 브라우저에서 열기
# http://localhost:8000
```

## 주요 기능

- **다국어 텍스트/음성 입력**: 한국어, 영어, 일본어, 중국어, 스페인어 등 자동 감지 (현재 텍스트 입력 기반, 음성 입력은 추후 활성화 예정)
- **문화 맥락 기반 매핑**: "BTS 뮤비 찍은 고궁" → 경복궁
- **실제 경로 계산**: ODsay 대중교통 API 기반 서울역 → 목적지 최적 경로, 환승역·방향·다음역까지 실데이터
- **주변 추천**: 한국관광공사 위치기반 데이터로 목적지 반경 내 실제 장소 5곳 안내
- **QR 코드**: 앱 없이 스마트폰에 저장, 오프라인 열람 가능
- **영수증 출력**: 손에 들고 표지판과 대조 가능

## API 엔드포인트

| Method | Endpoint | 설명 |
|---|---|---|
| POST | `/api/stt` | 음성 → 텍스트 (현재 비활성화) |
| POST | `/api/extract-destination` | Gemini NLU 목적지 추출 |
| GET | `/api/poi?keyword=경복궁` | 관광공사 POI 조회 (좌표 변환) |
| GET | `/api/odsay-route` | ODsay 실제 길찾기 (서울역 → 좌표) |
| GET | `/api/nearby` | 관광공사 위치기반 주변 추천 5곳 |
| GET | `/api/subway-route` | 지하철 실시간 도착정보 (보조) |
| POST | `/api/generate-route` | Gemini 경로 설명 생성 (ODsay 사실 데이터 기반) |
| POST | `/api/generate-qr` | QR 코드 생성 |
| POST | `/api/navigate` | **통합 파이프라인** |

## 개발자

조재원 · ZN Labs
