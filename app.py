
import io
import os
import html
import re
import ssl
import sqlite3
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, timedelta, datetime
from pathlib import Path

import pandas as pd
import requests
import streamlit as st
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib3.poolmanager import PoolManager
from bs4 import BeautifulSoup


# =========================================================
# 기본 설정
# =========================================================
st.set_page_config(
    page_title="법인 Finder",
    page_icon="🏢",
    layout="wide",
)

st.title("🏢 법인 Finder")
st.caption("법인명 + 본점/사업장 소재지 검색 · OpenDART 감사보고서/거래은행·차입금 분석 · v2.7")

DART_BASE = "https://opendart.fss.or.kr/api"
DB_PATH = Path(os.getenv("CORP_FINDER_DB", "corp_finder_cache.sqlite3"))


# =========================================================
# 거래은행 표시 UI
# =========================================================
st.markdown(
    """
    <style>
    .bank-note {
        background: #f7f9fc;
        border: 1px solid #e1e6ef;
        border-left: 5px solid #ff4b4b;
        border-radius: 8px;
        padding: 12px 14px;
        margin: 8px 0 12px 0;
        line-height: 1.55;
        font-size: 0.97rem;
        color: #1f2937;
        white-space: pre-wrap;
    }
    .bank-meta {
        display: flex;
        flex-wrap: wrap;
        gap: 7px;
        margin: 4px 0 10px 0;
    }
    .bank-chip {
        display: inline-block;
        padding: 4px 9px;
        border-radius: 999px;
        background: #eef2f7;
        border: 1px solid #dde3ec;
        font-size: 0.86rem;
        font-weight: 650;
        color: #273142;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def _evidence_items(bank_row):
    raw = str(bank_row.get("근거문구") or "").strip()
    if not raw:
        return []
    parts = [re.sub(r"\\s+", " ", x).strip() for x in re.split(r"\\n\\s*\\n", raw) if x.strip()]
    seen, out = set(), []
    for x in parts:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def render_bank_analysis(banks, title="🏦 감사보고서에서 확인된 거래 금융기관"):
    """거래은행 요약표와 은행별 감사보고서 근거를 한 화면에 표시한다."""
    if not banks:
        return

    st.markdown(f"### {title}")

    summary_rows = []
    for b in banks:
        evidences = _evidence_items(b)
        representative = evidences[0] if evidences else "-"
        if len(representative) > 180:
            representative = representative[:177] + "..."
        summary_rows.append({
            "은행": b.get("은행") or "-",
            "거래유형": b.get("거래유형") or "-",
            "확인강도": b.get("확인강도") or "-",
            "금액표기": b.get("금액표기") or "-",
            "근거건수": b.get("근거건수") or len(evidences),
            "대표 근거문구": representative,
        })

    summary_df = pd.DataFrame(summary_rows)
    table_height = min(90 + max(len(summary_df), 1) * 58, 620)
    st.dataframe(
        summary_df,
        use_container_width=True,
        hide_index=True,
        height=table_height,
        row_height=52,
        column_config={
            "은행": st.column_config.TextColumn("은행", width="medium"),
            "거래유형": st.column_config.TextColumn("거래유형", width="large"),
            "확인강도": st.column_config.TextColumn("확인강도", width="small"),
            "금액표기": st.column_config.TextColumn("금액표기", width="medium"),
            "근거건수": st.column_config.NumberColumn("근거건수", width="small", format="%d건"),
            "대표 근거문구": st.column_config.TextColumn("감사보고서 대표 근거", width="large"),
        },
    )

    st.caption(
        "※ 거래유형은 차입/대출·담보/질권·예금·보증/약정 등의 문맥을 함께 분석한 결과입니다. "
        "은행명이 기재되었다는 사실만으로 주거래은행이라고 단정하지 않습니다."
    )

    st.markdown("#### 📑 은행별 거래유형 · 감사보고서 근거")
    st.caption("아래 근거문구는 접어두지 않고 바로 표시됩니다. 은행별로 실제 보고서 문맥을 한 화면에서 비교할 수 있습니다.")

    for b in banks:
        evidences = _evidence_items(b)
        with st.container(border=True):
            head_left, head_right = st.columns([5, 1])
            with head_left:
                st.markdown(f"### 🏦 {b.get('은행') or '-'}")
                chips = []
                for value in [b.get("거래유형"), f"확인강도 {b.get('확인강도') or '-'}"]:
                    if value:
                        chips.append(f'<span class="bank-chip">{html.escape(str(value))}</span>')
                if b.get("금액표기"):
                    chips.append(f'<span class="bank-chip">금액 {html.escape(str(b.get("금액표기")))}</span>')
                st.markdown('<div class="bank-meta">' + ''.join(chips) + '</div>', unsafe_allow_html=True)
            with head_right:
                st.metric("근거", f"{b.get('근거건수') or len(evidences)}건")

            if evidences:
                for idx, evidence in enumerate(evidences, start=1):
                    safe = html.escape(evidence)
                    st.markdown(
                        f'<div class="bank-note"><b>근거 {idx}</b><br>{safe}</div>',
                        unsafe_allow_html=True,
                    )
            else:
                st.info("표시할 감사보고서 근거문구가 없습니다.")


# =========================================================
# OpenDART HTTPS 연결 설정 (로컬 + Streamlit Cloud 호환)
# =========================================================
# Streamlit Cloud/Linux에서는 기본 TLS 연결이 정상인 경우가 많고,
# 일부 Windows/Python 3.13 환경에서만 DH_KEY_TOO_SMALL 오류가 발생할 수 있습니다.
# 따라서 기본 TLS를 먼저 사용하고, 해당 SSL 오류일 때만 호환 모드로 재시도합니다.

class DARTSSLAdapter(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        pool_kwargs["ssl_context"] = ctx
        self.poolmanager = PoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            **pool_kwargs,
        )


def _retry_policy():
    return Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )


@st.cache_resource
def get_dart_default_session():
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=_retry_policy())
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 Corporate-Finder/2.7",
        "Accept": "*/*",
        "Connection": "keep-alive",
    })
    return session


@st.cache_resource
def get_dart_legacy_session():
    session = requests.Session()
    adapter = DARTSSLAdapter(max_retries=_retry_policy())
    session.mount("https://opendart.fss.or.kr/", adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 Corporate-Finder/2.7",
        "Accept": "*/*",
        "Connection": "keep-alive",
    })
    return session


def dart_get(url, **kwargs):
    try:
        return get_dart_default_session().get(url, **kwargs)
    except requests.exceptions.SSLError as e:
        msg = str(e).upper()
        if "DH_KEY_TOO_SMALL" in msg or "DH KEY TOO SMALL" in msg:
            return get_dart_legacy_session().get(url, **kwargs)
        raise

BANK_PATTERNS = {
    "하나은행": [
        r"하나은행", r"KEB\s*하나은행", r"KEB하나", r"㈜하나은행", r"\(주\)하나은행"
    ],
    "KB국민은행": [
        r"KB\s*국민은행", r"국민은행", r"㈜국민은행", r"\(주\)국민은행"
    ],
    "신한은행": [r"신한은행", r"㈜신한은행", r"\(주\)신한은행"],
    "우리은행": [r"우리은행", r"㈜우리은행", r"\(주\)우리은행"],
    "NH농협은행": [
        r"NH\s*농협은행", r"농협은행", r"농협중앙회", r"NH농협"
    ],
    "IBK기업은행": [
        r"IBK\s*기업은행", r"기업은행", r"중소기업은행"
    ],
    "KDB산업은행": [
        r"KDB\s*산업은행", r"산업은행", r"한국산업은행"
    ],
    "SC제일은행": [
        r"SC\s*제일은행", r"제일은행", r"스탠다드차타드은행", r"한국스탠다드차타드은행"
    ],
    "한국씨티은행": [r"한국씨티은행", r"씨티은행"],
    "iM뱅크(구 대구은행)": [r"iM뱅크", r"아이엠뱅크", r"대구은행"],
    "BNK부산은행": [r"BNK\s*부산은행", r"부산은행"],
    "BNK경남은행": [r"BNK\s*경남은행", r"경남은행"],
    "광주은행": [r"광주은행"],
    "전북은행": [r"전북은행"],
    "제주은행": [r"제주은행"],
    "Sh수협은행": [r"Sh\s*수협은행", r"수협은행"],
}

CATEGORY_PATTERNS = {
    "차입/대출": [
        r"차입금", r"단기차입", r"장기차입", r"대출", r"시설자금",
        r"운전자금", r"일반자금", r"한도대출"
    ],
    "담보/질권": [
        r"담보", r"근저당", r"질권", r"담보제공", r"담보설정"
    ],
    "예금": [
        r"예금", r"정기예금", r"사용제한", r"금융상품", r"예치금"
    ],
    "보증/약정": [
        r"지급보증", r"보증", r"약정", r"한도약정", r"신용장", r"외화"
    ],
}

RELATED_WORDS = [
    "은행", "금융기관", "차입금", "단기차입금", "장기차입금",
    "담보", "질권", "예금", "정기예금", "사용제한예금",
    "대출", "지급보증", "한도약정", "약정"
]


# =========================================================
# 유틸리티
# =========================================================
def get_secret(name, default=""):
    # Streamlit Cloud: st.secrets / 로컬: st.secrets 또는 환경변수 모두 지원
    try:
        value = st.secrets.get(name, None)
        if value not in (None, ""):
            return value
    except Exception:
        pass
    return os.getenv(name, default)


def normalize_company_name(name: str) -> str:
    if not name:
        return ""
    x = re.sub(r"\s+", "", str(name))
    x = x.replace("주식회사", "").replace("(주)", "").replace("㈜", "")
    return x.lower()


def request_json(url, params, timeout=30):
    r = dart_get(url, params=params, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    status = str(data.get("status", "000"))
    # 013은 조회된 데이터가 없다는 정상적인 빈 결과 코드
    if status == "013":
        return data
    if status not in ("000", ""):
        raise RuntimeError(data.get("message", f"DART 오류 코드: {status}"))
    return data


def decode_bytes(raw: bytes) -> str:
    for enc in ("utf-8", "cp949", "euc-kr"):
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return raw.decode("utf-8", errors="ignore")


def clean_text(raw_text: str) -> str:
    soup = BeautifulSoup(raw_text, "html.parser")
    text = soup.get_text("\n")
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def money_tokens(text: str):
    # 감사보고서에서 흔한 금액 표기를 넓게 포착
    patt = r"(?<!\d)(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*(?:원|천원|백만원|억원)"
    found = re.findall(patt, text)
    result = []
    for x in found:
        x = re.sub(r"\s+", "", x)
        if x not in result:
            result.append(x)
    return result[:5]



# =========================================================
# 지역검색용 로컬 DB / 외부 법인목록
# =========================================================
REGION_PROVINCES = [
    "전체", "서울특별시", "부산광역시", "대구광역시", "인천광역시",
    "광주광역시", "대전광역시", "울산광역시", "세종특별자치시",
    "경기도", "강원특별자치도", "충청북도", "충청남도", "전북특별자치도",
    "전라남도", "경상북도", "경상남도", "제주특별자치도",
]

CHEONGJU_DISTRICTS = ["전체", "상당구", "서원구", "흥덕구", "청원구"]

EXTERNAL_COL_CANDIDATES = {
    "법인명": ["법인명", "회사명", "업체명", "기업명", "상호명", "상호", "업소명"],
    "대표자": ["대표자", "대표자명", "대표"],
    "주소": [
        "본점주소", "본사주소", "공장대표주소(도로명)", "공장대표주소", "사업장 주소",
        "사업장주소", "영업소 주소(도로명)", "영업소주소", "소재지", "주소", "도로명주소"
    ],
    "지역": ["시군", "시군구", "지역", "소재 시군", "소재시군"],
    "업종": ["업종", "업종명", "산업분류", "산업분류명", "KSIC", "주생산품", "제품"],
    "전화번호": ["전화번호", "전화", "소재지전화", "사업장 번호", "사업장번호"],
    "사업자등록번호": ["사업자등록번호", "사업자번호"],
    "법인등록번호": ["법인등록번호", "법인번호"],
}


def get_db_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_local_db():
    with get_db_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS corp_master (
                corp_code TEXT PRIMARY KEY,
                corp_name TEXT NOT NULL,
                stock_code TEXT,
                modify_date TEXT,
                norm_name TEXT
            );

            CREATE TABLE IF NOT EXISTS company_profile (
                corp_code TEXT PRIMARY KEY,
                corp_name TEXT,
                stock_code TEXT,
                modify_date TEXT,
                ceo_nm TEXT,
                corp_cls TEXT,
                jurir_no TEXT,
                bizr_no TEXT,
                adres TEXT,
                hm_url TEXT,
                ir_url TEXT,
                phn_no TEXT,
                fax_no TEXT,
                induty_code TEXT,
                fetched_at TEXT,
                fetch_error TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_profile_adres ON company_profile(adres);
            CREATE INDEX IF NOT EXISTS idx_profile_name ON company_profile(corp_name);
            CREATE INDEX IF NOT EXISTS idx_master_norm_name ON corp_master(norm_name);
            """
        )


