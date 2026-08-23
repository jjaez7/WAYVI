from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import google.generativeai as genai
# from google.cloud import speech  # Google Cloud STT (음성 기능 활성화 시 주석 해제)
import httpx
import qrcode
import qrcode.image.svg
import io
import base64
import json
import os
import re
import uuid
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="WAYVI API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API 키 설정 ──────────────────────────────────────────
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "")
TOUR_API_KEY    = os.getenv("TOUR_API_KEY", "")
SEOUL_METRO_KEY = os.getenv("SEOUL_METRO_KEY", "")
ODSAY_API_KEY   = os.getenv("ODSAY_API_KEY", "")  # https://lab.odsay.com (서비스 플랫폼: Server)

# Gemini 클라이언트
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-2.5-flash-lite")

# Google Cloud STT (음성 기능 활성화 시 주석 해제)
# speech_client = speech.SpeechClient()

# QR 연결용 임시 저장소 (메모리)
route_store: dict = {}

# "경로 선택" 2단계 흐름용 임시 저장소 (메모리)
# /api/navigate 에서 옵션을 보여준 뒤, 사용자가 고른 다음 /api/navigate/select-route 에서
# 이어받아 마무리한다. 데모/공모전 규모라 TTL 없이 단순 dict로 운용 (선택 시 즉시 pop).
nav_session_store: dict = {}

# ── 모델 ──────────────────────────────────────────────────
class RouteResult(BaseModel):
    destination_name: str
    destination_name_local: str
    detected_language: str
    nearest_station: str
    line: str
    direction: str
    fast_exit_car: str
    transfer_info: list
    total_time: str
    steps: list
    cultural_context: Optional[str] = None


# ── STT (비활성화) ────────────────────────────────────────
@app.post("/api/stt")
async def speech_to_text(audio: UploadFile = File(...)):
    """음성 기능 비활성화 상태. 텍스트 입력을 이용하세요."""
    raise HTTPException(
        status_code=501,
        detail="음성 인식 기능은 현재 비활성화 상태입니다. 텍스트 입력을 이용해주세요."
    )


# ── Gemini NLU — 목적지 추출 ─────────────────────────────
@app.post("/api/extract-destination")
async def extract_destination(payload: dict):
    text     = payload.get("text", "")
    language = payload.get("language", "unknown")

    prompt = f"""You are a smart destination extractor for a Korean subway navigation kiosk.

User's utterance: "{text}"

STEP 1 — Detect the language of the utterance above. This is critical.
STEP 2 — Extract the intended Korean tourist destination from the utterance.
STEP 3 — Identify any cultural context clue (K-pop, K-drama, food, history, etc.).

Return ONLY valid JSON, no extra text, no markdown backticks.

Output format:
{{
  "detected_language": "(ISO 639-1 code: ko/en/ja/zh/es/fr/de/...)",
  "destination_korean": "경복궁",
  "destination_display": "(destination name in the DETECTED language)",
  "cultural_context": "(cultural context clue in the DETECTED language)",
  "confidence": 0.95,
  "candidates": []
}}

IMPORTANT: detected_language MUST reflect the actual language of the utterance.
- If the user spoke Korean → "ko"
- If English → "en"
- If Japanese → "ja"
- etc.

Cultural context and destination_display must be written in the detected language.
If confidence < 0.7, return top 3 candidates as objects with destination_korean and destination_display.

Common mappings:
- BTS + palace/고궁 → 경복궁
- 넷플릭스 시장 / Netflix market → 광장시장 or 통인시장
- 세종대왕 동상 → 광화문광장
- 벚꽃 공원 / cherry blossom → 여의도한강공원 or 남산공원
- K-pop idol streets → 홍대 or 강남
"""

    response = gemini_model.generate_content(prompt)
    raw = response.text.strip()
    raw = re.sub(r"```(?:json)?", "", raw).strip("`").strip()
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if not match:
        raise HTTPException(status_code=500, detail="LLM 파싱 오류")

    return json.loads(match.group())


