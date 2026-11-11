"""Conservative DART document and legal-entity matching checks.

No name-only matching is considered verified identity. The caller must show
all available legal identifiers and require manual confirmation when needed.
"""
import re

_AUDIT_TITLE = re.compile(
    r"^(?:\[[^\]]+\]\s*)*"
    r"(?:감사보고서|연결감사보고서|결합감사보고서|"
    r"audit\s*report|auditor['’]?s?\s*report|"
    r"independent\s*auditor['’]?s?\s*report)"
    r"(?:\s*\([^)]*\))?\s*$",
    re.IGNORECASE,
)
_PERIOD = re.compile(r"\((20\d{2})(?:[.\-/년\s]*(0?[1-9]|1[0-2]))?(?:\s*월)?\)")


def is_audit_report_title(title):
    """Only standalone audit reports, not incidental mentions in other notices."""
    return bool(_AUDIT_TITLE.fullmatch(str(title or "").strip()))


def _rank_audit_report(row):
    title = str(row.get("report_nm") or "").strip()
    received = re.sub(r"\D", "", str(row.get("rcept_dt") or ""))[:8]
    period = _PERIOD.search(title)
    if period:
        year, month = int(period.group(1)), int(period.group(2) or 12)
    elif len(received) >= 4:
        # The filing date is not the fiscal period. This is only a fallback
        # for reports whose titles omit an explicit accounting period.
        year, month = int(received[:4]) - 1, 12
    else:
        year, month = 0, 0
    bare = re.sub(r"^(?:\[[^\]]+\]\s*)*", "", title)
    standalone = int(bare.lower().startswith(("감사보고서", "audit report", "auditor", "independent")))
    return year, month, standalone, received, str(row.get("rcept_no") or "")


def pick_audit_report_rows(rows):
    candidates = [dict(row) for row in rows if is_audit_report_title(row.get("report_nm"))]
    return max(candidates, key=_rank_audit_report) if candidates else None


def _number(value):
    return re.sub(r"\D", "", str(value or ""))


def _legal_name(value):
    value = re.sub(r"\s+", "", str(value or "")).lower()
    return value.replace("주식회사", "").replace("(주)", "").replace("㈜", "")


def assess_external_dart_match(external, dart, profile):
    """Return (status, explanation): blocked / confirm.

    The external row may identify a factory or sole proprietor, while DART
    identifies a different legal entity. An address is never an identity key.
    """
    name = str(external.get("법인명") or "")
    legal_name = str(dart.get("corp_name") or profile.get("corp_name") or "")
    fields = (
        ("사업자등록번호", "bizr_no", "사업자등록번호"),
        ("법인등록번호", "jurir_no", "법인등록번호"),
    )
    matched_numbers = []
    for ext_key, dart_key, desc in fields:
        ext_value = _number(external.get(ext_key))
        dart_value = _number(profile.get(dart_key))
        if ext_value and dart_value:
            if ext_value != dart_value:
                return "blocked", f"{desc}가 서로 다릅니다. 같은 기업으로 연결할 수 없습니다."
            matched_numbers.append(desc)
    if _legal_name(name) != _legal_name(legal_name) and not matched_numbers:
        return "blocked", "회사명이 다르며 동일한 사업자·법인등록번호도 확인되지 않았습니다. 이름 유사성만으로 분석할 수 없습니다."
    reason = ("일치한 고유 식별정보: " + ", ".join(matched_numbers)) if matched_numbers else "법인명만 일치하며 고유 식별번호로 동일성이 확인되지 않았습니다."
    return "confirm", reason