def sync_corp_master(corp_df: pd.DataFrame):
    incoming_count = int(len(corp_df))
    incoming_max_date = str(corp_df["modify_date"].max()) if incoming_count and "modify_date" in corp_df.columns else ""
    with get_db_connection() as conn:
        current_count = int(conn.execute("SELECT COUNT(*) FROM corp_master").fetchone()[0])
        current_max_date = str(conn.execute("SELECT COALESCE(MAX(modify_date),'') FROM corp_master").fetchone()[0] or "")
        if current_count == incoming_count and current_max_date == incoming_max_date:
            return

    rows = [
        (str(r.corp_code), str(r.corp_name), str(r.stock_code), str(r.modify_date), str(r.norm_name))
        for r in corp_df[["corp_code", "corp_name", "stock_code", "modify_date", "norm_name"]].itertuples(index=False)
    ]
    with get_db_connection() as conn:
        conn.executemany(
            """
            INSERT INTO corp_master(corp_code, corp_name, stock_code, modify_date, norm_name)
            VALUES(?,?,?,?,?)
            ON CONFLICT(corp_code) DO UPDATE SET
                corp_name=excluded.corp_name,
                stock_code=excluded.stock_code,
                modify_date=excluded.modify_date,
                norm_name=excluded.norm_name
            """,
            rows,
        )


def address_db_stats():
    with get_db_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM corp_master").fetchone()[0]
        done = conn.execute(
            "SELECT COUNT(*) FROM company_profile WHERE COALESCE(adres,'') <> ''"
        ).fetchone()[0]
        failed = conn.execute(
            "SELECT COUNT(*) FROM company_profile WHERE COALESCE(fetch_error,'') <> ''"
        ).fetchone()[0]
    return int(total), int(done), int(failed)


def get_profile_rows_for_search(terms, limit=1000):
    terms = [str(x).strip() for x in terms if str(x).strip()]
    sql = """
        SELECT p.corp_code, p.corp_name, p.stock_code, p.ceo_nm, p.corp_cls,
               p.jurir_no, p.bizr_no, p.adres, p.hm_url, p.phn_no, p.induty_code,
               p.modify_date, p.fetched_at
        FROM company_profile p
        WHERE COALESCE(p.adres,'') <> ''
    """
    params = []
    for term in terms:
        sql += " AND p.adres LIKE ?"
        params.append(f"%{term}%")
    sql += " ORDER BY p.corp_name LIMIT ?"
    params.append(int(limit))
    with get_db_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def get_missing_profiles(batch_size=100, listed_first=True, retry_errors=False):
    where = "p.corp_code IS NULL"
    if retry_errors:
        where = "p.corp_code IS NULL OR COALESCE(p.fetch_error,'') <> ''"
    order = "CASE WHEN COALESCE(m.stock_code,'') <> '' THEN 0 ELSE 1 END, m.corp_name" if listed_first else "m.corp_name"
    sql = f"""
        SELECT m.corp_code, m.corp_name, m.stock_code, m.modify_date
        FROM corp_master m
        LEFT JOIN company_profile p ON p.corp_code = m.corp_code
        WHERE {where}
        ORDER BY {order}
        LIMIT ?
    """
    with get_db_connection() as conn:
        rows = conn.execute(sql, (int(batch_size),)).fetchall()
    return [dict(r) for r in rows]