# ── 한국관광공사 POI ──────────────────────────────────────
@app.get("/api/poi")
async def get_poi(keyword: str):
    url = "https://apis.data.go.kr/B551011/KorService2/searchKeyword2"
    params = {
        "serviceKey": TOUR_API_KEY,
        "MobileOS": "ETC",
        "MobileApp": "WAYVI",
        "_type": "json",
        "keyword": keyword,
        "numOfRows": 5,
        "contentTypeId": 12,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
        if resp.status_code != 200:
            return {"items": []}
        data  = resp.json()
        items = data.get("response", {}).get("body", {}).get("items", {}).get("item", [])
        if isinstance(items, dict):
            items = [items]
        results = [
            {
                "title":     item.get("title"),
                "addr":      item.get("addr1"),
                "mapx":      item.get("mapx"),
                "mapy":      item.get("mapy"),
                "contentid": item.get("contentid"),
            }
            for item in items
        ]
        return {"items": results}
    except Exception:
        return {"items": []}


# ── 한국관광공사 상세정보 (운영시간·입장료·휴관일) ────────
async def get_poi_detail(contentid: str) -> dict:
    """contentid로 detailCommon2 + detailIntro2 API를 호출해
    운영시간·입장료·휴관일 등 상세 정보를 반환한다."""
    if not contentid:
        return {}

    base_params = {
        "serviceKey":  TOUR_API_KEY,
        "MobileOS":    "ETC",
        "MobileApp":   "WAYVI",
        "_type":       "json",
        "contentId":   contentid,
        "defaultYN":   "Y",
        "firstImageYN":"Y",
        "addrinfoYN":  "Y",
        "overviewYN":  "Y",
    }
    detail = {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # ① 공통 상세정보 (개요, 주소, 전화번호)
            r1 = await client.get(
                "https://apis.data.go.kr/B551011/KorService2/detailCommon2",
                params=base_params,
            )
            if r1.status_code == 200:
                item = (
                    r1.json()
                    .get("response", {}).get("body", {})
                    .get("items", {}).get("item", [{}])
                )
                item = item[0] if isinstance(item, list) else item
                detail["overview"]  = item.get("overview", "")
                detail["homepage"]  = item.get("homepage", "")
                detail["tel"]       = item.get("tel", "")

            # ② 소개 상세정보 (운영시간·입장료·휴관일) — contentTypeId=12 관광지
            intro_params = {
                "serviceKey":    TOUR_API_KEY,
                "MobileOS":      "ETC",
                "MobileApp":     "WAYVI",
                "_type":         "json",
                "contentId":     contentid,
                "contentTypeId": "12",
            }
            r2 = await client.get(
                "https://apis.data.go.kr/B551011/KorService2/detailIntro2",
                params=intro_params,
            )
            if r2.status_code == 200:
                item2 = (
                    r2.json()
                    .get("response", {}).get("body", {})
                    .get("items", {}).get("item", [{}])
                )
                item2 = item2[0] if isinstance(item2, list) else item2
                detail["usetime"]    = item2.get("usetime", "")      # 운영시간
                detail["restdate"]   = item2.get("restdate", "")     # 휴관일
                detail["usefee"]     = item2.get("usefee", "")       # 입장료
                detail["parking"]    = item2.get("parking", "")      # 주차
                detail["infocenter"] = item2.get("infocenter", "")   # 문의처
    except Exception:
        pass
    return detail


# ── 관광지명 → 인근 지하철역명 변환 ──────────────────────
# 자주 쓰이는 관광지-역명 매핑 테이블 (POI 주소 기반 보완)
_PLACE_TO_STATION: dict[str, str] = {
    "경복궁":      "경복궁",
    "광화문광장":  "광화문",
    "광화문":      "광화문",
    "창덕궁":      "안국",
    "창경궁":      "혜화",
    "덕수궁":      "시청",
    "종묘":        "종로3가",
    "남산":        "명동",
    "N서울타워":   "명동",
    "명동":        "명동",
    "홍대":        "홍대입구",
    "홍익대학교":  "홍대입구",
    "이태원":      "이태원",
    "강남":        "강남",
    "코엑스":      "삼성",
    "삼성":        "삼성",
    "동대문":      "동대문역사문화공원",
    "동대문디자인플라자": "동대문역사문화공원",
    "DDP":         "동대문역사문화공원",
    "인사동":      "안국",
    "북촌한옥마을":"안국",
    "성수":        "성수",
    "성수동":      "성수",
    "광장시장":    "종로4가",
    "통인시장":    "경복궁",
    "여의도한강공원":"여의도",
    "여의도":      "여의도",
    "남산공원":    "명동",
    "롯데월드":    "잠실",
    "잠실":        "잠실",
    "올림픽공원":  "올림픽공원",
    "수원화성":    "수원",
    "서울숲":      "뚝섬",
    "뚝섬":        "뚝섬",
    "이화여대":    "이대",
    "신촌":        "신촌",
    "연남동":      "홍대입구",
    "망원":        "망원",
    "합정":        "합정",
    "상암":        "디지털미디어시티",
    "용산":        "용산",
    "국립중앙박물관": "이촌",
    "이촌":        "이촌",
    "한강공원":    "이촌",
    "압구정":      "압구정",
    "청담":        "청담",
    "신사":        "신사",
    "가로수길":    "신사",
    "건대":        "건대입구",
    "건국대학교":  "건대입구",
}

def _resolve_station_name(destination_korean: str, poi_addr: str) -> str:
    """관광지명 또는 POI 주소에서 가장 가까운 지하철 역명을 반환한다.
    (ODsay 응답에 역명이 없는 도보 전용 구간 등 fallback 표시용으로만 사용)"""
    # 1순위: 직접 매핑 테이블
    for key, station in _PLACE_TO_STATION.items():
        if key in destination_korean:
            return station

    # 2순위: POI 주소에서 동/구 키워드로 추정
    addr_hints = {
        "종로구": "경복궁", "중구": "명동", "마포구": "홍대입구",
        "강남구": "강남",   "송파구": "잠실", "용산구": "이태원",
        "성동구": "성수",   "영등포구": "여의도",
    }
    for hint, station in addr_hints.items():
        if hint in poi_addr:
            return station

    # 3순위: 관광지명 그대로 (역명과 동일한 경우 e.g. "홍대입구")
    return destination_korean


# ── 출발지 좌표 (현재는 서울역 고정) ──────────────────────
SEOUL_STATION_COORD = {"x": 126.972559, "y": 37.554678}  # 서울역(경위도)


class ODsayError(Exception):
    """ODsay API 호출/응답 오류. 라우트는 이 예외를 받으면 즉시 중단하고
    사용자에게 에러를 표시한다 (AI 추정으로 대체하지 않음)."""
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


# ── ODsay 대중교통 길찾기 ─────────────────────────────────
async def fetch_odsay_candidates_raw(sx: float, sy: float, ex: float, ey: float) -> list:
    """ODsay searchPubTransPathT 를 호출해 출발지 → 목적지의 모든 후보 경로(원본)를
    가공 없이 그대로 반환한다.

    주의: OPT=0("추천경로")은 ODsay 내부 알고리즘이 1개만 골라주는 것이라
    반드시 최단시간/최적이라는 보장이 없다. 그래서 OPT=1(타입별 정렬: 지하철/버스/
    버스+지하철 등 복수 후보)로 호출해서 후보 전체를 받아오고, 그중 무엇을 보여줄지는
    이 함수를 호출하는 쪽(예: categorize_odsay_paths)에서 숫자로 직접 비교해 정한다.
    AI는 이 비교에 전혀 관여하지 않는다."""
    if not ODSAY_API_KEY:
        raise ODsayError("ODSAY_API_KEY가 설정되지 않았습니다. .env 파일을 확인해주세요.")

    url = "https://api.odsay.com/v1/api/searchPubTransPathT"
    params = {
        "apiKey": ODSAY_API_KEY,
        "SX": sx, "SY": sy,
        "EX": ex, "EY": ey,
        "OPT": 1,             # 1 = 타입별 정렬 → 지하철/버스/버스+지하철 등 복수 후보 반환
        "SearchPathType": 0,  # 0 = 지하철+버스 모두 포함
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
    except Exception as e:
        raise ODsayError(f"ODsay API 호출 실패: {e}")

    if resp.status_code != 200:
        raise ODsayError(f"ODsay API 응답 오류 (status {resp.status_code})")

    data = resp.json()

    if "error" in data:
        err = data["error"]
        err_msg = err[0].get("message", "알 수 없는 오류") if isinstance(err, list) else str(err)
        raise ODsayError(f"ODsay API 오류: {err_msg}")

    paths = data.get("result", {}).get("path", [])
    if not paths:
        raise ODsayError("ODsay에서 해당 구간의 대중교통 경로를 찾지 못했습니다 (출발지/도착지 반경 700m 내 정류장 없음 등).")

    return paths


async def fetch_odsay_route(sx: float, sy: float, ex: float, ey: float) -> dict:
    """하위 호환용 단일 경로 조회. 모든 후보 중 실제 총 소요시간(info.totalTime)이
    가장 짧은 경로 하나만 직접 선택해 반환한다 (/api/odsay-route 단독 호출 엔드포인트용).
    여러 옵션을 사용자에게 보여줘야 하는 /api/navigate 흐름에서는
    fetch_odsay_candidates_raw + categorize_odsay_paths 를 사용한다."""
    paths = await fetch_odsay_candidates_raw(sx, sy, ex, ey)
    fastest = min(paths, key=lambda p: p.get("info", {}).get("totalTime", float("inf")))
    return fastest


# 경로 옵션 배지 우선순위 (화면에 보여주는 카드 정렬 순서)
# 배지는 언어 중립 키로 보낸다 — 실제 표시 문구(최적/Optimal/最適 등)는
# 프론트엔드에서 detected_language에 맞춰 번역해 렌더링한다.
_BADGE_PRIORITY = {"optimal": 0, "fastest": 1, "least_walk": 2, "cheapest": 3}


def categorize_odsay_paths(paths: list) -> list:
    """ODsay 후보 경로 전체를 4가지 기준으로 분류한다 — 네이버/카카오 지도처럼
    '최적 · 최소시간 · 최소도보 · 최저요금' 중 사용자가 직접 고르게 하기 위함이다.

    - fastest / least_walk / cheapest: ODsay 원본 수치(totalTime/totalWalk/payment)를
      그대로 비교해서 가장 작은 값을 가진 경로를 고른다.
    - optimal: 시간·환승 횟수·도보 거리를 함께 고려한 균형 점수(가중합, 낮을수록 좋음)로
      고른다. 가중치는 휴리스틱이지만 입력값은 전부 ODsay 원본 수치이며, AI는
      이 계산에 전혀 관여하지 않는다.

    배지는 "optimal"/"fastest"/"least_walk"/"cheapest" 같은 언어 중립 키로 반환한다
    (실제 화면 문구 번역은 프론트엔드 i18n 사전이 detected_language를 보고 처리).
    한 경로가 여러 기준에서 동시에 1위면 카드 하나에 배지를 합쳐서 보여주고,
    어떤 기준에서도 1위가 아닌 경로는 후보 목록에서 제외한다 (카드 난립 방지)."""
    if not paths:
        return []

    scored = []
    for idx, p in enumerate(paths):
        info = p.get("info", {})
        time_min  = info.get("totalTime", 0) or 0
        payment   = info.get("payment", 0) or 0
        walk_m    = info.get("totalWalk", 0) or 0
        transfers = (info.get("subwayTransitCount", 0) or 0) + (info.get("busTransitCount", 0) or 0)
        # 균형 점수: 1분 = 1점, 환승 1회 = 5점, 도보 100m = 1점 (가중치는 휴리스틱)
        balance_score = time_min + transfers * 5 + (walk_m / 100)
        scored.append({
            "idx": idx, "raw": p,
            "total_time_min": time_min, "payment": payment,
            "total_walk_m": walk_m, "transfer_count": transfers,
            "balance_score": balance_score,
        })

    winners = {
        "optimal":    min(scored, key=lambda s: s["balance_score"])["idx"],
        "fastest":    min(scored, key=lambda s: s["total_time_min"])["idx"],
        "least_walk": min(scored, key=lambda s: s["total_walk_m"])["idx"],
        "cheapest":   min(scored, key=lambda s: s["payment"])["idx"],
    }

    badge_map = {}
    for label, winner_idx in winners.items():
        badge_map.setdefault(winner_idx, []).append(label)

    options = []
    for s in scored:
        if s["idx"] not in badge_map:
            continue
        parsed = parse_odsay_path(s["raw"])
        options.append({
            "option_idx":      s["idx"],
            "badges":          badge_map[s["idx"]],
            "total_time_min":  s["total_time_min"],
            "payment":         s["payment"],
            "total_walk_m":    s["total_walk_m"],
            "transfer_count":  s["transfer_count"],
            "first_mode":      parsed.get("first_mode", ""),
            "first_line":      parsed.get("first_line", ""),
            "first_boarding_station": parsed.get("first_boarding_station", ""),
            "legs_summary": [
                {"mode": l["mode"], "label": l.get("line_name") or l.get("bus_no") or ""}
                for l in parsed.get("legs", []) if l["mode"] != "walk"
            ],
        })

    options.sort(key=lambda o: min(_BADGE_PRIORITY[b] for b in o["badges"]))
    return options


# pathType 참고: 1=지하철, 2=버스, 3=지하철+버스 (선택된 fastest가 어느 타입인지는
# parse_odsay_path가 subPath의 실제 trafficType들로부터 다시 판별하므로 무관하다)


_TRAFFIC_TYPE_SUBWAY = 1
_TRAFFIC_TYPE_BUS    = 2
_TRAFFIC_TYPE_WALK   = 3


def parse_odsay_path(path: dict) -> dict:
    """ODsay path 객체(subPath 배열)를 WAYVI 내부 경로 포맷으로 변환한다.
    여기서 만드는 모든 수치(시간/역명/방향/환승)는 ODsay 원본 데이터에서만 추출하며
    AI가 보강하거나 추정하지 않는다."""
    info = path.get("info", {})
    sub_paths = path.get("subPath", [])

    legs = []       # 지하철/버스 탑승 구간들 (steps 생성용)
    transfers = []  # 환승 정보
    prev_ride = None  # 직전 탑승 구간 (trafficType 1 or 2)

    for sp in sub_paths:
        ttype = sp.get("trafficType")

        if ttype == _TRAFFIC_TYPE_WALK:
            legs.append({
                "mode": "walk",
                "distance_m": sp.get("distance", 0),
                "time_min": sp.get("sectionTime", 0),
                "start_name": sp.get("startName", ""),
                "end_name": sp.get("endName", ""),
            })
            continue

        if ttype == _TRAFFIC_TYPE_SUBWAY:
            lane = sp.get("lane", {})
            if isinstance(lane, list):
                lane = lane[0] if lane else {}
            leg = {
                "mode": "subway",
                "line_name": lane.get("name", ""),
                "start_name": sp.get("startName", ""),
                "end_name": sp.get("endName", ""),
                "way": sp.get("way", ""),              # 진행방향 종착지 (표지판 기준)
                "station_count": sp.get("stationCount", 0),
                "time_min": sp.get("sectionTime", 0),
                "pass_stations": [
                    s.get("stationName", "")
                    for s in sp.get("passStopList", {}).get("stations", [])
                ],
            }
            # 탑승 직후 다음 정차역 = passStopList의 두 번째 역(첫 번째는 승차역 자신)
            if len(leg["pass_stations"]) >= 2:
                leg["next_station"] = leg["pass_stations"][1]
            else:
                leg["next_station"] = leg["end_name"]

            if prev_ride is not None:
                transfers.append({
                    "station":  sp.get("startName", ""),
                    "from_mode": prev_ride["mode"],
                    "from_line": prev_ride.get("line_name") or prev_ride.get("bus_no", ""),
                    "to_mode":  "subway",
                    "to_line":  leg["line_name"],
                    "direction": leg["way"],
                    "next_station_after_transfer": leg["next_station"],
                })
            legs.append(leg)
            prev_ride = leg
            continue

        if ttype == _TRAFFIC_TYPE_BUS:
            lanes = sp.get("lane", [])
            bus_no = lanes[0].get("busNo", "") if lanes else ""
            leg = {
                "mode": "bus",
                "bus_no": bus_no,
                "start_name": sp.get("startName", ""),
                "end_name": sp.get("endName", ""),
                "station_count": sp.get("stationCount", 0),
                "time_min": sp.get("sectionTime", 0),
            }
            if prev_ride is not None:
                transfers.append({
                    "station":  sp.get("startName", ""),
                    "from_mode": prev_ride["mode"],
                    "from_line": prev_ride.get("line_name") or prev_ride.get("bus_no", ""),
                    "to_mode":  "bus",
                    "to_line":  bus_no,
                    "direction": leg["end_name"],
                    "next_station_after_transfer": "",
                })
            legs.append(leg)
            prev_ride = leg
            continue

    first_ride = next((l for l in legs if l["mode"] in ("subway", "bus")), None)

    return {
        "total_time_min":     info.get("totalTime", 0),
        "payment":            info.get("payment", 0),
        "subway_transit_count": info.get("subwayTransitCount", 0),
        "bus_transit_count":    info.get("busTransitCount", 0),
        "total_distance_m":     info.get("totalDistance", 0),
        "first_line":         (first_ride.get("line_name") or first_ride.get("bus_no", "")) if first_ride else "",
        "first_mode":         first_ride.get("mode") if first_ride else "",
        "first_boarding_station": first_ride.get("start_name") if first_ride else "",
        "first_direction":    first_ride.get("way", "") if first_ride and first_ride["mode"] == "subway" else "",
        "first_next_station": first_ride.get("next_station", "") if first_ride else "",
        "legs":      legs,
        "transfers": transfers,
        "raw":       path,  # 디버깅/QA용 원본 보존
    }


# ── 서울시 지하철 실시간 도착정보 ────────────────────────
@app.get("/api/subway-route")
async def get_subway_route(start_station: str, end_station: str):
    url = f"http://swopenAPI.seoul.go.kr/api/subway/{SEOUL_METRO_KEY}/json/realtimeStationArrival/0/5/{end_station}"
    arrival_info = []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
        if resp.status_code == 200:
            data = resp.json()
            for row in data.get("realtimeArrivalList", [])[:3]:
                arrival_info.append({
                    "line":             row.get("subwayId"),
                    "train_no":         row.get("btrainNo"),
                    "arrival_message":  row.get("arvlMsg2"),
                    "next_station":     row.get("statnNm"),
                    "direction":        row.get("trainLineNm"),
                    "current_station":  row.get("arvlMsg3"),
                })
    except Exception:
        pass
    return {"start": start_station, "end": end_station, "realtime_arrivals": arrival_info}


# ── ODsay 길찾기 엔드포인트 (단독 호출용) ─────────────────
@app.get("/api/odsay-route")
async def get_odsay_route(ex: float, ey: float, sx: float = SEOUL_STATION_COORD["x"], sy: float = SEOUL_STATION_COORD["y"]):
    try:
        path = await fetch_odsay_route(sx, sy, ex, ey)
    except ODsayError as e:
        raise HTTPException(status_code=502, detail=e.message)
    return parse_odsay_path(path)


# ── ODsay 후보 경로 전체 비교 (검증/디버깅용) ─────────────
@app.get("/api/odsay-candidates")
async def get_odsay_candidates(ex: float, ey: float, sx: float = SEOUL_STATION_COORD["x"], sy: float = SEOUL_STATION_COORD["y"]):
    """ODsay가 반환하는 모든 후보 경로(OPT=1)의 총 소요시간을 나열한다.
    '가장 빠른 길이 맞는지' 직접 확인하고 싶을 때 이 엔드포인트로 검증할 수 있다."""
    if not ODSAY_API_KEY:
        raise HTTPException(status_code=400, detail="ODSAY_API_KEY가 설정되지 않았습니다.")

    url = "https://api.odsay.com/v1/api/searchPubTransPathT"
    params = {"apiKey": ODSAY_API_KEY, "SX": sx, "SY": sy, "EX": ex, "EY": ey, "OPT": 1, "SearchPathType": 0}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params)
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"ODsay 응답 오류 (status {resp.status_code})")

    data = resp.json()
    if "error" in data:
        raise HTTPException(status_code=502, detail=str(data["error"]))

    paths = data.get("result", {}).get("path", [])
    candidates = []
    for p in paths:
        info = p.get("info", {})
        modes = sorted({sp.get("trafficType") for sp in p.get("subPath", [])})
        mode_label = {1: "지하철", 2: "버스", 3: "도보"}
        candidates.append({
            "pathType": p.get("pathType"),  # 1=지하철, 2=버스, 3=지하철+버스
            "modes": [mode_label.get(m, str(m)) for m in modes if m != 3],
            "total_time_min": info.get("totalTime"),
            "payment": info.get("payment"),
            "subway_transit_count": info.get("subwayTransitCount"),
            "bus_transit_count": info.get("busTransitCount"),
            "total_walk_m": info.get("totalWalk"),
        })

    candidates_sorted = sorted(candidates, key=lambda c: c["total_time_min"] or float("inf"))
    return {
        "candidate_count": len(candidates),
        "candidates_by_time": candidates_sorted,
        "selected_by_navigate_endpoint": candidates_sorted[0] if candidates_sorted else None,
    }


# ── Gemini 경로 "설명" 생성 (사실 데이터는 전부 ODsay 원본) ─
# 핵심 원칙: 노선명/환승역/방향/다음역/소요시간 등 "사실(fact)"은
# ODsay가 계산한 odsay_route 그대로 사용한다. Gemini는 그 사실들을
# 사용자 언어로 자연스럽게 설명/번역하고, 인사말과 실용 팁만 덧붙인다.
# 즉 Gemini는 경로를 "생성"하지 않고 "통역·서술"만 한다.
@app.post("/api/generate-route")
async def generate_route(payload: dict):
    destination       = payload.get("destination", "")
    language          = payload.get("language", "en")
    current_location  = payload.get("current_location", "서울역")
    cultural_context  = payload.get("cultural_context", "")
    poi_info          = payload.get("poi_info", {})         # 한국관광공사 POI 실데이터
    odsay_route       = payload.get("odsay_route")           # ODsay 파싱 결과 (필수)

    if not odsay_route:
        raise HTTPException(status_code=400, detail="odsay_route가 필요합니다. /api/odsay-route 결과를 먼저 전달하세요.")

    lang_map = {
        "ko": "Korean", "en": "English", "ja": "Japanese",
        "zh": "Chinese (Simplified)", "es": "Spanish", "fr": "French",
        "auto": "Korean",
    }
    lang_name = lang_map.get(language, "English")

    poi_block = ""
    if poi_info:
        poi_block = f"""
═══ 한국관광공사 공식 POI 데이터 (REAL) ═══
- 공식 명칭 : {poi_info.get("title", destination)}
- 주소      : {poi_info.get("addr", "정보 없음")}
"""

    # ── ODsay 사실 데이터를 사람이 읽을 수 있게 정리해서 프롬프트에 주입 ──
    legs = odsay_route.get("legs", [])
    transfers = odsay_route.get("transfers", [])

    legs_lines = []
    for i, leg in enumerate(legs, 1):
        if leg["mode"] == "walk":
            legs_lines.append(f"  {i}. [도보] {leg.get('start_name','')} → {leg.get('end_name','')} (약 {leg.get('time_min',0)}분, {leg.get('distance_m',0)}m)")
        elif leg["mode"] == "subway":
            legs_lines.append(
                f"  {i}. [지하철] {leg.get('line_name','')} | 승차: {leg.get('start_name','')} → 하차: {leg.get('end_name','')} "
                f"| 진행방향(표지판 기준): {leg.get('way','')} 방면 | 다음 정차역: {leg.get('next_station','')} "
                f"| 정차역수: {leg.get('station_count',0)} | 소요시간: 약 {leg.get('time_min',0)}분"
            )
        elif leg["mode"] == "bus":
            legs_lines.append(
                f"  {i}. [버스] {leg.get('bus_no','')}번 | 승차: {leg.get('start_name','')} → 하차: {leg.get('end_name','')} "
                f"| 정류장수: {leg.get('station_count',0)} | 소요시간: 약 {leg.get('time_min',0)}분"
            )

    transfers_lines = []
    for i, t in enumerate(transfers, 1):
        transfers_lines.append(
            f"  환승{i}. {t.get('station','')}역에서 {t.get('from_line','')} → {t.get('to_line','')} 환승 "
            f"| 환승 후 방향: {t.get('direction','')} | 환승 후 다음역: {t.get('next_station_after_transfer','')}"
        )

    odsay_block = f"""
═══ ODsay 대중교통 길찾기 API 실데이터 (FACTS — 절대 변경 금지, 그대로 사용/번역만 할 것) ═══
총 소요시간 : 약 {odsay_route.get('total_time_min', 0)}분
총 요금     : {odsay_route.get('payment', 0)}원
환승 횟수   : 지하철 {odsay_route.get('subway_transit_count', 0)}회 / 버스 {odsay_route.get('bus_transit_count', 0)}회
첫 탑승 교통수단 : {odsay_route.get('first_mode','')}
첫 탑승역/정류장 : {odsay_route.get('first_boarding_station','')}
첫 구간 노선/버스번호 : {odsay_route.get('first_line','')}
첫 구간 진행방향(표지판 기준) : {odsay_route.get('first_direction','')}
첫 구간 다음 정차역 : {odsay_route.get('first_next_station','')}

[전체 구간 순서]
{chr(10).join(legs_lines) if legs_lines else '  (단일 구간, 환승 없음)'}

[환승 상세]
{chr(10).join(transfers_lines) if transfers_lines else '  (환승 없음 — 단일 노선/버스로 도착)'}
"""

    prompt = f"""You are WAYVI, a smart subway/bus navigation assistant for foreign tourists in Seoul, Korea.

Current location: {current_location}
Destination: {destination}
Cultural context: {cultural_context}
User's language: {lang_name}
{poi_block}{odsay_block}

═══ ABSOLUTE RULE ═══
The block above marked "FACTS" comes from a real, verified routing API (ODsay).
You MUST NOT invent, alter, omit, or "correct" any station name, line name, direction,
transfer station, next-station, time, or count. Every factual field in your output JSON
must be a direct translation/restatement of the FACTS block — never a guess.
Your job is ONLY to:
1) Translate/phrase these facts naturally in {lang_name}.
2) Write a warm 1-sentence greeting (may reference cultural context).
3) Write practical, encouraging tips for each step (e.g. "follow signs for X direction",
   general platform navigation advice) — tips are opinion/advice, not facts, so they may
   be generic if specific car-number/exit-number data isn't in the FACTS block.
4) If car number or exit number is not present in FACTS, leave that field as an empty string
   rather than inventing one.

Return ONLY valid JSON, no markdown backticks, no extra text:
{{
  "destination_name_local": "(destination name in {lang_name})",
  "greeting": "(warm 1-sentence greeting in {lang_name}, mention cultural context if relevant)",
  "total_time": "(from FACTS total_time_min, formatted naturally in {lang_name}, e.g. 'about 32 min')",
  "line": "(from FACTS first_line, translated/kept as-is)",
  "next_station": "(from FACTS first_next_station — do NOT change)",
  "board_at_car": "(leave empty string \\"\\" — not available from ODsay)",
  "fast_exit_door": "(leave empty string \\"\\" — not available from ODsay)",
  "transfer": [
    {{
      "station": "(from FACTS transfer station — do NOT change)",
      "from_line": "(from FACTS — do NOT change)",
      "to_line": "(from FACTS — do NOT change)",
      "direction": "(from FACTS transfer direction — do NOT change)",
      "next_station_after_transfer": "(from FACTS — do NOT change)",
      "walk_time": "(leave empty string \\"\\" unless estimable from distance)",
      "platform": "(leave empty string \\"\\" — not available from ODsay)",
      "tip": "(practical generic tip in {lang_name}, e.g. follow signs toward the line color/number)"
    }}
  ],
  "steps": [
    {{
      "step": 1,
      "action": "(short action label in {lang_name}, e.g. Board / Transfer / Get off / Walk)",
      "detail": "(specific instruction in {lang_name} built ONLY from the FACTS block — include line, direction, station names)",
      "tip": "(optional practical tip in {lang_name})"
    }}
  ],
  "arrival_tip": "(what to do/see upon arrival, in {lang_name})"
}}

Rules:
- "transfer" array must have exactly one entry per transfer listed in the FACTS block, in order. If FACTS shows no transfers, return [].
- "steps" must walk through every leg in [전체 구간 순서] in order (board → transfer if any → get off → walk if any), built only from FACTS.
- All text fields must be in {lang_name}.
- Never output a station, line, or direction name that does not appear in the FACTS block.
"""

    response = gemini_model.generate_content(prompt)
    raw = response.text.strip()
    raw = re.sub(r"```(?:json)?", "", raw).strip("`").strip()
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if not match:
        raise HTTPException(status_code=500, detail="경로 설명 생성 오류")

    result = json.loads(match.group())

    # ── 사실 필드 강제 동기화 ──────────────────────────────
    # Gemini가 그래도 사실을 살짝 바꿀 가능성에 대비해, 핵심 사실 필드는
    # ODsay 원본 값으로 다시 덮어써서 최종 출력의 정확성을 보장한다.
    result["total_time"] = f"약 {odsay_route.get('total_time_min', 0)}분"
    result["line"] = odsay_route.get("first_line", result.get("line", ""))
    result["next_station"] = odsay_route.get("first_next_station", result.get("next_station", ""))
    result["first_mode"] = odsay_route.get("first_mode", "subway")  # "subway" | "bus" — 화면 라벨/아이콘 분기용
    result["first_boarding_station"] = odsay_route.get("first_boarding_station", "")

    fixed_transfers = []
    ai_transfers = result.get("transfer", [])
    for i, t in enumerate(transfers):
        ai_tip = ai_transfers[i].get("tip", "") if i < len(ai_transfers) else ""
        fixed_transfers.append({
            "station": t.get("station", ""),
            "from_line": t.get("from_line", ""),
            "to_line": t.get("to_line", ""),
            "direction": t.get("direction", ""),
            "next_station_after_transfer": t.get("next_station_after_transfer", ""),
            "walk_time": "",
            "platform": "",
            "tip": ai_tip,
        })
    result["transfer"] = fixed_transfers
    result["board_at_car"] = ""     # ODsay는 호차 정보 미제공 — 환각 방지를 위해 빈 값 유지
    result["fast_exit_door"] = ""   # ODsay는 출구 정보 미제공 — 환각 방지를 위해 빈 값 유지

    return result


# ── 한국관광공사: 목적지 주변 추천 (위치기반 실데이터) ─────
@app.get("/api/nearby")
async def get_nearby(mapx: float, mapy: float, language: str = "en", exclude: str = ""):
    """목적지 좌표 반경 내 실제 관광지를 한국관광공사 위치기반 API로 조회하고,
    Gemini는 각 장소의 추천 이유 문구만 사용자 언어로 작성한다 (장소 자체는 생성하지 않음)."""
    url = "https://apis.data.go.kr/B551011/KorService2/locationBasedList2"
    params = {
        "serviceKey": TOUR_API_KEY,
        "MobileOS": "ETC",
        "MobileApp": "WAYVI",
        "_type": "json",
        "mapX": mapx,
        "mapY": mapy,
        "radius": 1500,        # 반경 1.5km
        "arrange": "E",        # 거리순 정렬
        "numOfRows": 8,
        "contentTypeId": 12,   # 관광지
    }
    items = []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
        if resp.status_code == 200:
            raw_items = (
                resp.json().get("response", {}).get("body", {})
                .get("items", {}).get("item", [])
            )
            if isinstance(raw_items, dict):
                raw_items = [raw_items]
            items = raw_items
    except Exception:
        pass

    # 검색 출발지(목적지) 자기 자신은 제외
    filtered = [it for it in items if it.get("title", "") != exclude][:5]

    if not filtered:
        return {"items": []}

    lang_map = {
        "ko": "Korean", "en": "English", "ja": "Japanese",
        "zh": "Chinese (Simplified)", "es": "Spanish", "fr": "French",
        "auto": "Korean",
    }
    lang_name = lang_map.get(language, "English")

    # 실제 장소 목록(이름/주소/거리)을 그대로 주고, 추천 이유 문구만 생성하게 한다.
    place_lines = []
    for i, it in enumerate(filtered, 1):
        dist_m = it.get("dist", "")
        try:
            dist_label = f"{round(float(dist_m))}m"
        except (TypeError, ValueError):
            dist_label = ""
        place_lines.append(f"{i}. {it.get('title','')} (주소: {it.get('addr1','')}, 거리: {dist_label})")

    prompt = f"""You are WAYVI, a tourist assistant. Below is a REAL list of nearby attractions
from Korea Tourism Organization's open data API (do NOT invent or remove any place):

{chr(10).join(place_lines)}

For EACH place above, write in {lang_name}:
- a short category label (역사/문화/음식/쇼핑/자연 style, translated)
- one short, appealing 1-sentence reason a tourist should visit

Return ONLY valid JSON, no backticks, no extra text, same order as the list above:
{{
  "items": [
    {{"name": "(exact place name from the list, translated to {lang_name} if helpful)", "category": "(in {lang_name})", "reason": "(1 sentence in {lang_name})"}}
  ]
}}
"""
    try:
        response = gemini_model.generate_content(prompt)
        raw = response.text.strip()
        raw = re.sub(r"```(?:json)?", "", raw).strip("`").strip()
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        ai_items = json.loads(match.group()).get("items", []) if match else []
    except Exception:
        ai_items = []

    results = []
    for i, it in enumerate(filtered):
        dist_m = it.get("dist", "")
        try:
            dist_label = f"도보 약 {round(float(dist_m) / 67)}분"  # 분속 약 67m 가정
        except (TypeError, ValueError):
            dist_label = ""
        ai = ai_items[i] if i < len(ai_items) else {}
        results.append({
            "name": it.get("title", ""),              # 실제 장소명 (관광공사 원본)
            "addr": it.get("addr1", ""),                # 실제 주소 (관광공사 원본)
            "distance": dist_label,                     # 실제 거리 기반 (관광공사 원본)
            "category": ai.get("category", ""),          # Gemini 작성 (번역/문구만)
            "reason": ai.get("reason", ""),               # Gemini 작성 (번역/문구만)
        })

    return {"items": results}



# ── QR 코드 생성 (모바일 웹 URL 삽입) ────────────────────
@app.post("/api/generate-qr")
async def generate_qr(payload: dict):
    route_data  = payload.get("route", {})
    route_id    = payload.get("route_id", str(uuid.uuid4()))
    base_url    = payload.get("base_url", "http://localhost:8000")

    # 경로 저장 (QR 스캔 시 조회용)
    route_store[route_id] = route_data

    # QR에는 모바일 웹 URL 삽입
    qr_url = f"{base_url}/route/{route_id}"

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=3,
    )
    qr.add_data(qr_url)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    return {"qr_base64": b64, "qr_url": qr_url, "route_id": route_id}


