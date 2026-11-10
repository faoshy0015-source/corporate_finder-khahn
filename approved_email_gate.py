"""Shared, server-verified email access gate for public HanaV Streamlit apps.

Deployment is deliberately two-phase: EMAIL_ACCESS_MODE=legacy (default)
keeps the existing app-password login; EMAIL_ACCESS_MODE=enforced activates
Supabase email OTP + a central RLS-protected grants table. In enforced mode
the absence of any dependency fails closed, never silently reverts.

No access is granted based on typed email, Streamlit session flags, decoded
JWT without server verification, or the legacy APP_PASSWORD alone.
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

import requests

APPS = frozenset({
    "stock-analyzer", "hanav-trading", "corporate-finder",
    "pension-tax", "private-portfolio",
})
SESSION_KEY = "_hanav_approved_email_session"
PENDING_KEY = "_hanav_approved_email_pending"
SEND_KEY = "_hanav_approved_email_last_sent"
OTP_DELAY_SECONDS = 60

EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
OTP_RE = re.compile(r"^[0-9]{6,10}$")


class AccessError(RuntimeError):
    """Safe user-visible error; never includes raw provider response content."""


def normalized_email(value):
    return value.strip().lower() if isinstance(value, str) else ""


def email_mode(secrets):
    """Unknown values must never downgrade enforcement to legacy."""
    try:
        mode = str(secrets.get("EMAIL_ACCESS_MODE", "legacy") or "").strip().lower()
    except Exception:
        mode = "invalid"
    if mode not in ("legacy", "enforced"):
        raise AccessError("EMAIL_ACCESS_MODE 설정 오류: legacy 또는 enforced만 가능합니다.")
    return mode


def _secret(secrets, key):
    try:
        v = secrets.get(key)
    except Exception:
        return ""
    return str(v or "").strip() if isinstance(v, str) else ""


def auth_config(secrets):
    url = _secret(secrets, "SUPABASE_URL").rstrip("/")
    anon = _secret(secrets, "SUPABASE_ANON_KEY")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.netloc
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise AccessError("Supabase 프로젝트 URL(SUPABASE_URL) 설정을 확인하세요.")
    if not anon:
        raise AccessError(
            "인증용 SUPABASE_ANON_KEY가 없습니다. "
            "Streamlit Secrets에 Supabase 공개용 anon 키를 설정하세요."
        )
    return {"url": url, "anon": anon}


def _headers(config, access=None):
    h = {"apikey": config["anon"], "Content-Type": "application/json",
         "Accept-Profile": "public"}
    if access:
        h["Authorization"] = "Bearer " + access
    return h


def _request(method, config, path, *, access=None, payload=None, params=None,
             http=None):
    session = http or requests
    try:
        response = session.request(
            method, config["url"] + path, headers=_headers(config, access),
            json=payload, params=params, timeout=(4, 12),
        )
    except requests.RequestException as exc:
        raise AccessError("인증 서버 연결 오류: " + type(exc).__name__) from None
    return response


def _object(response):
    try:
        obj = response.json()
    except (ValueError, TypeError):
        raise AccessError("인증 서버 응답 형식이 올바르지 않습니다.") from None
    if not isinstance(obj, dict):
        raise AccessError("인증 서버 응답 구조가 올바르지 않습니다.")
    return obj


def _email_ok(email):
    return isinstance(email, str) and len(email) <= 254 and bool(EMAIL_RE.fullmatch(email))


def send_code(config, email, *, http=None):
    """Only an existing Supabase Auth user can receive a sign-in OTP.

    create_user:false prevents arbitrary public signup through this gateway.
    Unknown/not-invited addresses will not be registered by this request.
    """
    email = normalized_email(email)
    if not _email_ok(email):
        raise AccessError("유효한 이메일 주소를 입력하세요.")
    response = _request(
        "POST", config, "/auth/v1/otp",
        payload={"email": email, "create_user": False},
        http=http,
    )
    if response.status_code == 429:
        raise AccessError("인증 요청이 제한됐습니다. 잠시 후 다시 시도하세요.")
    if not response.ok:
        raise AccessError(
            "인증번호 발송 요청이 처리되지 않았습니다. "
            f"(HTTP {response.status_code}) 인증 사용자/메일 발송 설정을 확인하세요."
        )
    return True


def auth_identity(config, access, *, http=None):
    """Return user identity verified directly by Supabase Auth, never by UI."""
    if not isinstance(access, str) or not access:
        return None
    response = _request("GET", config, "/auth/v1/user", access=access, http=http)
    if response.status_code in (401, 403, 404):
        return None
    if not response.ok:
        raise AccessError(f"인증 서버 사용자 확인 실패 (HTTP {response.status_code})")
    obj = _object(response)
    email = normalized_email(obj.get("email"))
    if (not _email_ok(email) or not isinstance(obj.get("id"), str)
            or not obj.get("id") or not obj.get("email_confirmed_at")):
        return None
    return {"email": email, "id": obj["id"]}


def _refresh(config, token, *, http=None):
    if not isinstance(token, str) or not token:
        return None
    response = _request(
        "POST", config, "/auth/v1/token?grant_type=refresh_token",
        payload={"refresh_token": token}, http=http,
    )
    if not response.ok:
        return None
    obj = _object(response)
    if not isinstance(obj.get("access_token"), str) or not isinstance(obj.get("refresh_token"), str):
        return None
    return {"access_token": obj["access_token"],
            "refresh_token": obj["refresh_token"]}


def has_grant(config, access, verified_email, app_code, *, http=None):
    """RLS allows a signed-in user to SELECT *only their own* grant rows."""
    if app_code not in APPS or not _email_ok(verified_email):
        return False
    response = _request(
        "GET", config, "/rest/v1/hanav_app_access",
        access=access,
        params={"select": "app_code,enabled",
                "email": "eq." + verified_email,
                "app_code": "in.(all," + app_code + ")"},
        http=http,
    )
    if not response.ok:
        raise AccessError(
            f"승인 목록 확인 실패 (HTTP {response.status_code}). "
            "공통 SQL 테이블·RLS 정책·Supabase 프로젝트 설정을 확인하세요."
        )
    try:
        rows = response.json()
    except (ValueError, TypeError):
        raise AccessError("승인 목록 응답이 올바르지 않습니다.") from None
    if not isinstance(rows, list):
        raise AccessError("승인 목록의 자료 형식이 올바르지 않습니다.")
    # A specific deny overrides an 'all' grant. A shared 'all' grant is
    # otherwise inherited by any app without its own per-app record.
    per_app = [row for row in rows if isinstance(row, dict)
               and row.get("app_code") == app_code]
    if per_app:
        return any(row.get("enabled") is True for row in per_app)
    return any(isinstance(row, dict)
               and row.get("app_code") == "all"
               and row.get("enabled") is True for row in rows)


def validate_session(config, saved, app_code, *, http=None):
    """Revocation takes effect on the next page rerun (DB is never cached)."""
    if app_code not in APPS or not isinstance(saved, dict):
        return None
    access = saved.get("access_token")
    refresh = saved.get("refresh_token")
    if not isinstance(access, str) or not access:
        return None
    identity = auth_identity(config, access, http=http)
    if identity is None:
        tokens = _refresh(config, refresh, http=http)
        if not tokens:
            return None
        access = tokens["access_token"]
        refresh = tokens["refresh_token"]
        identity = auth_identity(config, access, http=http)
    if identity is None:
        return None
    if not has_grant(config, access, identity["email"], app_code, http=http):
        return None
    return {"access_token": access, "refresh_token": refresh,
            "email": identity["email"], "id": identity["id"]}


def verify_code(config, email, otp, app_code, *, http=None):
    email = normalized_email(email)
    if not _email_ok(email) or not isinstance(otp, str) or not OTP_RE.fullmatch(otp.strip()):
        raise AccessError("이메일과 숫자 인증번호를 확인하세요.")
    response = _request(
        "POST", config, "/auth/v1/verify",
        payload={"email": email, "token": otp.strip(), "type": "email"},
        http=http,
    )
    if not response.ok:
        raise AccessError("인증번호가 틀렸거나 만료됐습니다.")
    obj = _object(response)
    tokens = {"access_token": obj.get("access_token"),
              "refresh_token": obj.get("refresh_token")}
    verified = validate_session(config, tokens, app_code, http=http)
    if not verified or verified["email"] != email:
        raise AccessError("이 앱의 접근 승인이 확인되지 않았습니다.")
    return verified


def require_approved_email(st, app_code, title):
    """Call *before* the existing password gate and any private data/API calls.

    In 'legacy' mode it preserves the app's current behavior until the owner
    provisions Supabase Auth + SMTP + shared grants and flips the secret.
    In 'enforced' mode all missing/outage/unauthorized cases fail closed.
    """
    if app_code not in APPS:
        st.error("앱 인증 범위 오류: 등록되지 않은 앱입니다.")
        st.stop()
        return
    try:
        mode = email_mode(st.secrets)
        if mode == "legacy":
            return
        config = auth_config(st.secrets)
    except AccessError as exc:
        st.error(str(exc))
        st.stop()
        return

    saved = st.session_state.get(SESSION_KEY)
    if saved:
        try:
            verified = validate_session(config, saved, app_code)
        except AccessError as exc:
            st.error(str(exc))
            st.stop()
            return
        if verified:
            st.session_state[SESSION_KEY] = verified
            with st.sidebar:
                st.caption("🔐 이메일 승인: " + verified["email"])
                if st.button("이메일 로그아웃", key="hanav_email_sign_out"):
                    st.session_state.clear()
                    st.rerun()
            return
        # Revoked, expired or forged session must never reach other app gates.
        st.session_state.clear()

    st.markdown(
        "<div style='text-align:center;max-width:490px;margin:5vh auto 24px'>"
        "<h2 style='color:#008878;margin-bottom:8px'>" + title + "</h2>"
        "<p>승인된 이메일로 본인 인증 후 이용할 수 있습니다.</p>"
        "</div>",
        unsafe_allow_html=True,
    )
    left, center, right = st.columns([1, 1.3, 1])
    with center:
        pending = st.session_state.get(PENDING_KEY)
        if not pending:
            with st.form("hanav_approved_email_request", clear_on_submit=False):
                email = st.text_input(
                    "승인 이메일", placeholder="name@example.com",
                    max_chars=254, autocomplete="email")
                submitted = st.form_submit_button("인증번호 요청", use_container_width=True)
            if submitted:
                e = normalized_email(email)
                if not _email_ok(e):
                    st.error("올바른 이메일 주소를 입력하세요.")
                else:
                    now = time.monotonic()
                    last = st.session_state.get(SEND_KEY, {})
                    if (isinstance(last, dict)
                            and last.get("email") == e
                            and now - last.get("time", 0) < OTP_DELAY_SECONDS):
                        st.warning("인증번호는 60초 간격으로 요청할 수 있습니다.")
                    else:
                        try:
                            send_code(config, e)
                            st.session_state[SEND_KEY] = {"email": e, "time": now}
                            st.session_state[PENDING_KEY] = e
                            st.rerun()
                        except AccessError as exc:
                            st.error(str(exc))
        else:
            st.caption("인증번호 요청: " + pending)
            with st.form("hanav_approved_email_verify", clear_on_submit=True):
                code = st.text_input(
                    "이메일 인증번호", type="password", max_chars=10,
                    autocomplete="one-time-code")
                submitted = st.form_submit_button("승인 이메일로 로그인", use_container_width=True)
            if submitted:
                try:
                    verified = verify_code(config, pending, code, app_code)
                    st.session_state[SESSION_KEY] = verified
                    st.session_state.pop(PENDING_KEY, None)
                    st.rerun()
                except AccessError as exc:
                    st.error(str(exc))
            if st.button("다른 이메일로 시도", key="hanav_approved_email_change"):
                st.session_state.pop(PENDING_KEY, None)
                st.rerun()
        st.caption(
            "승인된 계정만 이용 가능합니다. 메일이 도착하지 않으면 "
            "스팸함이나 관리자에게 인증 메일 발송 설정을 확인하세요."
        )
    st.stop()
