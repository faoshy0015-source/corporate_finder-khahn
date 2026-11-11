"""Regression checks for DART entity identity and true audit filings."""
from dart_integrity import (
    assess_external_dart_match, is_audit_report_title, pick_audit_report_rows,
)


def test_only_genuine_audit_reports_selected():
    assert is_audit_report_title("감사보고서 (2025.12)")
    assert is_audit_report_title("[기재정정] 연결감사보고서 (2024.12)")
    assert not is_audit_report_title("소규모합병에 따른 감사보고서 관련 안내")
    assert not is_audit_report_title("감사보고서제출")
    assert not is_audit_report_title("분기보고서 (2026.09)")


def test_newer_fiscal_period_beats_later_correction_to_old_report():
    reports = [
        {"report_nm": "감사보고서 (2025.12)", "rcept_dt": "20260324", "rcept_no": "20260324000001"},
        {"report_nm": "[기재정정] 감사보고서 (2024.12)", "rcept_dt": "20261101", "rcept_no": "20261101000001"},
        {"report_nm": "감사보고서 관련 소규모공시", "rcept_dt": "20261105", "rcept_no": "20261105000001"},
    ]
    assert pick_audit_report_rows(reports)["rcept_no"] == "20260324000001"


def test_same_period_prefers_individual_company_audit():
    reports = [
        {"report_nm": "연결감사보고서 (2025.12)", "rcept_dt": "20260330", "rcept_no": "z"},
        {"report_nm": "감사보고서 (2025.12)", "rcept_dt": "20260320", "rcept_no": "a"},
    ]
    assert pick_audit_report_rows(reports)["rcept_no"] == "a"


def test_daeho_machine_not_auto_mapped_to_different_dart_name():
    external = {"법인명": "대호기계", "주소": "충청북도 청주시 청원구 토성로 104"}
    dart = {"corp_name": "대호기계공업", "corp_code": "00870764"}
    assert assess_external_dart_match(external, dart, {"adres": "전북 익산시"})[0] == "blocked"


def test_matching_name_does_not_override_conflicting_registration():
    external = {"법인명": "대호기계", "사업자등록번호": "301-15-30115"}
    dart = {"corp_name": "대호기계"}
    assert assess_external_dart_match(external, dart, {"bizr_no": "1234567890"})[0] == "blocked"


def test_matching_business_registration_can_confirm_alias_after_manual_review():
    external = {"법인명": "ABC공장", "사업자등록번호": "123-45-67890"}
    dart = {"corp_name": "ABC주식회사"}
    assert assess_external_dart_match(external, dart, {"bizr_no": "1234567890"})[0] == "confirm"