# ── 모바일 웹 — QR 스캔 시 열리는 경로 페이지 ───────────
@app.get("/route/{route_id}", response_class=HTMLResponse)
async def mobile_route_page(route_id: str):
    route = route_store.get(route_id)
    if not route:
        return HTMLResponse(
            "<h2 style='font-family:sans-serif;padding:24px'>경로 정보를 찾을 수 없습니다. 키오스크에서 다시 검색해주세요.</h2>",
            status_code=404,
        )

    # 첫 구간 교통수단에 따른 라벨/아이콘 분기 (지하철/버스)
    first_mode = route.get("first_mode", "subway")
    if first_mode == "bus":
        line_icon, line_label = "🚌", "탑승 버스"
        direction_label = "▶ 하차 정류장"
    else:
        line_icon, line_label = "🚇", "탑승 노선"
        direction_label = "▶ 방향 (다음역)"

    # 스텝 HTML
    steps_html = ""
    for i, s in enumerate(route.get("steps", []), 1):
        tip_html = f'<div class="tip">💡 {s.get("tip","")}</div>' if s.get("tip") else ""
        is_transfer = (
            "환승" in (s.get("action","") + s.get("detail",""))
            or "transfer" in (s.get("action","") + s.get("detail","")).lower()
        )
        num_class = "step-num transfer" if is_transfer else "step-num"
        steps_html += f"""
        <div class="step">
          <div class="{num_class}">{i}</div>
          <div class="step-body">
            <div class="step-action">{s.get("action","")}</div>
            <div class="step-detail">{s.get("detail","")}</div>
            {tip_html}
          </div>
        </div>"""

    # 환승 카드 HTML
    transfers = route.get("transfer", [])
    transfer_html = ""
    if transfers:
        transfer_html = f'<div class="section-title">🔄 환승 정보 ({len(transfers)}회)</div>'
        for i, t in enumerate(transfers, 1):
            tip = f'<div class="t-tip">💡 {t.get("tip","")}</div>' if t.get("tip") else ""
            platform = f'<div class="t-row">플랫폼: <b>{t.get("platform","")}</b></div>' if t.get("platform") else ""
            transfer_html += f"""
            <div class="t-card">
              <div class="t-title">환승 {i} · {t.get("station","—")}</div>
              <div class="t-row"><b>{t.get("from_line","—")}</b> → <b>{t.get("to_line","—")}</b>
                {(' · 도보 ' + t.get('walk_time','')) if t.get('walk_time') else ''}
              </div>
              {(f'<div class="t-row">방향: <b>{t.get("direction","")}</b></div>') if t.get("direction") else ""}
              {(f'<div class="t-row">다음 정차역: <b>{t.get("next_station_after_transfer","")}</b></div>') if t.get("next_station_after_transfer") else ""}
              {platform}
              {tip}
            </div>"""

    nearby_recs = route.get("nearby_recommendations", [])
    nearby_html = ""
    if nearby_recs:
        rows = ""
        for i, r in enumerate(nearby_recs[:5], 1):
            rows += f"""<tr>
              <td style="color:#9c8c72;font-size:0.75rem">{i}</td>
              <td>
                <span style="font-weight:700;color:#f0e6d0">{r.get("name","—")}</span>
                {f'<span style="font-size:0.68rem;color:#c9993a;margin-left:4px">{r.get("category","")}</span>' if r.get("category") else ""}
                {f'<div style="font-size:0.75rem;color:#9c8c72;margin-top:2px">{r.get("reason","")}</div>' if r.get("reason") else ""}
              </td>
              <td style="font-size:0.73rem;color:#8E9BA8;white-space:nowrap;font-family:monospace">{r.get("distance","—")}</td>
            </tr>"""
        nearby_html = f"""
        <div class="section-title">주변 추천</div>
        <table style="width:100%;border-collapse:collapse;font-size:0.8rem;margin-bottom:16px">
          <thead><tr>
            <th style="padding:6px 8px;text-align:left;font-size:0.58rem;color:#9c8c72;border-bottom:1px solid rgba(201,153,58,0.2);letter-spacing:0.1em">#</th>
            <th style="padding:6px 8px;text-align:left;font-size:0.58rem;color:#9c8c72;border-bottom:1px solid rgba(201,153,58,0.2);letter-spacing:0.1em">장소</th>
            <th style="padding:6px 8px;text-align:left;font-size:0.58rem;color:#9c8c72;border-bottom:1px solid rgba(201,153,58,0.2);letter-spacing:0.1em">거리</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>"""

    # 환승 테이블 HTML
    transfers = route.get("transfer", [])
    transfer_html = ""
    if transfers:
        t_rows = ""
        for i, t in enumerate(transfers, 1):
            t_rows += f"""<tr>
              <td style="padding:8px;font-size:0.7rem;color:#c9993a;font-weight:700">{i}</td>
              <td style="padding:8px;font-weight:700;color:#f0e6d0">{t.get("station","—")}</td>
              <td style="padding:8px;font-family:monospace;font-size:0.8rem;color:#f0e6d0">{t.get("from_line","—")} → {t.get("to_line","—")}</td>
              <td style="padding:8px;font-size:0.78rem;color:#9c8c72">
                {t.get("direction","—")}
                {f'<div style="color:#8E9BA8;font-size:0.72rem">▶ {t.get("next_station_after_transfer","")}</div>' if t.get("next_station_after_transfer") else ""}
              </td>
              <td style="padding:8px;font-size:0.75rem;color:#9c8c72">{t.get("walk_time","—")}</td>
            </tr>"""
        transfer_html = f"""
        <div class="section-title">🔄 환승 ({len(transfers)}회)</div>
        <table style="width:100%;border-collapse:collapse;font-size:0.8rem;margin-bottom:16px">
          <thead><tr>
            {''.join(f'<th style="padding:6px 8px;text-align:left;font-size:0.58rem;color:#9c8c72;border-bottom:1px solid rgba(201,153,58,0.2);letter-spacing:0.1em">{h}</th>' for h in ['#','환승역','노선','방면','도보'])}
          </tr></thead>
          <tbody>{t_rows}</tbody>
        </table>
        {''.join(f'<div class="t-tip" style="font-size:0.75rem;color:#2e7d6b;margin-bottom:6px">↳ 환승{i+1} {t.get("tip","")}</div>' for i,t in enumerate(transfers) if t.get("tip"))}
        """

    arrival = f'<div class="arrival-tip">🏁 {route.get("arrival_tip","")}</div>' if route.get("arrival_tip") else ""
    greeting = f'<div class="greeting">{route.get("greeting","")}</div>' if route.get("greeting") else ""

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WAYVI — {route.get("destination_name_local","Route")}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+KR:wght@700;900&family=Noto+Sans+KR:wght@300;400;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #1a1510; --surface: #231c14; --surface2: #2d2318;
    --red: #c0392b; --gold: #c9993a; --teal: #2e7d6b; --blue: #1a4f7a;
    --text: #f0e6d0; --dim: #9c8c72;
    --border: rgba(201,153,58,0.15); --border-b: rgba(201,153,58,0.35);
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg); color: var(--text);
    font-family: 'Noto Sans KR', sans-serif;
    padding: 20px; max-width: 480px; margin: 0 auto;
  }}
  .header-bar {{
    height: 3px; margin: -20px -20px 16px;
    background: linear-gradient(90deg, var(--red), var(--gold), var(--teal), var(--blue), var(--gold), var(--red));
  }}
  .logo {{
    font-family: 'Noto Serif KR', serif;
    font-size: 1.3rem; font-weight: 900;
    background: linear-gradient(135deg, var(--text) 40%, var(--gold));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
    margin-bottom: 2px;
  }}
  .logo-sub {{ font-size: 0.6rem; color: var(--dim); letter-spacing: 0.15em; text-transform: uppercase; margin-bottom: 12px; }}
  .dest {{
    font-family: 'Noto Serif KR', serif;
    font-size: 1.7rem; font-weight: 900; margin: 12px 0 4px; color: var(--text);
  }}
  .greeting {{ font-size: 0.85rem; color: #5baa8e; margin-bottom: 16px; line-height: 1.6; font-style: italic; }}
  .info-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 20px; }}
  .info-box {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 2px; padding: 11px; text-align: center;
  }}
  .info-label {{ font-size: 0.58rem; color: var(--dim); text-transform: uppercase; letter-spacing: 0.12em; margin-bottom: 4px; font-weight: 700; }}
  .info-value {{ font-size: 0.9rem; font-weight: 700; }}
  .info-value.warn {{ color: var(--gold); }}
  .section-title {{
    font-size: 0.6rem; font-weight: 700; letter-spacing: 0.2em;
    text-transform: uppercase; color: var(--gold);
    margin-bottom: 10px; display: flex; align-items: center; gap: 8px;
  }}
  .section-title::after {{ content: ''; flex: 1; height: 1px; background: var(--border); }}
  /* 환승 카드 */
  .t-card {{
    background: var(--surface2); border: 1px solid var(--border);
    border-left: 3px solid var(--gold);
    border-radius: 2px; padding: 11px 13px; margin-bottom: 8px; font-size: 0.82rem;
  }}
  .t-title {{ font-weight: 700; color: var(--gold); margin-bottom: 5px; font-size: 0.8rem; }}
  .t-row {{ color: var(--dim); margin-bottom: 3px; line-height: 1.5; }}
  .t-row b {{ color: var(--text); }}
  .t-tip {{ font-size: 0.75rem; color: var(--teal); margin-top: 4px; }}
  /* 스텝 */
  .steps {{ display: flex; flex-direction: column; gap: 0; margin-bottom: 20px; }}
  .step {{ display: flex; gap: 12px; position: relative; }}
  .step:not(:last-child)::after {{
    content: ''; position: absolute; left: 13px; top: 30px; bottom: 0;
    width: 1px; background: var(--border);
  }}
  .step-num {{
    width: 28px; height: 28px; border-radius: 50%;
    background: var(--red); color: white;
    font-size: 0.72rem; font-weight: 700;
    display: flex; align-items: center; justify-content: center;
    flex-shrink: 0; margin-top: 2px;
    font-family: 'Noto Serif KR', serif;
  }}
  .step-num.transfer {{ background: var(--gold); color: var(--bg); }}
  .step-body {{ flex: 1; padding-bottom: 16px; }}
  .step-action {{ font-size: 0.6rem; color: var(--dim); text-transform: uppercase; letter-spacing: 0.12em; margin-bottom: 3px; font-weight: 700; }}
  .step-detail {{ font-size: 0.88rem; font-weight: 500; line-height: 1.6; }}
  .tip {{ font-size: 0.75rem; color: var(--teal); margin-top: 4px; padding: 3px 9px; background: rgba(46,125,107,0.07); border-left: 2px solid var(--teal); }}
  .nearby, .arrival-tip {{
    background: var(--surface); border-radius: 2px;
    padding: 11px 14px; font-size: 0.8rem; color: var(--dim);
    line-height: 1.6; margin-bottom: 10px;
    border-left: 3px solid var(--blue);
  }}
  .save-btn {{
    width: 100%; background: var(--red); color: white;
    border: none; border-radius: 2px; padding: 14px;
    font-size: 0.95rem; font-weight: 700; cursor: pointer; margin-top: 16px;
    font-family: 'Noto Sans KR', sans-serif; letter-spacing: 0.05em;
  }}
  .footer {{
    text-align: center; font-size: 0.62rem; color: var(--dim);
    margin-top: 24px; padding-top: 16px; border-top: 1px solid var(--border);
  }}
