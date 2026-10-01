
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import pandas as pd
import requests
import streamlit as st
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
st.caption("OpenDART 감사보고서 기반 · 거래 금융기관 / 차입금 / 담보 / 예금 확인")

DART_BASE = "https://opendart.fss.or.kr/api"

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
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


def normalize_company_name(name: str) -> str:
    if not name:
        return ""
    x = re.sub(r"\s+", "", str(name))
    x = x.replace("주식회사", "").replace("(주)", "").replace("㈜", "")
    return x.lower()


def request_json(url, params, timeout=30):
    r = requests.get(url, params=params, timeout=timeout)
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
# DART API
# =========================================================
@st.cache_data(ttl=86400, show_spinner=False)
def load_corp_codes(api_key: str) -> pd.DataFrame:
    url = f"{DART_BASE}/corpCode.xml"
    r = requests.get(url, params={"crtfc_key": api_key}, timeout=60)
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
    r = requests.get(
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

with st.sidebar:
    st.header("설정")
    if api_key:
        st.success("DART API 키 연결됨")
    else:
        st.warning("DART API 키가 필요합니다.")

    st.markdown(
        """
**Streamlit Cloud → Settings → Secrets**

```toml
DART_API_KEY = "발급받은키"
APP_PASSWORD = "원하는비밀번호"
```

`APP_PASSWORD`는 선택사항입니다.
"""
    )

if not api_key:
    st.info("왼쪽 안내대로 Streamlit Secrets에 `DART_API_KEY`를 등록한 뒤 앱을 재실행해 주세요.")
    st.stop()


# =========================================================
# 법인 코드 불러오기
# =========================================================
try:
    with st.spinner("DART 법인 목록을 불러오는 중입니다..."):
        corp_df = load_corp_codes(api_key)
except Exception as e:
    st.error(f"DART 연결 실패: {e}")
    st.stop()


# =========================================================
# UI
# =========================================================
single_tab, bulk_tab = st.tabs(["🔎 단일 법인 조회", "📚 여러 법인 일괄 조회"])


with single_tab:
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
            selected_label = st.selectbox("회사 선택", labels)
            selected = options[labels.index(selected_label)]

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
            st.markdown("### 🏦 감사보고서에서 확인된 금융기관")
            show_df = pd.DataFrame(banks)[
                ["은행", "거래유형", "확인강도", "금액표기", "근거건수"]
            ]
            st.dataframe(show_df, use_container_width=True, hide_index=True)

            st.caption(
                "※ '확인강도'는 감사보고서 안에서 해당 은행명과 차입·담보·예금·약정 키워드가 "
                "함께 확인되는 정도를 뜻하며, '주거래은행'을 단정하는 지표가 아닙니다."
            )

            with st.expander("은행별 근거 문구 보기"):
                for b in banks:
                    st.markdown(f"#### {b['은행']} · {b['거래유형']}")
                    st.text(b["근거문구"])
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


with bulk_tab:
    st.subheader("여러 회사 일괄 분석")
    bulk_text = st.text_area(
        "회사명을 한 줄에 하나씩 입력",
        placeholder="다셀주식회사\n우진교통\n신흥기업",
        height=170,
    )
    st.caption("1차 버전은 한 번에 최대 20개 법인을 권장합니다.")

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
                        "error": f"동명/유사 법인 {len(matches)}개 - 단일 조회에서 정확한 법인을 선택해 주세요.",
                    })
                else:
                    selected = matches.iloc[0].to_dict()
                    try:
                        results.append(analyze_company(api_key, selected))
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


st.divider()
st.caption(
    "법인 Finder는 DART 공시 원문에서 금융기관 관련 정보를 탐색하는 업무보조 도구입니다. "
    "특정 은행명이 기재되어 있다는 사실만으로 해당 은행을 주거래은행이라고 단정할 수 없으며, "
    "최종 판단은 감사보고서 원문과 회사 확인을 통해 검증해야 합니다."
)
