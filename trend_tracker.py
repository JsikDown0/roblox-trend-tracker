#!/usr/bin/env python3
"""
로블록스 트렌드 트래커 v2

3시간마다 로블록스 탐색(홈) 화면의 정렬 목록과 각 게임의 동접·방문수를 기록하고,
하루 한 번(기본 오전 9시대) 전날 하루치 데이터를 이전 날들과 비교해 디스코드로 리포트를 보냅니다.

v2에서 보완한 점
  - 하루 여러 번 측정 → 하루 평균/최고 동접으로 비교 (시간대 흔들림 감소)
  - 시간대 영향이 없는 '하루 방문 증가량'을 주 지표로 사용
  - 2일 이상 연속 상승한 게임만 급상승으로 표시, 지난주 같은 요일과도 비교
  - 한 번 차트에 오른 게임은 30일간 계속 추적(관심 목록) → 뜨고 지는 흐름이 기록으로 남음
  - 스폰서(광고) 정렬에 나온 게임 💰, AFK로 동접을 부풀렸을 가능성이 있는 게임 ⚠️ 표시
  - 전체·PC 기준 외에 한국·모바일 기준 목록도 수집
  - 클론 신호 키워드를 로블록스 검색으로 한 번 더 확인

외부 라이브러리 없이 파이썬 표준 라이브러리만 사용합니다.

사용법:
    python trend_tracker.py                 # 수집 (+ 보낼 시간이면 리포트 전송)
    python trend_tracker.py --force-report  # 수집 후 무조건 리포트 전송
    python trend_tracker.py --dry-run       # 디스코드 대신 화면에 출력 (항상 리포트 생성)
    python trend_tracker.py --report-only   # 수집 없이 저장된 기록으로 리포트만
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ──────────────────────────────────────────────────────────────
# 설정 (환경변수로 바꿀 수 있음)
# ──────────────────────────────────────────────────────────────
KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DAILY_DIR = DATA_DIR / "daily"
WATCHLIST_PATH = DATA_DIR / "watchlist.json"
META_PATH = DATA_DIR / "meta.json"

MIN_CCU = int(os.getenv("MIN_CCU", "300"))             # 급상승 후보 최소 평균 동접
NEW_GAME_DAYS = int(os.getenv("NEW_GAME_DAYS", "60"))  # 신작 기준 (출시 후 일수)
TOP_N = int(os.getenv("TOP_N", "10"))                  # 섹션별 표시 개수
REPORT_HOUR = int(os.getenv("REPORT_HOUR", "9"))       # 이 시각(KST) 이후 첫 실행에서 리포트 전송
WATCH_DAYS = int(os.getenv("WATCH_DAYS", "30"))        # 차트에서 빠진 뒤에도 추적하는 일수
WATCH_MAX = int(os.getenv("WATCH_MAX", "5000"))        # 관심 목록 최대 크기
MAX_SORTS = int(os.getenv("MAX_SORTS", "40"))          # 기준별 수집할 정렬 최대 개수
KW_YOUNG_DAYS = int(os.getenv("KW_YOUNG_DAYS", "90"))  # 클론 신호: 이 일수 이내 출시작 이름만 봄
LOOKBACK_DAYS = 8                                      # 비교용으로 불러올 과거 일수
AFK_VISITS_PER_CCU = float(os.getenv("AFK_VISITS_PER_CCU", "8"))  # 이보다 낮으면 ⚠️

# 수집 기준: "국가:기기" 쉼표 구분. 첫 번째가 기본 기준.
VARIANTS_RAW = os.getenv("VARIANTS", "all:computer,kr:phone")
VARIANT_LABELS = {"all:computer": "전체·PC", "kr:phone": "한국·모바일", "all:phone": "전체·모바일", "kr:computer": "한국·PC"}

FEATURED_SORT_KEYWORDS = ["trending", "up-and-coming", "up and coming"]
SPONSORED_SORT_KEYWORDS = ["sponsor"]
SKIP_SORT_KEYWORDS = ["continue", "favorite", "friends", "recommended-for-you", "recently"]

EXPLORE = "https://apis.roblox.com/explore-api/v1"
SEARCH = "https://apis.roblox.com/search-api/omni-search"
GAMES_API = "https://games.roblox.com/v1/games"
DEVICE_EXTRA = {"cpuCores": "8", "maxResolution": "1920x1080", "maxMemory": "8192", "networkType": "wifi"}
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) roblox-trend-tracker/2.0"


def parse_variants(raw: str) -> list[str]:
    out = []
    for part in raw.split(","):
        part = part.strip().lower()
        if ":" in part:
            out.append(part)
    return out or ["all:computer"]


VARIANTS = parse_variants(VARIANTS_RAW)


def vlabel(vk: str) -> str:
    return VARIANT_LABELS.get(vk, vk)


# ──────────────────────────────────────────────────────────────
# HTTP
# ──────────────────────────────────────────────────────────────
def http_get_json(url: str, params: dict | None = None, retries: int = 4):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Language": "en-US,en;q=0.9"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            if e.code == 429 or e.code >= 500:
                time.sleep(3 * (attempt + 1))
                continue
            body = e.read().decode("utf-8", "ignore")[:300]
            raise RuntimeError(f"{url.split('?')[0]} → HTTP {e.code}: {body}") from e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = str(e)
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"{url.split('?')[0]} 요청 실패 ({last_err})")


def http_post_json(url: str, payload: dict, retries: int = 4):
    data = json.dumps(payload).encode("utf-8")
    for _ in range(retries):
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    wait = float(json.loads(e.read().decode()).get("retry_after", 2))
                except Exception:
                    wait = 2
                time.sleep(wait + 0.5)
                continue
            body = e.read().decode("utf-8", "ignore")[:300]
            raise RuntimeError(f"디스코드 전송 실패 HTTP {e.code}: {body}") from e
    raise RuntimeError("디스코드 전송 실패 (속도 제한 반복)")


# ──────────────────────────────────────────────────────────────
# 수집
# ──────────────────────────────────────────────────────────────
def device_params(vk: str) -> dict:
    country, device = vk.split(":", 1)
    return {"device": device, "country": country, **DEVICE_EXTRA}


def is_game_sort(sort: dict) -> bool:
    ctype = str(sort.get("contentType", "Games")).lower()
    if ctype and ctype not in ("games", "game"):
        return False
    sid = str(sort.get("sortId", "")).lower()
    return bool(sid) and not any(k in sid for k in SKIP_SORT_KEYWORDS)


def sort_display_name(sort: dict) -> str:
    for key in ("sortDisplayName", "topic", "sortTitle", "title"):
        if sort.get(key):
            return str(sort[key])
    return str(sort.get("sortId"))


def is_sponsored(sid: str, name: str) -> bool:
    s = f"{sid} {name}".lower()
    return any(k in s for k in SPONSORED_SORT_KEYWORDS)


def fetch_sorts(session_id: str, vk: str) -> list[dict]:
    sorts, token = [], None
    for _ in range(6):
        params = {"sessionId": session_id, **device_params(vk)}
        if token:
            params["sortsPageToken"] = token
        data = http_get_json(f"{EXPLORE}/get-sorts", params)
        sorts.extend(data.get("sorts", []) or [])
        token = data.get("nextSortsPageToken")
        if not token:
            break
    return sorts


def fetch_sort_games(session_id: str, vk: str, sort_id: str) -> list[dict]:
    params = {"sessionId": session_id, "sortId": sort_id, **device_params(vk)}
    data = http_get_json(f"{EXPLORE}/get-sort-content", params)
    return data.get("games", []) or []


def fetch_game_details(universe_ids: list[int]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for i in range(0, len(universe_ids), 50):
        chunk = universe_ids[i : i + 50]
        try:
            data = http_get_json(GAMES_API, {"universeIds": ",".join(map(str, chunk))})
            for g in data.get("data", []) or []:
                out[int(g["id"])] = g
        except RuntimeError as e:
            print(f"[경고] 게임 상세 조회 일부 실패: {e}", file=sys.stderr)
        time.sleep(0.3)
    return out


def collect_run(now: datetime, watch_ids: list[str]) -> dict:
    """한 번의 측정. 정렬 목록(기준별) + 차트 게임과 관심 목록 게임의 현재 수치."""
    session_id = str(uuid.uuid4())
    variants: dict[str, dict] = {}
    games: dict[str, dict] = {}
    errors = []

    for vk in VARIANTS:
        try:
            raw_sorts = fetch_sorts(session_id, vk)
        except RuntimeError as e:
            errors.append(f"{vlabel(vk)} 정렬 목록: {e}")
            continue
        sorts = [s for s in raw_sorts if is_game_sort(s)][:MAX_SORTS]
        vsorts = {}
        for s in sorts:
            sid = str(s["sortId"])
            name = sort_display_name(s)
            try:
                sort_games = fetch_sort_games(session_id, vk, sid)
            except RuntimeError as e:
                print(f"[경고] {vk}/{sid} 조회 실패, 기본 목록 사용: {e}", file=sys.stderr)
                sort_games = s.get("games", []) or []
            time.sleep(0.25)
            ids = []
            for g in sort_games:
                uid = g.get("universeId")
                if not uid:
                    continue
                uid = str(int(uid))
                ids.append(uid)
                rec = games.setdefault(uid, {})
                rec.setdefault("name", g.get("name", ""))
                rec.setdefault("place", g.get("rootPlaceId"))
                if g.get("playerCount") is not None:
                    rec["playing"] = int(g["playerCount"])
            if ids:
                vsorts[sid] = {"name": name, "ids": ids, "sponsored": is_sponsored(sid, name)}
        if vsorts:
            variants[vk] = vsorts

    if not variants:
        raise RuntimeError("로블록스 탐색 정렬을 하나도 못 가져왔어요. API 구조가 바뀌었을 수 있어요. " + " / ".join(errors))

    all_ids = sorted(set(games) | set(watch_ids), key=int)
    details = fetch_game_details([int(u) for u in all_ids])
    for uid_int, d in details.items():
        uid = str(uid_int)
        rec = games.setdefault(uid, {})
        rec["name"] = d.get("name") or rec.get("name", "")
        rec["place"] = d.get("rootPlaceId") or rec.get("place")
        if d.get("playing") is not None:
            rec["playing"] = int(d["playing"])
        rec["visits"] = int(d.get("visits") or 0)
        rec["created"] = (d.get("created") or "")[:10]
        rec["fav"] = int(d.get("favoritedCount") or 0)

    return {"ts": int(now.timestamp()), "hhmm": now.strftime("%H:%M"), "variants": variants, "games": games}


# ──────────────────────────────────────────────────────────────
# 저장 구조
#   data/daily/YYYY-MM-DD.json : 그날의 모든 측정을 게임별로 합친 기록
#   data/watchlist.json        : 추적 중인 게임 {id: {added, last_charted, name}}
#   data/meta.json             : 첫 기록 날짜 등
# ──────────────────────────────────────────────────────────────
def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def daily_path(d: str) -> Path:
    return DAILY_DIR / f"{d}.json"


def load_daily(d: str) -> dict | None:
    return read_json(daily_path(d), None)


def merge_run(daily: dict, run: dict) -> dict:
    daily.setdefault("runs", [])
    if run["hhmm"] not in daily["runs"]:
        daily["runs"].append(run["hhmm"])
    G = daily.setdefault("games", {})

    # 이번 측정에서 각 게임의 정렬 순위 (기준:정렬 → 순위)
    ranks: dict[str, dict[str, int]] = defaultdict(dict)
    sponsored = set()
    for vk, vsorts in run["variants"].items():
        for sid, s in vsorts.items():
            for i, uid in enumerate(s["ids"], start=1):
                ranks[uid][f"{vk}|{sid}"] = i
                if s["sponsored"]:
                    sponsored.add(uid)

    for uid, g in run["games"].items():
        rec = G.setdefault(uid, {"s": [], "r": {}})
        rec["n"] = g.get("name") or rec.get("n", "")
        if g.get("place"):
            rec["p"] = g["place"]
        if g.get("created"):
            rec["c"] = g["created"]
        if g.get("fav") is not None:
            rec["f"] = g["fav"]
        if g.get("playing") is not None:
            rec["s"].append([run["hhmm"], g["playing"]])
        if g.get("visits"):
            v = [run["ts"], g["visits"]]
            if "v0" not in rec:
                rec["v0"] = v
            rec["v1"] = v
        for key, rk in ranks.get(uid, {}).items():
            if key not in rec["r"] or rk < rec["r"][key]:
                rec["r"][key] = rk
        if uid in sponsored:
            rec["sp"] = 1

    daily["latest"] = {
        vk: {sid: {"name": s["name"], "ids": s["ids"][:100], "sponsored": s["sponsored"]} for sid, s in vs.items()}
        for vk, vs in run["variants"].items()
    }
    daily["latest_at"] = run["hhmm"]
    return daily


def update_watchlist(watch: dict, run: dict, today: str) -> dict:
    charted = set()
    for vs in run["variants"].values():
        for s in vs.values():
            charted.update(s["ids"])
    for uid in charted:
        w = watch.setdefault(uid, {"added": today})
        w["last_charted"] = today
        w["name"] = run["games"].get(uid, {}).get("name", w.get("name", ""))
    cutoff = (date.fromisoformat(today) - timedelta(days=WATCH_DAYS)).isoformat()
    watch = {u: w for u, w in watch.items() if w.get("last_charted", w["added"]) >= cutoff}
    if len(watch) > WATCH_MAX:
        keep = sorted(watch.items(), key=lambda kv: kv[1].get("last_charted", ""), reverse=True)[:WATCH_MAX]
        watch = dict(keep)
    return watch


# ──────────────────────────────────────────────────────────────
# 하루 통계
# ──────────────────────────────────────────────────────────────
def day_stats(daily: dict | None, uid: str, prev_daily: dict | None = None) -> dict | None:
    """그날 게임의 평균/최고 동접, 하루 방문 증가량(vpd)."""
    if not daily:
        return None
    rec = daily.get("games", {}).get(uid)
    if not rec or not rec.get("s"):
        return None
    ccus = [c for _, c in rec["s"]]
    st = {"avg": sum(ccus) / len(ccus), "peak": max(ccus), "n": len(ccus), "charted": bool(rec.get("r")),
          "sp": bool(rec.get("sp")), "vpd": None}
    v0, v1 = rec.get("v0"), rec.get("v1")
    if v0 and v1 and v1[0] - v0[0] >= 6 * 3600 and v1[1] > v0[1]:
        st["vpd"] = (v1[1] - v0[1]) / (v1[0] - v0[0]) * 86400
    elif prev_daily and v1:
        pv = prev_daily.get("games", {}).get(uid, {}).get("v1")
        if pv and v1[0] - pv[0] >= 6 * 3600 and v1[1] > pv[1]:
            st["vpd"] = (v1[1] - pv[1]) / (v1[0] - pv[0]) * 86400
    return st


def afk_flag(st: dict | None) -> bool:
    if not st or st["vpd"] is None or st["avg"] < 1000:
        return False
    return st["vpd"] / st["avg"] < AFK_VISITS_PER_CCU


# ──────────────────────────────────────────────────────────────
# 키워드 (클론 신호)
# ──────────────────────────────────────────────────────────────
STOPWORDS = {
    "the", "and", "for", "with", "you", "your", "our", "from", "this", "that", "are",
    "new", "update", "upd", "event", "free", "roblox", "game", "games", "beta", "alpha",
    "release", "official", "edition", "version", "part", "season", "testing", "test",
    "halloween", "christmas", "xmas", "winter", "summer", "spring", "easter", "admin",
    "codes", "code", "pass", "vip", "best", "super", "mega", "ultra", "fun", "play",
}


def tokenize(name: str) -> list[str]:
    s = name.lower()
    s = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", s)
    s = re.sub(r"[^0-9a-z가-힣]+", " ", s)
    words = [w for w in s.split() if len(w) >= 3 and not w.isdigit()]
    return [w for w in words if w not in STOPWORDS]


def keyword_index(names: dict[str, str], dedupe: bool = True) -> dict[str, set[str]]:
    idx: dict[str, set[str]] = defaultdict(set)
    for uid, name in names.items():
        toks = tokenize(name)
        for t in set(toks):
            idx[t].add(uid)
        for a, b in zip(toks, toks[1:]):
            idx[f"{a} {b}"].add(uid)
    if not dedupe:
        return idx
    bigrams = {k: v for k, v in idx.items() if " " in k}
    for k in [k for k in idx if " " not in k]:
        if any(idx[k] == v and k in bk.split() for bk, v in bigrams.items()):
            del idx[k]
    return idx


# 장르를 뜻하는 흔한 단어는 트렌드 신호가 아니므로 클론 신호에서 제외
GENRE_WORDS = {
    "simulator", "simulation", "sim", "tycoon", "tower", "towers", "defense", "defence", "obby", "obbies",
    "anime", "roleplay", "rpg", "horror", "story", "world", "build", "builder", "avatar", "city", "life",
    "school", "house", "home", "war", "wars", "survival", "survive", "race", "racing", "run", "runner",
    "fight", "fighting", "fighters", "battle", "battles", "pvp", "fps", "shooter", "army", "military",
    "hangout", "chill", "minigames", "mini", "tag", "hide", "seek", "parkour", "difficulty", "chart",
    "clicker", "tap", "idle", "incremental", "merge", "farm", "farming", "fishing", "fish", "pet", "pets",
    "dress", "fashion", "creator", "studio", "catalog", "edition", "remastered", "classic", "legends",
    "legend", "adventure", "adventures", "quest", "dungeon", "dungeons", "zombie", "zombies", "car", "cars",
}


def charted_names(daily: dict | None, ref_day: date | None = None, max_age: int | None = None) -> dict[str, str]:
    """그날 차트에 오른 게임 이름. max_age가 있으면 ref_day 기준 그 일수 이내 출시작만."""
    if not daily:
        return {}
    out = {}
    for u, g in daily.get("games", {}).items():
        if not g.get("r"):
            continue
        if max_age is not None:
            try:
                age = (ref_day - date.fromisoformat(g.get("c", "")[:10])).days
            except ValueError:
                continue
            if age < 0 or age > max_age:
                continue
        out[u] = g.get("n", "")
    return out


def is_genre_keyword(kw: str) -> bool:
    return all(w in GENRE_WORDS for w in kw.split())


def search_keyword(keyword: str) -> dict | None:
    """로블록스 검색으로 키워드 관련 게임 수와 동접 합계(첫 페이지)를 센다. 실패하면 None."""
    try:
        data = http_get_json(SEARCH, {"searchQuery": keyword, "sessionId": str(uuid.uuid4()), "pageType": "all"}, retries=2)
    except RuntimeError:
        return None
    count, ccu = 0, 0
    kw_tokens = set(keyword.split())
    for group in data.get("searchResults", []) or []:
        for c in group.get("contents", []) or []:
            if not c.get("universeId"):
                continue
            name_toks = set(tokenize(c.get("name", "")))
            if kw_tokens <= name_toks:
                count += 1
                ccu += int(c.get("playerCount") or 0)
    return {"count": count, "ccu": ccu}


# ──────────────────────────────────────────────────────────────
# 분석
# ──────────────────────────────────────────────────────────────
def day_str(d: date) -> str:
    return d.isoformat()


def analyze(report_day: str, days: dict[str, dict], watch: dict, meta: dict, ranks_now: dict | None, ranks_prev: dict | None,
            do_search: bool = True) -> dict:
    D = date.fromisoformat(report_day)
    dd = {k: v for k, v in days.items() if v}
    cur = dd.get(report_day)
    prev = dd.get(day_str(D - timedelta(days=1)))
    games = cur.get("games", {}) if cur else {}
    hist_days = sorted(k for k in dd if k <= report_day)

    def stats_on(uid: str, d: date):
        k = day_str(d)
        return day_stats(dd.get(k), uid, dd.get(day_str(d - timedelta(days=1))))

    cache: dict[str, dict] = {}

    def series(uid: str) -> dict[str, dict]:
        if uid not in cache:
            cache[uid] = {k: s for k in hist_days if (s := stats_on(uid, date.fromisoformat(k)))}
        return cache[uid]

    # 1) 주요 정렬 순위 (리포트 시점 기준) + 어제 리포트 대비 변동
    featured, kr_only = [], []
    main_vk = VARIANTS[0]
    if ranks_now:
        main = ranks_now.get(main_vk, {})
        used = set()
        for kw in FEATURED_SORT_KEYWORDS:
            for sid, s in main.items():
                if sid in used or not (kw in sid.lower() or kw in s["name"].lower()):
                    continue
                prev_ids = ((ranks_prev or {}).get(main_vk, {}).get(sid) or {}).get("ids", [])
                prev_rank = {u: i + 1 for i, u in enumerate(prev_ids)}
                rows = [{"uid": u, "rank": i + 1, "prev_rank": prev_rank.get(u), "has_prev": bool(prev_ids),
                         "st": stats_on(u, D)} for i, u in enumerate(s["ids"][:TOP_N])]
                featured.append({"name": s["name"], "rows": rows})
                used.add(sid)
                break
        main_top = set()
        for s in main.values():
            if not s.get("sponsored"):
                main_top.update(s["ids"][:50])
        for vk in VARIANTS[1:]:
            seen = []
            for sid, s in ranks_now.get(vk, {}).items():
                if s.get("sponsored") or not any(k in sid.lower() or k in s["name"].lower() for k in FEATURED_SORT_KEYWORDS):
                    continue
                for u in s["ids"][:30]:
                    if u not in main_top and u not in seen:
                        seen.append(u)
            if seen:
                kr_only.append({"vk": vk, "rows": [{"uid": u, "st": stats_on(u, D)} for u in seen[:TOP_N]]})

    # 2) 급상승: 지표(하루 방문 증가량, 없으면 평균 동접)가 연속 상승 + 약 3일 전 대비 1.3배 이상
    rising = []
    for uid in games:
        ser = series(uid)
        st = ser.get(report_day)
        if not st or st["avg"] < MIN_CCU:
            continue
        use_vpd = st["vpd"] is not None and sum(1 for s in ser.values() if s["vpd"] is not None) >= 2
        metric = (lambda s: s["vpd"]) if use_vpd else (lambda s: s["avg"])
        keys = [k for k in sorted(ser) if metric(ser[k]) is not None]
        if len(keys) < 2 or keys[-1] != report_day:
            continue
        vals = [metric(ser[k]) for k in keys]
        streak = 0
        for a, b in zip(reversed(vals[:-1]), reversed(vals)):
            if b > a * 1.05:
                streak += 1
            else:
                break
        base_target = day_str(D - timedelta(days=3))
        base_key = min(keys[:-1], key=lambda k: abs((date.fromisoformat(k) - date.fromisoformat(base_target)).days))
        base_val = max(metric(ser[base_key]), 1)
        growth = vals[-1] / base_val
        need_streak = 2 if len(keys) >= 3 else 1
        if growth < 1.3 or streak < need_streak:
            continue
        wk = ser.get(day_str(D - timedelta(days=7)))
        wow = (vals[-1] / max(metric(wk), 1)) if wk and metric(wk) is not None else None
        rising.append({"uid": uid, "use_vpd": use_vpd, "base": metric(ser[base_key]), "now": vals[-1],
                       "growth": growth, "streak": streak, "base_gap": (D - date.fromisoformat(base_key)).days,
                       "wow": wow, "st": st})
    rising.sort(key=lambda r: (r["streak"] >= 2, r["growth"]), reverse=True)

    # 3) 처음 차트에 등장 (리포트 날짜에 관심 목록에 새로 들어온 게임)
    new_entries = []
    first_day = meta.get("first_day")
    if first_day and first_day < report_day:
        for uid, w in watch.items():
            if w.get("added") == report_day:
                st = stats_on(uid, D)
                new_entries.append({"uid": uid, "st": st, "avg": st["avg"] if st else 0})
        new_entries.sort(key=lambda r: r["avg"], reverse=True)

    # 4) 신작 레이더
    young = []
    for uid, g in games.items():
        if not g.get("r") or not g.get("c"):
            continue
        try:
            age = (D - date.fromisoformat(g["c"][:10])).days
        except ValueError:
            continue
        if 0 <= age <= NEW_GAME_DAYS:
            ser = series(uid)
            st = ser.get(report_day)
            if not st:
                continue
            pst = ser.get(day_str(D - timedelta(days=1)))
            young.append({"uid": uid, "age": age, "st": st, "pst": pst})
    young.sort(key=lambda r: r["st"]["avg"], reverse=True)

    # 5) 클론 신호: 최근 출시작(KW_YOUNG_DAYS일 이내) 이름에 3개 이상 겹치는 키워드. 장르 단어는 제외
    clones = []
    base_k = None
    for back in (7, 6, 5, 4, 3, 2, 1):
        k = day_str(D - timedelta(days=back))
        if k in dd:
            base_k = k
            break
    names_now = charted_names(cur, D, KW_YOUNG_DAYS)
    idx_now = keyword_index(names_now)
    idx_prev = {}
    if base_k:
        bd = date.fromisoformat(base_k)
        idx_prev = keyword_index(charted_names(dd.get(base_k), bd, KW_YOUNG_DAYS), dedupe=False)

    def avg_of(u):
        s = stats_on(u, D)
        return s["avg"] if s else 0

    def age_of(u):
        try:
            return (D - date.fromisoformat(games[u].get("c", "")[:10])).days
        except (KeyError, ValueError):
            return None

    for kw, ids in idx_now.items():
        if len(ids) < 3 or is_genre_keyword(kw):
            continue
        before = len(idx_prev.get(kw, set())) if base_k else None
        if before is not None and len(ids) < before:
            continue  # 줄어든 키워드는 제외 (유지·증가만)
        ages = [a for u in ids if (a := age_of(u)) is not None]
        top = sorted(ids, key=avg_of, reverse=True)
        clones.append({"kw": kw, "count": len(ids), "before": before, "examples": top[:3],
                       "ccu": sum(avg_of(u) for u in ids),
                       "avg_age": round(sum(ages) / len(ages)) if ages else None,
                       "newest": min(ages) if ages else None})
    clones.sort(key=lambda c: (c["count"] - (c["before"] if c["before"] is not None else 0), c["count"], c["ccu"]), reverse=True)
    clones = clones[:TOP_N]
    if do_search:
        for c in clones[:5]:
            c["search"] = search_keyword(c["kw"])
            time.sleep(0.3)

    return {
        "report_day": report_day,
        "partial": bool(cur) and cur.get("partial", False),
        "runs": len(cur.get("runs", [])) if cur else 0,
        "n_games": len(games),
        "n_watch": len(watch),
        "n_days": len(hist_days),
        "base_k": base_k,
        "featured": featured,
        "kr_only": kr_only,
        "rising": rising[:TOP_N],
        "new_entries": new_entries[:TOP_N],
        "new_total": len(new_entries),
        "young": young[:TOP_N],
        "clones": clones,
        "has_compare": len(hist_days) >= 2,
    }


# ──────────────────────────────────────────────────────────────
# 리포트 (디스코드 임베드)
# ──────────────────────────────────────────────────────────────
def fmt_num(n) -> str:
    if n is None:
        return "-"
    n = float(n)
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n/1_000:.0f}K"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return f"{int(round(n))}"


def md_escape(s: str) -> str:
    s = s.replace("[", "(").replace("]", ")")
    return re.sub(r"([\\*_~`|>])", r"\\\1", s)


class Linker:
    def __init__(self, days: dict[str, dict], report_day: str):
        self.info = {}
        for k in sorted(days):
            for uid, g in (days[k] or {}).get("games", {}).items():
                self.info[uid] = g
        self.D = date.fromisoformat(report_day)

    def link(self, uid: str, max_len: int = 36) -> str:
        g = self.info.get(uid, {})
        name = g.get("n") or uid
        if len(name) > max_len:
            name = name[: max_len - 1] + "…"
        place = g.get("p")
        url = f"https://www.roblox.com/games/{place}" if place else "https://www.roblox.com/discover"
        return f"[{md_escape(name)}]({url})"

    def age(self, uid: str) -> str:
        c = self.info.get(uid, {}).get("c")
        if not c:
            return ""
        try:
            a = (self.D - date.fromisoformat(c[:10])).days
        except ValueError:
            return ""
        if a < 60:
            return f" · 출시 {a}일"
        if a < 730:
            return f" · 출시 {a // 30}개월"
        return f" · 출시 {a // 365}년"


def flags(st: dict | None) -> str:
    out = ""
    if st and st.get("sp"):
        out += " 💰"
    if afk_flag(st):
        out += " ⚠️"
    return out


def clamp_lines(lines: list[str], limit: int = 3900) -> str:
    out, total = [], 0
    for ln in lines:
        if total + len(ln) + 1 > limit:
            out.append("…")
            break
        out.append(ln)
        total += len(ln) + 1
    return "\n".join(out) if out else "해당 없음"


COLORS = {"head": 0x5865F2, "featured": 0xF1C40F, "kr": 0xE67E22, "rising": 0xE74C3C, "new": 0x2ECC71,
          "young": 0x3498DB, "clone": 0x9B59B6}


def build_embeds(a: dict, L: Linker, report_hhmm: str) -> list[dict]:
    E = []
    D = date.fromisoformat(a["report_day"])
    wd = "월화수목금토일"[D.weekday()]
    span = "오늘 지금까지" if a["partial"] else "하루"
    head = [
        f"**{D.month}/{D.day}({wd}) {span}** 기준 · 측정 {a['runs']}회 · 게임 {a['n_games']}개 (관심 목록 {a['n_watch']}개)",
        f"순위는 오늘 {report_hhmm} KST 기준",
        "💰 스폰서 광고 노출 · ⚠️ 방문 대비 동접이 높음(AFK 가능성)",
    ]
    if a["n_days"] < 2:
        head.append("\n기록이 아직 하루치라 비교 섹션은 내일부터 채워져요. 1주일쯤 쌓이면 연속 상승·요일 비교·클론 신호가 제대로 작동해요.")
    elif a["n_days"] < 8:
        head.append(f"\n기록 {a['n_days']}일째예요. 8일째부터 지난주 같은 요일 비교가 나와요.")
    E.append({"title": "📊 로블록스 트렌드 리포트", "description": "\n".join(head), "color": COLORS["head"]})

    for f in a["featured"]:
        lines = []
        for r in f["rows"]:
            st = day_stats_latest(L, r["uid"])
            if not r["has_prev"]:
                mv = ""
            elif r["prev_rank"] is None:
                mv = " `NEW`"
            else:
                d = r["prev_rank"] - r["rank"]
                mv = f" `▲{d}`" if d > 0 else (f" `▼{-d}`" if d < 0 else " `-`")
            lines.append(f"**{r['rank']}.** {L.link(r['uid'])}{flags(r['st'])}{mv} · 동접 {fmt_num(st)}{L.age(r['uid'])}")
        E.append({"title": f"🔥 {f['name']} 상위 {len(f['rows'])} ({vlabel(VARIANTS[0])})", "description": clamp_lines(lines),
                  "color": COLORS["featured"]})

    for k in a["kr_only"]:
        lines = [f"{L.link(r['uid'])}{flags(r['st'])} · 동접 {fmt_num(day_stats_latest(L, r['uid']))}{L.age(r['uid'])}"
                 for r in k["rows"]]
        E.append({"title": f"🇰🇷 {vlabel(k['vk'])}에서만 상위권", "description": clamp_lines(lines), "color": COLORS["kr"]})

    if a["has_compare"]:
        lines = []
        for r in a["rising"]:
            unit = "하루 방문" if r["use_vpd"] else "평균 동접"
            wow = f" · 지난주 같은 요일 ×{r['wow']:.1f}" if r["wow"] else ""
            st = r["st"]
            extra = f" · 평균 동접 {fmt_num(st['avg'])}" if r["use_vpd"] else f" · 최고 {fmt_num(st['peak'])}"
            lines.append(
                f"{L.link(r['uid'])}{flags(st)}\n　{unit} {fmt_num(r['base'])} → **{fmt_num(r['now'])}** "
                f"(×{r['growth']:.1f}, {r['streak']}일 연속↑){extra}{wow}{L.age(r['uid'])}"
            )
        E.append({"title": f"🚀 급상승 TOP {len(a['rising'])} · 연속 상승 + 약 3일 전 대비 1.3배↑",
                  "description": clamp_lines(lines) if lines else "조건을 만족하는 게임이 없어요.", "color": COLORS["rising"]})

        lines = [f"{L.link(r['uid'])}{flags(r['st'])} · 평균 동접 {fmt_num(r['avg'])}{L.age(r['uid'])}" for r in a["new_entries"]]
        if a["new_total"] > len(a["new_entries"]):
            lines.append(f"외 {a['new_total'] - len(a['new_entries'])}개")
        E.append({"title": "🆕 처음 차트에 등장", "description": clamp_lines(lines), "color": COLORS["new"]})

    lines = []
    for r in a["young"]:
        st, pst = r["st"], r["pst"]
        delta = ""
        if pst:
            delta = f" (전날 {fmt_num(pst['avg'])}, ×{st['avg'] / max(pst['avg'], 1):.1f})"
        vpd = f" · 하루 방문 {fmt_num(st['vpd'])}" if st["vpd"] else ""
        lines.append(f"{L.link(r['uid'])}{flags(st)} · 출시 {r['age']}일 · 평균 동접 **{fmt_num(st['avg'])}**{delta}{vpd}")
    E.append({"title": f"🐣 신작 레이더 · 출시 {NEW_GAME_DAYS}일 이내", "description": clamp_lines(lines), "color": COLORS["young"]})

    lines = []
    for c in a["clones"]:
        if c["before"] is None:
            cnt = f"{c['count']}개"
        elif c["count"] > c["before"]:
            cnt = f"{c['before']}→**{c['count']}**개"
        else:
            cnt = f"{c['count']}개(유지)"
        age = f" · 평균 출시 {c['avg_age']}일(가장 최근 {c['newest']}일)" if c["avg_age"] is not None else ""
        srch = ""
        if c.get("search"):
            srch = f" · 검색 {c['search']['count']}개"
        ex = ", ".join(L.link(u, 24) for u in c["examples"])
        lines.append(f"**`{c['kw']}`** 최근작 {cnt} · 평균 동접 합 {fmt_num(c['ccu'])}{age}{srch}\n　{ex}")
    if a["base_k"]:
        gap = (D - date.fromisoformat(a["base_k"])).days
        title = f"🧬 클론 신호 · 출시 {KW_YOUNG_DAYS}일 이내 게임에 겹치는 키워드 ({gap}일 전 대비)"
    else:
        title = f"🧬 클론 신호 · 출시 {KW_YOUNG_DAYS}일 이내 게임 3개 이상에 겹치는 키워드"
    E.append({"title": title, "description": clamp_lines(lines), "color": COLORS["clone"]})
    return E


_LATEST_CCU: dict[str, int] = {}


def day_stats_latest(L: Linker, uid: str):
    return _LATEST_CCU.get(uid)


def send_discord(webhook: str, embeds: list[dict]):
    batch, size = [], 0
    for e in embeds:
        esize = len(e.get("title", "")) + len(e.get("description", ""))
        if batch and (size + esize > 5500 or len(batch) >= 10):
            http_post_json(webhook, {"embeds": batch})
            time.sleep(1)
            batch, size = [], 0
        batch.append(e)
        size += esize
    if batch:
        http_post_json(webhook, {"embeds": batch})


def print_embeds(embeds: list[dict]):
    for e in embeds:
        print("=" * 64)
        print(e["title"])
        print("-" * 64)
        print(e["description"])
    print("=" * 64)


# ──────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────
def notify_error(webhook: str, msg: str, dry: bool):
    print(msg, file=sys.stderr)
    if webhook and not dry:
        try:
            http_post_json(webhook, {"content": msg[:1900]})
        except Exception:
            pass


def make_report(now: datetime, today_daily: dict, watch: dict, meta: dict, do_search: bool = True) -> list[dict]:
    today = now.date()
    yesterday = day_str(today - timedelta(days=1))
    days = {}
    for back in range(0, LOOKBACK_DAYS + 2):
        k = day_str(today - timedelta(days=back))
        days[k] = today_daily if back == 0 else load_daily(k)

    if days.get(yesterday):
        report_day = yesterday
    else:  # 첫날: 오늘 지금까지의 기록으로
        report_day = day_str(today)
        today_daily["partial"] = True

    ranks_now = today_daily.get("latest")
    prev_daily = days.get(yesterday) or {}
    ranks_prev = prev_daily.get("report_ranks")

    _LATEST_CCU.clear()
    for uid, g in today_daily.get("games", {}).items():
        if g.get("s"):
            _LATEST_CCU[uid] = g["s"][-1][1]

    a = analyze(report_day, {k: v for k, v in days.items() if k <= report_day or k == day_str(today)},
                watch, meta, ranks_now, ranks_prev, do_search=do_search)
    L = Linker({k: v for k, v in days.items() if v}, report_day)
    today_daily.pop("partial", None)
    return build_embeds(a, L, today_daily.get("latest_at", now.strftime("%H:%M")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="디스코드로 보내지 않고 화면에 출력")
    ap.add_argument("--force-report", action="store_true", help="시간과 상관없이 리포트 전송")
    ap.add_argument("--report-only", action="store_true", help="수집 없이 저장된 기록으로 리포트만")
    args = ap.parse_args()
    force = args.force_report or os.getenv("FORCE_REPORT", "0") == "1"

    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not args.dry_run and not webhook:
        sys.exit("DISCORD_WEBHOOK_URL 환경변수가 없어요. GitHub 저장소 Secrets에 등록했는지 확인하세요.")

    now = datetime.now(KST)
    today = day_str(now.date())
    watch = read_json(WATCHLIST_PATH, {})
    meta = read_json(META_PATH, {})
    meta.setdefault("first_day", today)
    daily = load_daily(today) or {"date": today}

    if not args.report_only:
        try:
            run = collect_run(now, list(watch.keys()))
        except Exception as e:
            notify_error(webhook, f"⚠️ 트렌드 수집 실패 ({now:%m/%d %H:%M}): {e}", args.dry_run)
            sys.exit(1)
        daily = merge_run(daily, run)
        watch = update_watchlist(watch, run, today)
        n_sorts = sum(len(v) for v in run["variants"].values())
        print(f"측정 {run['hhmm']}: 기준 {len(run['variants'])}개, 정렬 {n_sorts}개, 게임 {len(run['games'])}개, 관심 목록 {len(watch)}개")

    due = now.hour >= REPORT_HOUR and not daily.get("report_sent")
    if args.dry_run or force or due:
        embeds = make_report(now, daily, watch, meta)
        if args.dry_run:
            print_embeds(embeds)
        else:
            try:
                send_discord(webhook, embeds)
                print(f"디스코드 전송 완료 (임베드 {len(embeds)}개)")
            except Exception as e:
                notify_error(webhook, f"⚠️ 리포트 전송 실패: {e}", False)
        if due and not args.dry_run:
            daily["report_sent"] = now.strftime("%H:%M")
            daily["report_ranks"] = daily.get("latest")
    else:
        print("리포트 시간이 아니어서 수집만 했어요.")

    if not args.report_only:
        write_json(daily_path(today), daily)
        write_json(WATCHLIST_PATH, watch)
        write_json(META_PATH, meta)


if __name__ == "__main__":
    main()