</style>
</head>
<body>
  <div class="header-bar"></div>
  <div class="logo">WAYVI</div>
  <div class="logo-sub">서울 스마트 교통 안내</div>
  <div class="dest">{route.get("destination_name_local","")}</div>
  {greeting}
  <div class="info-row">
    <div class="info-box">
      <div class="info-label">⏱ 소요시간</div>
      <div class="info-value">{route.get("total_time","—")}</div>
    </div>
    <div class="info-box">
      <div class="info-label">{line_icon} {line_label}</div>
      <div class="info-value">{route.get("line","—")}</div>
    </div>
    <div class="info-box" style="grid-column:span 2">
      <div class="info-label">{direction_label}</div>
      <div class="info-value" style="font-size:0.82rem">{route.get("next_station","—")}</div>
    </div>
    {f'''<div class="info-box">
      <div class="info-label">🚋 탑승 호차</div>
      <div class="info-value warn">{route.get("board_at_car","")}</div>
    </div>''' if route.get("board_at_car") else ""}
    {f'''<div class="info-box" style="grid-column:span 2">
      <div class="info-label">🚪 빠른 하차 출구</div>
      <div class="info-value warn">{route.get("fast_exit_door","")}</div>
    </div>''' if route.get("fast_exit_door") else ""}
  </div>
  {transfer_html}
  <div class="section-title">단계별 경로 안내</div>
  <div class="steps">{steps_html}</div>
  {arrival}
  {nearby_html}
  <button class="save-btn" onclick="window.print()">🖨 저장 / 인쇄</button>
  <div class="footer">WAYVI · 외국인 관광객을 위한 스마트 교통 안내 키오스크</div>