def save_company_profile(master_row: dict, company: dict | None = None, error: str = ""):
    company = company or {}
    values = (
        master_row.get("corp_code", ""),
        company.get("corp_name") or master_row.get("corp_name", ""),
        company.get("stock_code") or master_row.get("stock_code", ""),
        master_row.get("modify_date", ""),
        company.get("ceo_nm", ""),
        company.get("corp_cls", ""),
        company.get("jurir_no", ""),
        company.get("bizr_no", ""),
        company.get("adres", ""),
        company.get("hm_url", ""),
        company.get("ir_url", ""),
        company.get("phn_no", ""),
        company.get("fax_no", ""),
        company.get("induty_code", ""),
        datetime.now().isoformat(timespec="seconds"),
        str(error)[:500],
    )
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO company_profile(
                corp_code, corp_name, stock_code, modify_date, ceo_nm, corp_cls,
                jurir_no, bizr_no, adres, hm_url, ir_url, phn_no, fax_no,
                induty_code, fetched_at, fetch_error
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(corp_code) DO UPDATE SET
                corp_name=excluded.corp_name,
                stock_code=excluded.stock_code,
                modify_date=excluded.modify_date,
                ceo_nm=excluded.ceo_nm,
                corp_cls=excluded.corp_cls,
                jurir_no=excluded.jurir_no,
                bizr_no=excluded.bizr_no,
                adres=excluded.adres,
                hm_url=excluded.hm_url,
                ir_url=excluded.ir_url,
                phn_no=excluded.phn_no,
                fax_no=excluded.fax_no,
                induty_code=excluded.induty_code,
                fetched_at=excluded.fetched_at,
                fetch_error=excluded.fetch_error
            """,
            values,
        )


def fetch_company_info_uncached(api_key: str, corp_code: str) -> dict:
    return request_json(
        f"{DART_BASE}/company.json",
        {"crtfc_key": api_key, "corp_code": corp_code},
        timeout=30,
    )


def build_address_cache(api_key: str, batch_size=100, listed_first=True, retry_errors=False):
    targets = get_missing_profiles(batch_size, listed_first, retry_errors)
    if not targets:
        return 0, 0

    progress = st.progress(0)
    status_box = st.empty()
    ok = 0
    fail = 0
    for idx, row in enumerate(targets, start=1):
        status_box.write(f"주소 DB 구축 {idx}/{len(targets)} · {row['corp_name']}")
        try:
            info = fetch_company_info_uncached(api_key, row["corp_code"])
            save_company_profile(row, info, "")
            ok += 1
        except Exception as exc:
            msg = str(exc)
            save_company_profile(row, {}, msg)
            fail += 1
            if "요청 제한" in msg or "020" in msg:
                status_box.warning("OpenDART 요청 한도에 도달해 이번 주소 DB 구축을 중단했습니다. 다음 이용 가능 시점에 이어서 실행하면 됩니다.")
                progress.progress(idx / len(targets))
                return ok, fail
        progress.progress(idx / len(targets))

    status_box.empty()
    return ok, fail


def export_address_cache_excel():
    with get_db_connection() as conn:
        df = pd.read_sql_query(
            """
            SELECT corp_code AS DART고유번호, corp_name AS 법인명, stock_code AS 종목코드,
                   ceo_nm AS 대표자, corp_cls AS 법인구분, jurir_no AS 법인등록번호,
                   bizr_no AS 사업자등록번호, adres AS 주소, induty_code AS 업종코드,
                   phn_no AS 전화번호, hm_url AS 홈페이지, modify_date AS DART수정일,
                   fetched_at AS 수집일시, fetch_error AS 오류
            FROM company_profile
            ORDER BY corp_name
            """,
            conn,
        )
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="주소DB")
    out.seek(0)
    return out.getvalue()


def import_address_cache_file(uploaded_file):
    if uploaded_file is None:
        return 0
    raw = uploaded_file.getvalue()
    name = (uploaded_file.name or "").lower()
    if name.endswith((".xlsx", ".xls")):
        df = pd.read_excel(io.BytesIO(raw), dtype=str).fillna("")
    else:
        df = None
        for enc in ("utf-8-sig", "cp949", "euc-kr", "utf-8"):
            try:
                df = pd.read_csv(io.BytesIO(raw), dtype=str, encoding=enc).fillna("")
                break
            except Exception:
                pass
        if df is None:
            raise RuntimeError("CSV 인코딩을 판별하지 못했습니다.")

    rename = {
        "DART고유번호": "corp_code", "법인명": "corp_name", "종목코드": "stock_code",
        "대표자": "ceo_nm", "법인구분": "corp_cls", "법인등록번호": "jurir_no",
        "사업자등록번호": "bizr_no", "주소": "adres", "업종코드": "induty_code",
        "전화번호": "phn_no", "홈페이지": "hm_url", "DART수정일": "modify_date",
        "수집일시": "fetched_at", "오류": "fetch_error",
    }
    df = df.rename(columns=rename)
    if "corp_code" not in df.columns:
        raise RuntimeError("주소 DB 파일에 'DART고유번호' 열이 없습니다.")

    count = 0
    with get_db_connection() as conn:
        for _, r in df.iterrows():
            corp_code = str(r.get("corp_code", "")).strip()
            if not corp_code:
                continue
            master = conn.execute(
                "SELECT corp_code, corp_name, stock_code, modify_date FROM corp_master WHERE corp_code=?",
                (corp_code,),
            ).fetchone()
            master_row = dict(master) if master else {
                "corp_code": corp_code,
                "corp_name": str(r.get("corp_name", "")),
                "stock_code": str(r.get("stock_code", "")),
                "modify_date": str(r.get("modify_date", "")),
            }
            company = {
                "corp_name": str(r.get("corp_name", "")), "stock_code": str(r.get("stock_code", "")),
                "ceo_nm": str(r.get("ceo_nm", "")), "corp_cls": str(r.get("corp_cls", "")),
                "jurir_no": str(r.get("jurir_no", "")), "bizr_no": str(r.get("bizr_no", "")),
                "adres": str(r.get("adres", "")), "hm_url": str(r.get("hm_url", "")),
                "phn_no": str(r.get("phn_no", "")), "induty_code": str(r.get("induty_code", "")),
            }
            save_company_profile(master_row, company, str(r.get("fetch_error", "")))
            count += 1
    return count


def _normalize_colname(x):
    return re.sub(r"[\s_\-·./()]+", "", str(x)).lower()


def _find_external_column(df, candidates):
    norm_map = {_normalize_colname(c): c for c in df.columns}
    for cand in candidates:
        key = _normalize_colname(cand)
        if key in norm_map:
            return norm_map[key]
    for cand in candidates:
        key = _normalize_colname(cand)
        for norm, orig in norm_map.items():
            if key and (key in norm or norm in key):
                return orig
    return None


def _external_header_score(values):
    """외부 기업목록에서 실제 헤더 행을 찾기 위한 점수.

    충북 제조업체총람처럼 상단에 제목/공백 행이 있고 실제 열 이름이 3~10행 아래에
    있는 파일을 자동 인식한다. '업 체 명'처럼 공백이 들어간 열 이름도 정규화해서 처리한다.
    """
    cells = [_normalize_colname(v) for v in values if str(v).strip() and str(v).lower() != "nan"]
    if not cells:
        return -1

    name_keys = {_normalize_colname(x) for x in EXTERNAL_COL_CANDIDATES["법인명"]}
    addr_keys = {_normalize_colname(x) for x in EXTERNAL_COL_CANDIDATES["주소"]}
    region_keys = {_normalize_colname(x) for x in EXTERNAL_COL_CANDIDATES["지역"]}
    product_keys = {_normalize_colname(x) for x in EXTERNAL_COL_CANDIDATES["업종"]}
    phone_keys = {_normalize_colname(x) for x in EXTERNAL_COL_CANDIDATES["전화번호"]}

    # 시/군명처럼 자료마다 조금씩 달라지는 열 이름도 헤더 단서로 사용
    extra_region_keys = {_normalize_colname(x) for x in ["시군명", "시군", "시군구", "지역명"]}
    region_keys |= extra_region_keys

    has_name = any(c in name_keys for c in cells)
    has_addr = any(c in addr_keys for c in cells)
    has_region = any(c in region_keys for c in cells)

    # 회사명과 주소(또는 지역)가 같이 있는 행만 실제 헤더 후보로 인정
    if not has_name or not (has_addr or has_region):
        return -1

    score = 10
    score += 5 if has_addr else 0
    score += 3 if has_region else 0
    score += 2 if any(c in product_keys for c in cells) else 0
    score += 2 if any(c in phone_keys for c in cells) else 0
    score += min(len(cells), 10) * 0.1
    return score


def _read_external_table(uploaded_file):
    """CSV/Excel 기업목록을 읽고 실제 헤더 행을 자동 탐지한다.

    반환값: (DataFrame, meta)
    meta에는 파일/시트/헤더행 정보가 들어간다.
    """
    raw = uploaded_file.getvalue()
    name = (uploaded_file.name or "").lower()

    if name.endswith((".xlsx", ".xls")):
        excel = pd.ExcelFile(io.BytesIO(raw))
        best = None
        for sheet in excel.sheet_names:
            try:
                preview = pd.read_excel(
                    excel,
                    sheet_name=sheet,
                    header=None,
                    dtype=str,
                    nrows=60,
                ).fillna("")
            except Exception:
                continue

            for idx, row in preview.iterrows():
                score = _external_header_score(row.tolist())
                if score >= 0 and (best is None or score > best[0]):
                    best = (score, sheet, int(idx))

        if best is not None:
            _, sheet, header_row = best
            df = pd.read_excel(
                excel,
                sheet_name=sheet,
                header=header_row,
                dtype=str,
            ).fillna("")
            meta = {
                "파일": uploaded_file.name,
                "시트": sheet,
                "헤더행": header_row + 1,
                "헤더자동감지": True,
            }
            return df, meta

        # 일반적인 1행 헤더 파일은 기존 방식으로도 처리
        df = pd.read_excel(excel, sheet_name=excel.sheet_names[0], dtype=str).fillna("")
        return df, {
            "파일": uploaded_file.name,
            "시트": excel.sheet_names[0],
            "헤더행": 1,
            "헤더자동감지": False,
        }

    # CSV도 상단에 제목 행이 있을 수 있으므로 먼저 header=None으로 읽어 헤더를 탐색
    last_error = None
    for enc in ("utf-8-sig", "cp949", "euc-kr", "utf-8"):
        try:
            preview = pd.read_csv(
                io.BytesIO(raw),
                dtype=str,
                encoding=enc,
                header=None,
                nrows=60,
                engine="python",
            ).fillna("")
            best_row = None
            best_score = -1
            for idx, row in preview.iterrows():
                score = _external_header_score(row.tolist())
                if score > best_score:
                    best_score = score
                    best_row = int(idx)

            if best_score >= 0 and best_row is not None:
                df = pd.read_csv(
                    io.BytesIO(raw),
                    dtype=str,
                    encoding=enc,
                    header=best_row,
                    engine="python",
                ).fillna("")
                return df, {
                    "파일": uploaded_file.name,
                    "인코딩": enc,
                    "헤더행": best_row + 1,
                    "헤더자동감지": True,
                }

            df = pd.read_csv(
                io.BytesIO(raw),
                dtype=str,
                encoding=enc,
                engine="python",
            ).fillna("")
            return df, {
                "파일": uploaded_file.name,
                "인코딩": enc,
                "헤더행": 1,
                "헤더자동감지": False,
            }
        except Exception as e:
            last_error = e
            continue

    raise RuntimeError(f"CSV를 읽지 못했습니다: {last_error or '인코딩/형식을 확인해 주세요.'}")


def read_external_company_file(uploaded_file):
    df, read_meta = _read_external_table(uploaded_file)

    # 완전히 빈 열/행 정리
    df = df.dropna(axis=0, how="all").dropna(axis=1, how="all").fillna("")
    df.columns = [str(c).strip() for c in df.columns]

    mapped = {}
    for canonical, candidates in EXTERNAL_COL_CANDIDATES.items():
        mapped[canonical] = _find_external_column(df, candidates)

    if not mapped["법인명"]:
        cols = ", ".join([str(c) for c in df.columns[:20]])
        raise RuntimeError(
            "회사명/법인명/업체명에 해당하는 열을 찾지 못했습니다. "
            f"현재 인식된 열: {cols}"
        )
    if not mapped["주소"] and not mapped["지역"]:
        cols = ", ".join([str(c) for c in df.columns[:20]])
        raise RuntimeError(
            "주소/본사주소/공장주소/소재지에 해당하는 열을 찾지 못했습니다. "
            f"현재 인식된 열: {cols}"
        )

    out = pd.DataFrame()
    for canonical in EXTERNAL_COL_CANDIDATES:
        col = mapped.get(canonical)
        out[canonical] = df[col].astype(str).str.strip() if col else ""

    out["검색주소"] = (out["지역"].fillna("") + " " + out["주소"].fillna("")).str.strip()
    out["norm_name"] = out["법인명"].map(normalize_company_name)
    out = out[out["법인명"].astype(str).str.strip() != ""].reset_index(drop=True)

    # UI에서 사용자가 어떤 행/열을 인식했는지 확인할 수 있도록 메타 포함
    mapped_with_meta = dict(mapped)
    mapped_with_meta["__파일인식정보__"] = read_meta
    return out, mapped_with_meta

def filter_external_by_region(df: pd.DataFrame, terms, name_query="", limit=3000):
    if df is None or df.empty:
        return pd.DataFrame()
    x = df.copy()
    series = x["검색주소"].fillna("").astype(str)
    for term in [str(t).strip() for t in terms if str(t).strip()]:
        x = x[series.loc[x.index].str.contains(re.escape(term), case=False, na=False)]
    q = normalize_company_name(name_query)
    if q:
        x = x[x["norm_name"].str.contains(re.escape(q), na=False)]
    return x.head(int(limit)).copy()


def dart_match_for_name(corp_df: pd.DataFrame, company_name: str):
    q = normalize_company_name(company_name)
    if not q:
        return []
    exact = corp_df[corp_df["norm_name"] == q]
    if not exact.empty:
        return exact.head(20).to_dict("records")
    contains = corp_df[corp_df["norm_name"].str.contains(re.escape(q), na=False)]
    return contains.head(20).to_dict("records")


def region_terms_from_inputs(province, city, district, town):
    terms = []
    if province and province != "전체":
        terms.append(province)
    for x in (city, district, town):
        x = str(x or "").strip()
        if x and x != "전체":
            terms.append(x)
    return terms


# =========================================================
# DART API
# =========================================================
@st.cache_data(ttl=86400, show_spinner=False)
def load_corp_codes(api_key: str) -> pd.DataFrame:
    url = f"{DART_BASE}/corpCode.xml"
    r = dart_get(url, params={"crtfc_key": api_key}, timeout=60)
    r.raise_for_status()

    try:
        zf = zipfile.ZipFile(io.BytesIO(r.content))
    except zipfile.BadZipFile:
        msg = decode_bytes(r.content)
        raise RuntimeError(f"고유번호 파일을 열 수 없습니다. API 키를 확인해 주세요.\n{msg[:300]}")

    names = zf.namelist()
    xml_name = next((n for n in names if n.lower().endswith(".xml")), None)
    if not xml_name:
        raise RuntimeError("DART 고유번호 XML 파일을 찾지 못했습니다.")

    xml_text = decode_bytes(zf.read(xml_name))
    root = ET.fromstring(xml_text)

    rows = []
    for item in root.findall(".//list"):
        def _t(tag):
            node = item.find(tag)
            return (node.text or "").strip() if node is not None else ""

        rows.append({
            "corp_code": _t("corp_code"),
            "corp_name": _t("corp_name"),
            "stock_code": _t("stock_code"),
            "modify_date": _t("modify_date"),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("DART 법인 고유번호 목록이 비어 있습니다.")
    df["norm_name"] = df["corp_name"].map(normalize_company_name)
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def get_company_info(api_key: str, corp_code: str) -> dict:
    return request_json(
        f"{DART_BASE}/company.json",
        {"crtfc_key": api_key, "corp_code": corp_code},
        timeout=30,
    )


@st.cache_data(ttl=1800, show_spinner=False)
def get_disclosures(api_key: str, corp_code: str, years_back=5) -> pd.DataFrame:
    end = date.today()
    start = end - timedelta(days=365 * years_back + 30)

    params = {
        "crtfc_key": api_key,
        "corp_code": corp_code,
        "bgn_de": start.strftime("%Y%m%d"),
        "end_de": end.strftime("%Y%m%d"),
        "page_no": 1,
        "page_count": 100,
    }
    data = request_json(f"{DART_BASE}/list.json", params, timeout=30)
    items = data.get("list", [])
    df = pd.DataFrame(items)
    return df


def pick_latest_audit_report(disclosures: pd.DataFrame):
    if disclosures is None or disclosures.empty or "report_nm" not in disclosures.columns:
        return None

    df = disclosures.copy()
    # "감사보고서", "[기재정정]감사보고서" 등 포함
    mask = df["report_nm"].astype(str).str.contains("감사보고서", na=False)
    audit = df[mask].copy()

    if audit.empty:
        return None

    # 최근 접수일 / 접수번호 기준
    sort_cols = [c for c in ["rcept_dt", "rcept_no"] if c in audit.columns]
    if sort_cols:
        audit = audit.sort_values(sort_cols, ascending=False)
    return audit.iloc[0].to_dict()


@st.cache_data(ttl=3600, show_spinner=False)
def download_disclosure_text(api_key: str, rcept_no: str) -> str:
    url = f"{DART_BASE}/document.xml"
    r = dart_get(
        url,
        params={"crtfc_key": api_key, "rcept_no": rcept_no},
        timeout=90,
    )
    r.raise_for_status()

    try:
        zf = zipfile.ZipFile(io.BytesIO(r.content))
    except zipfile.BadZipFile:
        msg = decode_bytes(r.content)
        raise RuntimeError(f"공시 원문 ZIP을 열 수 없습니다.\n{msg[:400]}")

    candidates = []
    for name in zf.namelist():
        lower = name.lower()
        if lower.endswith((".xml", ".html", ".htm", ".txt")):
            info = zf.getinfo(name)
            # 비정상적으로 큰 단일 파일은 메모리 과다 사용 방지
            if info.file_size <= 20 * 1024 * 1024:
                candidates.append((name, info.file_size))

    if not candidates:
        raise RuntimeError("분석 가능한 감사보고서 원문 파일을 찾지 못했습니다.")

    # 텍스트 길이가 충분한 문서들을 우선적으로 합침
    parts = []
    total_chars = 0
    for name, _ in sorted(candidates, key=lambda x: x[1], reverse=True):
        raw = zf.read(name)
        txt = clean_text(decode_bytes(raw))
        if len(txt) < 50:
            continue
        parts.append(txt)
        total_chars += len(txt)
        if total_chars > 2_500_000:
            break

    return "\n\n".join(parts)


# =========================================================
# 분석
# =========================================================
def bank_hits_in_text(text: str):
    hits = []

    # 문맥 단위: 너무 짧은 줄은 인접 문장으로 합치기 위해 우선 문단 기반
    blocks = [b.strip() for b in re.split(r"\n{1,}", text) if b.strip()]
    blocks = [re.sub(r"\s+", " ", b) for b in blocks]

    for bank, patterns in BANK_PATTERNS.items():
        regex = re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)

        contexts = []
        categories = set()
        monies = []

        for i, block in enumerate(blocks):
            if not regex.search(block):
                continue

            # 표/문장 조각이 너무 짧으면 주변 1줄 결합
            pieces = []
            if i > 0:
                pieces.append(blocks[i - 1])
            pieces.append(block)
            if i + 1 < len(blocks):
                pieces.append(blocks[i + 1])

            context = " | ".join(pieces)
            context = context[:800]

            # 관련 거래 키워드가 같이 있는 문맥을 우선
            related = any(word in context for word in RELATED_WORDS)
            if related or len(contexts) < 3:
                if context not in contexts:
                    contexts.append(context)

            for cat, cat_patterns in CATEGORY_PATTERNS.items():
                if any(re.search(p, context, re.IGNORECASE) for p in cat_patterns):
                    categories.add(cat)

            monies.extend(money_tokens(context))

            if len(contexts) >= 8:
                break

        if contexts:
            unique_money = []
            for m in monies:
                if m not in unique_money:
                    unique_money.append(m)

            # "주거래은행" 단정 대신 증빙 강도만 표시
            evidence_score = len(categories) + min(len(contexts), 3)
            if evidence_score >= 5:
                strength = "높음"
            elif evidence_score >= 3:
                strength = "보통"
            else:
                strength = "낮음"

            hits.append({
                "은행": bank,
                "거래유형": ", ".join(sorted(categories)) if categories else "은행명 확인",
                "확인강도": strength,
                "금액표기": ", ".join(unique_money[:4]),
                "근거건수": len(contexts),
                "근거문구": "\n\n".join(contexts[:5]),
            })

    # 은행명이 특정되지 않았지만 금융기관 관련 문구가 있는 경우
    generic_contexts = []
    generic_regex = re.compile("|".join(map(re.escape, RELATED_WORDS)))
    for block in blocks:
        if generic_regex.search(block):
            if len(block) > 15:
                generic_contexts.append(block[:600])
        if len(generic_contexts) >= 10:
            break

    return hits, generic_contexts


def search_companies(corp_df: pd.DataFrame, query: str) -> pd.DataFrame:
    q = normalize_company_name(query)
    if not q:
        return corp_df.iloc[0:0].copy()

    exact = corp_df[corp_df["norm_name"] == q]
    if not exact.empty:
        return exact.head(20).copy()

    contains = corp_df[corp_df["norm_name"].str.contains(re.escape(q), na=False)]
    return contains.head(30).copy()


def company_label(row):
    stock = row.get("stock_code", "")
    stock_txt = f" / 종목코드 {stock}" if stock else ""
    return f'{row["corp_name"]} / DART {row["corp_code"]}{stock_txt}'


def analyze_company(api_key: str, corp_row: dict):
    corp_code = corp_row["corp_code"]
    corp_name = corp_row["corp_name"]

    company = get_company_info(api_key, corp_code)
    disclosures = get_disclosures(api_key, corp_code)
    report = pick_latest_audit_report(disclosures)

    base = {
        "법인명": corp_name,
        "DART고유번호": corp_code,
        "대표자": company.get("ceo_nm", ""),
        "사업자등록번호": company.get("bizr_no", ""),
        "법인등록번호": company.get("jurir_no", ""),
        "주소": company.get("adres", ""),
        "홈페이지": company.get("hm_url", ""),
    }

    if not report:
        return {
            "company": base,
            "report": None,
            "banks": [],
            "generic": [],
            "error": "최근 5년 공시에서 '감사보고서'를 찾지 못했습니다.",
        }

    rcept_no = str(report.get("rcept_no", ""))
    text = download_disclosure_text(api_key, rcept_no)
    banks, generic = bank_hits_in_text(text)

    report_info = {
        "보고서명": report.get("report_nm", ""),
        "접수일": report.get("rcept_dt", ""),
        "접수번호": rcept_no,
        "DART링크": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}",
    }

    return {
        "company": base,
        "report": report_info,
        "banks": banks,
        "generic": generic,
        "error": "",
    }


def make_excel(results):
    output = io.BytesIO()

    summary_rows = []
    bank_rows = []
    evidence_rows = []

    for result in results:
        c = result["company"]
        report = result.get("report") or {}

        summary_rows.append({
            **c,
            "감사보고서": report.get("보고서명", ""),
            "접수일": report.get("접수일", ""),
            "DART링크": report.get("DART링크", ""),
            "확인은행수": len(result.get("banks", [])),
            "오류/비고": result.get("error", ""),
        })

        for b in result.get("banks", []):
            bank_rows.append({
                "법인명": c["법인명"],
                "은행": b["은행"],
                "거래유형": b["거래유형"],
                "확인강도": b["확인강도"],
                "금액표기": b["금액표기"],
                "근거건수": b["근거건수"],
                "DART링크": report.get("DART링크", ""),
            })
            evidence_rows.append({
                "법인명": c["법인명"],
                "은행": b["은행"],
                "근거문구": b["근거문구"],
            })

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame(summary_rows).to_excel(writer, index=False, sheet_name="법인요약")
        pd.DataFrame(bank_rows).to_excel(writer, index=False, sheet_name="거래은행")
        pd.DataFrame(evidence_rows).to_excel(writer, index=False, sheet_name="근거문구")

        for ws in writer.book.worksheets:
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            # 과도하게 넓은 셀 방지
            for col in ws.columns:
                letter = col[0].column_letter
                max_len = 0
                for cell in col[:100]:
                    value = "" if cell.value is None else str(cell.value)
                    max_len = max(max_len, min(len(value), 60))
                ws.column_dimensions[letter].width = max(12, min(max_len + 2, 60))

    output.seek(0)
    return output.getvalue()


# =========================================================
# 로그인 (선택)
# =========================================================
APP_PASSWORD = str(get_secret("APP_PASSWORD", "")).strip()

if APP_PASSWORD:
    if "corp_finder_auth" not in st.session_state:
        st.session_state.corp_finder_auth = False

    if not st.session_state.corp_finder_auth:
        st.subheader("🔒 접속 비밀번호")
        pwd = st.text_input("비밀번호", type="password")
        if st.button("접속", type="primary"):
            if pwd == APP_PASSWORD:
                st.session_state.corp_finder_auth = True
                st.rerun()
            else:
                st.error("비밀번호가 맞지 않습니다.")
        st.stop()


# =========================================================
# API 키
# =========================================================
api_key = str(get_secret("DART_API_KEY", "")).strip()

if not api_key:
    st.error("DART API 키가 설정되지 않았습니다. Streamlit Cloud의 App settings → Secrets에 `DART_API_KEY = \"발급받은키\"`를 등록한 뒤 Reboot app 해주세요.")
    st.stop()

if len(api_key) != 40:
    st.warning(f"DART API 키 길이가 일반적인 40자리와 다릅니다. 현재 {len(api_key)}자리입니다. Secrets 값에 따옴표 외의 공백/문자가 들어갔는지 확인해 주세요.")


# =========================================================
# 법인 코드 불러오기
# =========================================================
try:
    with st.spinner("DART 법인 목록을 불러오는 중입니다..."):
        corp_df = load_corp_codes(api_key)
except Exception as e:
    st.error(f"DART 연결 실패: {type(e).__name__}: {e}")
    st.info("로컬에서는 정상인데 Streamlit Cloud에서만 실패하면, App settings → Secrets의 DART_API_KEY가 현재 배포 앱에도 저장되어 있는지 확인한 뒤 Reboot app 해주세요. 이 버전은 Cloud에서는 기본 TLS를 사용하고, Windows의 DH_KEY_TOO_SMALL 오류가 있을 때만 SSL 호환 모드로 자동 재시도합니다.")
    st.stop()

try:
    init_local_db()
    sync_corp_master(corp_df)
except Exception as e:
    st.error(f"로컬 주소 DB 초기화 실패: {e}")
    st.stop()


# =========================================================
# UI
# =========================================================
name_tab, region_tab, bulk_tab, db_tab = st.tabs([
    "🔎 법인명 조회", "📍 본점 소재지 검색", "📚 여러 법인 일괄 조회", "🗄️ 주소 DB 관리"
])


with name_tab:
    st.subheader("회사명으로 검색")
    query = st.text_input(
        "법인명",
        placeholder="예: 다셀주식회사, 우진교통, 신흥기업",
        key="single_query",
    )

    if query:
        matches = search_companies(corp_df, query)
        if matches.empty:
            st.warning("DART 고유번호 목록에서 회사를 찾지 못했습니다.")
        else:
            options = matches.to_dict("records")
            labels = [company_label(x) for x in options]
            selected_label = st.selectbox("회사 선택", labels, key="name_company_select")
            selected = options[labels.index(selected_label)]

            info = None
            try:
                info = get_company_info(api_key, selected["corp_code"])
                save_company_profile(selected, info, "")
            except Exception:
                pass

            if info:
                cols = st.columns(4)
                cols[0].metric("대표자", info.get("ceo_nm") or "-")
                cols[1].metric("사업자번호", info.get("bizr_no") or "-")
                cols[2].metric("법인번호", info.get("jurir_no") or "-")
                cols[3].metric("업종코드", info.get("induty_code") or "-")
                if info.get("adres"):
                    st.write(f"**본점 주소:** {info['adres']}")

            if st.button("최근 감사보고서 분석", type="primary", key="single_analyze"):
                try:
                    with st.spinner("최근 감사보고서를 찾고 금융기관 관련 내용을 분석 중입니다..."):
                        result = analyze_company(api_key, selected)
                    st.session_state["single_result"] = result
                except Exception as e:
                    st.error(f"분석 중 오류: {e}")

    result = st.session_state.get("single_result")
    if result:
        st.divider()

        c = result["company"]
        r = result.get("report")

        st.subheader(c["법인명"])
        info_cols = st.columns(4)
        info_cols[0].metric("대표자", c.get("대표자") or "-")
        info_cols[1].metric("사업자번호", c.get("사업자등록번호") or "-")
        info_cols[2].metric("DART 코드", c.get("DART고유번호") or "-")
        info_cols[3].metric("확인 은행 수", len(result.get("banks", [])))

        if c.get("주소"):
            st.write(f"**주소:** {c['주소']}")

        if r:
            st.write(f"**최근 감사보고서:** {r['보고서명']} · 접수일 {r['접수일']}")
            st.link_button("DART 원문 열기", r["DART링크"])
        else:
            st.warning(result.get("error", "감사보고서를 찾지 못했습니다."))

        banks = result.get("banks", [])
        if banks:
            render_bank_analysis(banks, "🏦 감사보고서에서 확인된 금융기관")
        elif r:
            st.warning("감사보고서 원문에서 등록된 은행명을 찾지 못했습니다.")

        generic = result.get("generic", [])
        if generic:
            with st.expander("은행명이 특정되지 않은 금융기관 관련 문구"):
                for txt in generic[:10]:
                    st.write("• " + txt)

        excel = make_excel([result])
        st.download_button(
            "📥 분석 결과 엑셀 다운로드",
            data=excel,
            file_name=f"{c['법인명']}_법인Finder.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


with region_tab:
    st.subheader("본점 소재지 / 사업장 소재지로 검색")
    st.caption(
        "DART 주소 DB 또는 외부 기업목록(CSV/Excel)을 이용해 회사를 찾은 뒤, "
        "DART 법인과 매칭하여 이전 버전과 동일하게 거래은행·차입금·담보·예금·보증/약정 정보를 분석합니다."
    )

    total_cnt, addr_cnt, fail_cnt = address_db_stats()
    c1, c2, c3 = st.columns(3)
    c1.metric("DART 전체 법인", f"{total_cnt:,}")
    c2.metric("주소 DB 구축", f"{addr_cnt:,}")
    c3.metric("수집 오류", f"{fail_cnt:,}")

    if addr_cnt == 0:
        st.warning(
            "현재 DART 주소 DB가 0건입니다. DART 전체 법인목록에는 주소가 포함되어 있지 않아 "
            "본점 소재지 검색을 하려면 회사별 기업개황을 먼저 수집해야 합니다."
        )
        qa, qb = st.columns([1.2, 2.8])
        quick_batch = qa.selectbox(
            "빠른 주소수집", [50, 100, 200, 300], index=1, key="region_quick_batch"
        )
        if qb.button("🚀 DART 주소 DB 지금 구축", type="primary", key="region_quick_build"):
            ok, fail = build_address_cache(api_key, quick_batch, True, False)
            if ok or fail:
                st.success(f"주소 DB 구축: 성공 {ok:,}개 / 오류 {fail:,}개")
                st.rerun()
            else:
                st.info("추가로 수집할 법인이 없습니다.")
        st.info(
            "청주시 제조업체를 빠르게 찾는 목적이라면 DART 주소 DB를 11만 건 넘게 구축하는 것보다 "
            "아래 '외부 기업목록'에 충청북도/청주시 제조업체 총람을 올리는 방식이 훨씬 빠릅니다. "
            "외부 목록에서 회사를 고른 뒤 DART 법인과 매칭해 감사보고서·거래은행 분석으로 연결할 수 있습니다."
        )
    elif addr_cnt < 1000:
        st.caption(
            f"현재 본점주소 {addr_cnt:,}건만 수집되어 있어 특정 지역의 결과가 일부 누락될 수 있습니다. "
            "더 넓게 찾으려면 주소 DB를 추가 구축하거나 외부 기업목록을 함께 사용하세요."
        )

    # 처음 실행할 때는 별도 파일 업로드를 요구하지 않는 DART 주소 DB를 기본값으로 둡니다.
    # 외부 목록은 사용자가 명시적으로 선택했을 때만 업로드 영역을 보여 줍니다.
    source_mode = st.radio(
        "검색 데이터",
        ["DART 주소 DB", "외부 기업목록", "둘 다"],
        horizontal=True,
        key="region_source_mode",
        help="DART 주소 DB=본점 소재지, 외부 기업목록=본점/공장/사업장 등 파일에 들어 있는 주소 기준",
    )

    if source_mode == "DART 주소 DB" and addr_cnt == 0:
        st.info(
            "현재는 DART 본점주소가 아직 0건입니다. 위의 '🚀 DART 주소 DB 지금 구축'을 먼저 눌러 주세요. "
            "주소는 DART 전체 법인목록에 포함되어 있지 않아 회사별 기업개황을 한 번씩 수집해야 합니다."
        )

    if source_mode in ("외부 기업목록", "둘 다"):
        uploaded = st.file_uploader(
            "기업목록 CSV / Excel",
            type=["csv", "xlsx", "xls"],
            key="external_company_file",
            help="회사명과 주소가 들어 있는 파일이면 대부분 자동으로 열을 인식합니다.",
        )
        if uploaded is not None:
            file_sig = f"{uploaded.name}:{uploaded.size}"
            if st.session_state.get("external_file_sig") != file_sig:
                try:
                    ext_df, mapped = read_external_company_file(uploaded)
                    st.session_state["external_company_df"] = ext_df
                    st.session_state["external_file_sig"] = file_sig
                    st.session_state["external_mapped_cols"] = mapped
                except Exception as e:
                    st.error(f"외부 기업목록 읽기 실패: {e}")
            if "external_company_df" in st.session_state:
                st.success(f"외부 기업목록 {len(st.session_state['external_company_df']):,}개 불러옴")
                with st.expander("인식된 열 확인"):
                    st.json(st.session_state.get("external_mapped_cols", {}))

        st.link_button(
            "📥 2025 충청북도 제조업체 총람 다운로드 페이지",
            "https://www.chungbuk.go.kr/www/contents.do?key=5382",
        )
        st.caption(
            "사용 순서: 위 페이지에서 '청주시 제조업체총람' 다운로드 → 이 화면의 기업목록에 업로드 → "
            "청주시/구/읍·면·동 검색. 이 자료는 사업장/공장 소재지 기준이며 DART 본점주소와는 의미가 다를 수 있습니다."
        )

    st.markdown("#### 지역 조건")
    r1, r2, r3, r4 = st.columns([1.2, 1, 1, 1])
    province = r1.selectbox("시·도", REGION_PROVINCES, index=REGION_PROVINCES.index("충청북도"), key="region_province")
    city = r2.text_input("시·군", value="청주시" if province == "충청북도" else "", placeholder="예: 청주시", key="region_city")
    if province == "충청북도" and "청주시" in city:
        district = r3.selectbox("구", CHEONGJU_DISTRICTS, key="region_district")
    else:
        district = r3.text_input("구", placeholder="예: 흥덕구", key="region_district_text")
    town = r4.text_input("읍·면·동 / 상세지역", placeholder="예: 오창읍", key="region_town")

    name_filter = st.text_input("법인명 추가 필터 (선택)", placeholder="예: 테크, 산업, 전자", key="region_name_filter")
    max_rows = st.select_slider("최대 검색 결과", options=[100, 300, 500, 1000, 3000], value=500, key="region_max_rows")

    if st.button("🔍 지역 법인 검색", type="primary", key="region_search_btn"):
        terms = region_terms_from_inputs(province, city, district, town)
        result_frames = []

        if source_mode in ("DART 주소 DB", "둘 다"):
            if addr_cnt == 0:
                st.warning("DART 본점주소 DB가 0건이라 DART 지역검색 결과는 없습니다. 위의 주소 DB 구축을 먼저 실행해 주세요.")
            dart_loc = get_profile_rows_for_search(terms, limit=max_rows)
            if not dart_loc.empty:
                if name_filter:
                    q = normalize_company_name(name_filter)
                    dart_loc = dart_loc[dart_loc["corp_name"].map(normalize_company_name).str.contains(re.escape(q), na=False)]
                dart_show = pd.DataFrame({
                    "출처": "DART",
                    "법인명": dart_loc["corp_name"],
                    "대표자": dart_loc["ceo_nm"],
                    "주소": dart_loc["adres"],
                    "업종": dart_loc["induty_code"],
                    "사업자등록번호": dart_loc["bizr_no"],
                    "법인등록번호": dart_loc["jurir_no"],
                    "DART고유번호": dart_loc["corp_code"],
                    "전화번호": dart_loc["phn_no"],
                })
                result_frames.append(dart_show)

        if source_mode in ("외부 기업목록", "둘 다"):
            ext_df = st.session_state.get("external_company_df")
            if ext_df is None or ext_df.empty:
                if source_mode == "외부 기업목록":
                    st.warning("외부 기업목록을 먼저 업로드해 주세요.")
                else:
                    st.caption("외부 기업목록이 없어 이번 검색에서는 DART 주소 DB만 사용합니다.")
            else:
                ext_loc = filter_external_by_region(ext_df, terms, name_filter, limit=max_rows)
                if not ext_loc.empty:
                    ext_show = pd.DataFrame({
                        "출처": "외부목록",
                        "법인명": ext_loc["법인명"],
                        "대표자": ext_loc["대표자"],
                        "주소": ext_loc["주소"],
                        "업종": ext_loc["업종"],
                        "사업자등록번호": ext_loc["사업자등록번호"],
                        "법인등록번호": ext_loc["법인등록번호"],
                        "DART고유번호": "",
                        "전화번호": ext_loc["전화번호"],
                    })
                    result_frames.append(ext_show)

        if result_frames:
            combined = pd.concat(result_frames, ignore_index=True)
            combined["_dedupe"] = combined["법인명"].map(normalize_company_name) + "|" + combined["주소"].astype(str)
            combined = combined.drop_duplicates("_dedupe").drop(columns="_dedupe").reset_index(drop=True)
            st.session_state["region_search_results"] = combined.head(max_rows)
        else:
            st.session_state["region_search_results"] = pd.DataFrame()

    region_results = st.session_state.get("region_search_results", pd.DataFrame())
    if isinstance(region_results, pd.DataFrame) and not region_results.empty:
        st.markdown(f"### 검색 결과 · {len(region_results):,}개")
        st.caption("표에서 회사를 한 줄 클릭하면 아래에서 DART 법인을 매칭한 뒤 거래은행·차입금 분석을 할 수 있습니다.")
        event = st.dataframe(
            region_results,
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="region_results_grid",
        )

        selected_indices = []
        try:
            selected_indices = list(event.selection.rows)
        except Exception:
            selected_indices = []

        if selected_indices:
            idx = selected_indices[0]
            picked = region_results.iloc[idx].to_dict()
            previous = st.session_state.get("region_picked_company") or {}
            previous_key = (str(previous.get("법인명", "")), str(previous.get("주소", "")))
            picked_key = (str(picked.get("법인명", "")), str(picked.get("주소", "")))
            if picked_key != previous_key:
                # 다른 회사를 클릭했는데 직전 회사의 은행 분석 결과가 남아 보이지 않도록 초기화
                st.session_state.pop("region_analysis_result", None)
            st.session_state["region_picked_company"] = picked

        picked = st.session_state.get("region_picked_company")
        if picked:
            st.markdown("#### 선택 회사")
            st.write(f"**{picked.get('법인명','')}**")
            st.write(f"주소: {picked.get('주소') or '-'}")
            if picked.get("업종"):
                st.write(f"업종/제품: {picked.get('업종')}")

            direct_code = str(picked.get("DART고유번호", "")).strip()
            candidate_rows = []
            if direct_code:
                hit = corp_df[corp_df["corp_code"].astype(str) == direct_code]
                candidate_rows = hit.to_dict("records")
            else:
                candidate_rows = dart_match_for_name(corp_df, picked.get("법인명", ""))

            if not candidate_rows:
                st.warning("DART에서 같은 이름의 공시대상 법인을 찾지 못했습니다. 외부 목록의 모든 회사가 DART 공시대상인 것은 아닙니다.")
            else:
                labels = [company_label(x) for x in candidate_rows]
                sel_label = st.selectbox("DART 법인 매칭", labels, key="region_dart_match_select")
                selected_corp = candidate_rows[labels.index(sel_label)]

                st.info("이 회사를 선택하면 이전 버전과 동일하게 감사보고서에서 거래은행과 금융거래 근거를 찾아줍니다.")
                if st.button("🏦 거래은행 · 차입금 · 담보 분석", type="primary", key="region_analyze_btn"):
                    try:
                        with st.spinner("DART 감사보고서에서 거래은행·차입금·담보·예금·약정 정보를 분석 중입니다..."):
                            result = analyze_company(api_key, selected_corp)
                        st.session_state["region_analysis_result"] = result
                        try:
                            profile = get_company_info(api_key, selected_corp["corp_code"])
                            save_company_profile(selected_corp, profile, "")
                        except Exception:
                            pass
                    except Exception as e:
                        st.error(f"분석 중 오류: {e}")

        rr = st.session_state.get("region_analysis_result")
        if rr:
            st.divider()
            c = rr["company"]
            r = rr.get("report")
            st.markdown(f"### {c['법인명']} 분석")
            cols = st.columns(4)
            cols[0].metric("대표자", c.get("대표자") or "-")
            cols[1].metric("사업자번호", c.get("사업자등록번호") or "-")
            cols[2].metric("법인번호", c.get("법인등록번호") or "-")
            cols[3].metric("확인은행", len(rr.get("banks", [])))
            st.write(f"**본점 주소:** {c.get('주소') or '-'}")
            if r:
                st.write(f"**최근 감사보고서:** {r['보고서명']} · {r['접수일']}")
                st.link_button("DART 원문 열기", r["DART링크"], key="region_dart_link")
            if rr.get("banks"):
                render_bank_analysis(rr["banks"], "🏦 감사보고서에서 확인된 거래 금융기관")
            elif r:
                st.warning("감사보고서 원문에서 등록된 은행명을 찾지 못했습니다.")
            elif rr.get("error"):
                st.warning(rr["error"])

            generic = rr.get("generic", [])
            if generic:
                with st.expander("은행명이 특정되지 않은 금융기관 관련 문구"):
                    for txt in generic[:10]:
                        st.write("• " + txt)

            st.download_button(
                "📥 선택 회사 분석 엑셀",
                data=make_excel([rr]),
                file_name=f"{c['법인명']}_법인Finder.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="region_excel_download",
            )

        st.caption("지역 검색은 기업을 찾는 단계이고, 실제 거래은행 정보는 선택한 법인의 최신 감사보고서를 분석한 뒤 표시됩니다.")

        csv_data = region_results.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
        st.download_button(
            "📥 지역 검색결과 CSV 다운로드",
            data=csv_data,
            file_name="법인Finder_지역검색결과.csv",
            mime="text/csv",
            key="region_csv_download",
        )
    elif "region_search_results" in st.session_state:
        ext_ready = isinstance(st.session_state.get("external_company_df"), pd.DataFrame) and not st.session_state.get("external_company_df").empty
        if source_mode == "DART 주소 DB" and addr_cnt == 0:
            st.info(
                "아직 검색할 DART 본점주소 데이터가 없습니다. 화면 위쪽의 '🚀 DART 주소 DB 지금 구축'을 먼저 실행해 주세요."
            )
        elif source_mode == "외부 기업목록" and not ext_ready:
            st.info("먼저 청주시 제조업체총람 같은 CSV/Excel 기업목록을 업로드해 주세요.")
        elif source_mode == "둘 다" and addr_cnt == 0 and not ext_ready:
            st.info(
                "현재 검색 가능한 주소 데이터가 없습니다. DART 주소 DB를 구축하거나 외부 기업목록을 업로드하면 지역검색을 시작할 수 있습니다."
            )
        else:
            st.info(
                "조건에 맞는 회사를 찾지 못했습니다. DART 주소 DB가 일부만 구축된 경우 결과가 누락될 수 있으므로 "
                "주소 DB를 더 구축하거나 외부 기업목록을 함께 사용해 주세요."
            )


with bulk_tab:
    st.subheader("여러 회사 일괄 분석")
    bulk_text = st.text_area(
        "회사명을 한 줄에 하나씩 입력",
        placeholder="다셀주식회사\n우진교통\n신흥기업",
        height=170,
    )
    st.caption("한 번에 최대 20개 법인을 권장합니다.")

    if st.button("일괄 분석 시작", type="primary", key="bulk_analyze"):
        names = [x.strip() for x in bulk_text.splitlines() if x.strip()]
        names = list(dict.fromkeys(names))[:20]

        if not names:
            st.warning("회사명을 입력해 주세요.")
        else:
            results = []
            progress = st.progress(0)
            status = st.empty()

            for idx, name in enumerate(names, start=1):
                status.write(f"{idx}/{len(names)} · {name} 확인 중...")
                matches = search_companies(corp_df, name)

                if matches.empty:
                    results.append({
                        "company": {
                            "법인명": name,
                            "DART고유번호": "",
                            "대표자": "",
                            "사업자등록번호": "",
                            "법인등록번호": "",
                            "주소": "",
                            "홈페이지": "",
                        },
                        "report": None,
                        "banks": [],
                        "generic": [],
                        "error": "DART 법인 매칭 실패",
                    })
                elif len(matches) > 1:
                    results.append({
                        "company": {
                            "법인명": name,
                            "DART고유번호": "",
                            "대표자": "",
                            "사업자등록번호": "",
                            "법인등록번호": "",
                            "주소": "",
                            "홈페이지": "",
                        },
                        "report": None,
                        "banks": [],
                        "generic": [],
                        "error": f"동명/유사 법인 {len(matches)}개 - 법인명 조회에서 정확한 법인을 선택해 주세요.",
                    })
                else:
                    selected = matches.iloc[0].to_dict()
                    try:
                        result = analyze_company(api_key, selected)
                        results.append(result)
                        try:
                            profile = get_company_info(api_key, selected["corp_code"])
                            save_company_profile(selected, profile, "")
                        except Exception:
                            pass
                    except Exception as e:
                        results.append({
                            "company": {
                                "법인명": selected.get("corp_name", name),
                                "DART고유번호": selected.get("corp_code", ""),
                                "대표자": "",
                                "사업자등록번호": "",
                                "법인등록번호": "",
                                "주소": "",
                                "홈페이지": "",
                            },
                            "report": None,
                            "banks": [],
                            "generic": [],
                            "error": str(e),
                        })

                progress.progress(idx / len(names))

            status.empty()
            st.session_state["bulk_results"] = results

    results = st.session_state.get("bulk_results", [])
    if results:
        st.divider()
        st.markdown("### 📊 일괄 분석 결과")

        rows = []
        for result in results:
            c = result["company"]
            r = result.get("report") or {}
            banks = result.get("banks", [])
            rows.append({
                "법인명": c.get("법인명", ""),
                "대표자": c.get("대표자", ""),
                "주소": c.get("주소", ""),
                "최근감사보고서": r.get("보고서명", ""),
                "접수일": r.get("접수일", ""),
                "확인은행": ", ".join(b["은행"] for b in banks),
                "확인은행수": len(banks),
                "비고": result.get("error", ""),
            })

        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        excel = make_excel(results)
        st.download_button(
            "📥 전체 결과 엑셀 다운로드",
            data=excel,
            file_name="법인Finder_일괄분석.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


with db_tab:
    st.subheader("DART 본점주소 DB 구축 / 백업")
    total_cnt, addr_cnt, fail_cnt = address_db_stats()
    cols = st.columns(4)
    cols[0].metric("DART 법인", f"{total_cnt:,}")
    cols[1].metric("주소 수집 완료", f"{addr_cnt:,}")
    cols[2].metric("수집 오류", f"{fail_cnt:,}")
    pct = (addr_cnt / total_cnt * 100) if total_cnt else 0
    cols[3].metric("진행률", f"{pct:.1f}%")

    st.info(
        "DART 고유번호 목록에는 주소가 없어서 기업개황 API를 회사별로 조회해야 합니다. "
        "따라서 전체 주소 DB는 한 번에 만들기보다 아래 배치 단위로 누적하는 방식입니다. "
        "OpenDART 호출량 제한에 걸릴 수 있으므로 배치 구축이 안전합니다. "
        "법인명 조회/감사보고서 분석을 한 회사는 자동으로 주소 DB에도 저장됩니다."
    )

    b1, b2, b3 = st.columns([1, 1, 1])
    batch_size = b1.selectbox("이번에 수집할 회사 수", [20, 50, 100, 200, 300, 500, 1000], index=2, key="db_batch_size")
    listed_first = b2.checkbox("상장사 우선", value=True, key="db_listed_first")
    retry_errors = b3.checkbox("오류 회사 재시도 포함", value=False, key="db_retry_errors")

    if st.button("주소 DB 이어서 구축", type="primary", key="db_build_btn"):
        ok, fail = build_address_cache(api_key, batch_size, listed_first, retry_errors)
        if ok == 0 and fail == 0:
            st.success("추가로 수집할 회사가 없습니다.")
        else:
            st.success(f"이번 실행: 성공 {ok:,}개 / 오류 {fail:,}개")
            st.rerun()

    st.markdown("#### 주소 DB 백업 / 복원")
    st.caption("Streamlit Cloud 재배포 시 로컬 파일이 초기화될 수 있으므로, 주소 DB는 가끔 엑셀로 내려받아 두는 것을 권장합니다.")

    st.download_button(
        "📥 주소 DB 엑셀 백업",
        data=export_address_cache_excel(),
        file_name="법인Finder_주소DB.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="db_export_btn",
    )

    restore_file = st.file_uploader("주소 DB 백업파일 복원", type=["xlsx", "xls", "csv"], key="db_restore_file")
    if restore_file is not None and st.button("주소 DB 복원 실행", key="db_restore_btn"):
        try:
            count = import_address_cache_file(restore_file)
            st.success(f"주소 DB {count:,}건을 복원했습니다.")
            st.rerun()
        except Exception as e:
            st.error(f"복원 실패: {e}")


st.divider()
st.caption(
    "법인 Finder는 DART 및 사용자가 불러온 공공/업무용 기업목록을 결합해 법인 탐색과 DART 공시 분석을 돕는 업무보조 도구입니다. "
    "특정 은행명이 기재되어 있다는 사실만으로 해당 은행을 주거래은행이라고 단정할 수 없으며, "
    "최종 판단은 감사보고서 원문과 회사 확인을 통해 검증해야 합니다."
)