</body>
</html>""")


# ── 역명으로 실시간 도착정보 직접 조회 (AI 미개입) ──────
async def fetch_realtime_arrivals(station_name: str) -> list:
    """서울시 실시간 도착정보 API → 원본 그대로 반환 (AI 미개입)"""
    url = f"http://swopenAPI.seoul.go.kr/api/subway/{SEOUL_METRO_KEY}/json/realtimeStationArrival/0/5/{station_name}"
    arrivals = []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
        if resp.status_code == 200:
            for row in resp.json().get("realtimeArrivalList", [])[:5]:
                arrivals.append({
                    "line":            row.get("subwayId", ""),
                    "direction":       row.get("trainLineNm", ""),   # "성수 방면"
                    "arrival_message": row.get("arvlMsg2", ""),      # "2분 후 도착"
                    "current_pos":     row.get("arvlMsg3", ""),      # "성수 출발"
                    "train_no":        row.get("btrainNo", ""),
                })
    except Exception:
        pass
    return arrivals


# ── 통합 파이프라인 ───────────────────────────────────────
@app.post("/api/navigate")
async def navigate(payload: dict):
    text             = payload.get("text", "")
    language         = payload.get("language", "en")
    current_location = payload.get("current_location", "서울역")
    base_url         = payload.get("base_url", "http://localhost:8000")

    # 1. 목적지 추출 (언어 자동 감지 포함, Gemini)
    dest_result        = await extract_destination({"text": text, "language": language})
    destination_korean = dest_result.get("destination_korean", "")
    cultural_context    = dest_result.get("cultural_context", "")
    candidates          = dest_result.get("candidates", [])
    detected_language   = dest_result.get("detected_language", language) or language

    if dest_result.get("confidence", 0) < 0.7 and candidates:
        return {
            "status":     "clarification_needed",
            "candidates": candidates,
            "message":    "Did you mean one of these?",
        }

    # 2. POI 조회 (한국관광공사) — 목적지 좌표(mapx/mapy) 확보가 핵심
    poi_result = await get_poi(destination_korean)
    poi_items  = poi_result.get("items", [])
    poi_info   = poi_items[0] if poi_items else {}

    if not poi_info or not poi_info.get("mapx") or not poi_info.get("mapy"):
        return {
            "status":  "error",
            "message": f"'{destination_korean}'의 위치 정보를 한국관광공사 데이터에서 찾을 수 없습니다. 다른 표현으로 다시 말씀해주세요.",
        }

    dest_x = float(poi_info["mapx"])
    dest_y = float(poi_info["mapy"])

    # 3. ODsay 후보 경로 전체 조회 — 서울역(출발지 고정) → 목적지 좌표
    #    AI는 절대 경로를 만들어내지 않는다. 여기서 실패하면 즉시 중단.
    try:
        raw_paths = await fetch_odsay_candidates_raw(
            SEOUL_STATION_COORD["x"], SEOUL_STATION_COORD["y"], dest_x, dest_y
        )
    except ODsayError as e:
        return {"status": "error", "message": f"경로를 찾을 수 없습니다: {e.message}"}

    options = categorize_odsay_paths(raw_paths)

    nav_ctx = {
        "destination_korean": destination_korean,
        "cultural_context":   cultural_context,
        "detected_language":  detected_language,
        "current_location":   current_location,
        "poi_info":           poi_info,
        "base_url":           base_url,
        "raw_paths":          raw_paths,
    }

    # 후보가 2개 이상(즉 실제로 선택지가 있을 때)만 사용자에게 고르게 한다.
    # (전부 같은 경로로 수렴하면 굳이 고르라고 안 하고 바로 진행)
    if len(options) > 1:
        nav_id = str(uuid.uuid4())
        nav_session_store[nav_id] = nav_ctx
        return {
            "status":             "route_selection_needed",
            "nav_id":             nav_id,
            "destination_korean": destination_korean,
            "cultural_context":   cultural_context,
            "language":           detected_language,
            "options":            options,
        }

    chosen_path = raw_paths[options[0]["option_idx"]] if options else raw_paths[0]
    return await _finalize_navigation(nav_ctx, chosen_path)


# ── 경로 선택 후 마무리 (Gemini 설명 생성 + 실시간 + 주변추천 + QR) ─
async def _finalize_navigation(ctx: dict, chosen_path: dict) -> dict:
    """사용자가 (또는 옵션이 1개뿐이라 자동으로) 고른 ODsay 경로 하나를 받아
    나머지 파이프라인(4~7단계)을 마무리한다. /api/navigate 와
    /api/navigate/select-route 양쪽에서 공유하는 로직이다."""
    destination_korean = ctx["destination_korean"]
    cultural_context    = ctx["cultural_context"]
    detected_language   = ctx["detected_language"]
    current_location    = ctx["current_location"]
    poi_info            = ctx["poi_info"]
    base_url            = ctx["base_url"]

    dest_x = float(poi_info["mapx"])
    dest_y = float(poi_info["mapy"])

    odsay_route = parse_odsay_path(chosen_path)

    # 4. Gemini는 ODsay 사실 데이터를 사용자 언어로 "설명"만 생성
    route = await generate_route({
        "destination":      destination_korean,
        "language":         detected_language,
        "current_location": current_location,
        "poi_info":         poi_info,
        "cultural_context": cultural_context,
        "odsay_route":      odsay_route,
    })

    # 5. 실시간 도착정보 — 서울시 API 원본 직접 조회 (AI 미개입), 보조정보로만 사용
    departure_station = _resolve_station_name(current_location, "")
    arrival_station    = _resolve_station_name(destination_korean, poi_info.get("addr", ""))
    transfer_stations  = [t.get("station", "") for t in route.get("transfer", []) if t.get("station")]

    import asyncio
    station_targets   = [departure_station] + transfer_stations + [arrival_station]
    realtime_tasks    = [fetch_realtime_arrivals(s) for s in station_targets]
    realtime_results  = await asyncio.gather(*realtime_tasks)

    realtime_map = {}
    for station, arrivals in zip(station_targets, realtime_results):
        if arrivals:
            realtime_map[station] = arrivals

    # 6. 목적지 주변 추천 5곳 — 한국관광공사 위치기반 API 실데이터
    nearby_result = await get_nearby(
        mapx=dest_x, mapy=dest_y, language=detected_language, exclude=poi_info.get("title", "")
    )
    route["nearby_recommendations"] = nearby_result.get("items", [])

    # 7. QR 생성
    route_id  = str(uuid.uuid4())
    qr_result = await generate_qr({"route": route, "route_id": route_id, "base_url": base_url})

    return {
        "status":             "success",
        "destination_korean": destination_korean,
        "cultural_context":   cultural_context,
        "poi":                poi_info,
        "route":              route,
        "odsay_summary": {     # 디버깅/투명성용 — ODsay 핵심 사실 노출
            "total_time_min": odsay_route.get("total_time_min"),
            "payment":        odsay_route.get("payment"),
            "subway_transit_count": odsay_route.get("subway_transit_count"),
            "bus_transit_count":    odsay_route.get("bus_transit_count"),
        },
        "realtime":           realtime_map,   # ← 화면에서 직접 표시
        "qr_base64":          qr_result["qr_base64"],
        "qr_url":             qr_result["qr_url"],
        "route_id":           route_id,
        "language":           detected_language,
    }


@app.post("/api/navigate/select-route")
async def navigate_select_route(payload: dict):
    """/api/navigate 가 route_selection_needed 를 반환했을 때, 사용자가 고른
    옵션(option_idx)을 받아 나머지 파이프라인을 마무리한다."""
    nav_id      = payload.get("nav_id", "")
    option_idx  = payload.get("option_idx")

    ctx = nav_session_store.pop(nav_id, None)
    if ctx is None:
        raise HTTPException(
            status_code=400,
            detail="세션이 만료되었거나 잘못된 요청입니다. 처음부터 다시 검색해주세요.",
        )

    raw_paths = ctx["raw_paths"]
    if not isinstance(option_idx, int) or not (0 <= option_idx < len(raw_paths)):
        raise HTTPException(status_code=400, detail="잘못된 경로 선택입니다.")

    chosen_path = raw_paths[option_idx]
    return await _finalize_navigation(ctx, chosen_path)



# ── 정적 파일 & HTML ──────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def root():
    with open("templates/index.html", "r", encoding="utf-8") as f:
        return f.read()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)