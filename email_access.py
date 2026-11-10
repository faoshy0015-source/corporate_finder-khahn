"""Server-side email allowlist and Supabase Auth one-time-code verification.

Email authentication precedes and complements the existing APP_PASSWORD gate. User-supplied emails and existing Streamlit
session flags are never treated as authentication. Every rerun validates the
Supabase-issued access token against the Auth server and rechecks the allowlist.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import time
from urllib.parse import urlparse

import requests

_EMAIL = re.compile(r"^[A-Za-z0-9.!#$%&'*+\-/=?^_`{|}~]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_CODE = re.compile(r"^[0-9]{6,10}$")
SESSION_KEY = "_approved_email_session"
PENDING_KEY = "_approved_email_pending"
LAST_SENT_KEY = "_approved_email_last_sent"
# Supabase email rate limiting also applies. This reduces accidental repeated
# sends within a single browser session; it is NOT a substitute for server limits.
RESEND_WAIT_SECONDS = 60


class EmailAuthError(RuntimeError):
    """A user-displayable error that never includes tokens or response bodies."""


@dataclass(frozen=True)
class EmailAuthConfig:
    url: str
    anon_key: str
    allowlist: frozenset[str]


def normal_email(value):
    if not isinstance(value, str):
        return ""
    return value.strip().casefold()


def parse_approved_emails(value):
    """Only exact addresses are allowed; no domains, glob patterns or wildcards."""
    if isinstance(value, (list, tuple, set)):
        candidates = list(value)
    elif isinstance(value, str):
        candidates = re.split(r"[,;\n]", value)
    else:
        candidates = []
    result = set()
    for candidate in candidates:
        address = normal_email(candidate)
        if not address:
            continue
        if len(address) > 254 or "*" in address or not _EMAIL.fullmatch(address):
            raise EmailAuthError(
                "APP_ALLOWED_EMAILS에는 구체적인 이메일 주소만 등록하세요. "
                "와일드카드·도메인 전체 허용은 지원하지 않습니다."
            )
        result.add(address)
    return frozenset(result)


def _secret(secrets, key):
    try:
        value = secrets.get(key, "")
    except Exception:
        return ""
    return value


def get_email_auth_config(secrets):
    url = str(_secret(secrets, "SUPABASE_URL") or "").strip().rstrip("/")
    key = str(_secret(secrets, "SUPABASE_ANON_KEY") or "").strip()
    allowed = parse_approved_emails(_secret(secrets, "APP_ALLOWED_EMAILS"))
    if not allowed:
        raise EmailAuthError(
            "승인 이메일 목록 미설정: Streamlit Secrets의 APP_ALLOWED_EMAILS에 "
            "본인 이메일을 포함해 승인할 주소를 등록해야 합니다."
        )
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.path not in ("", "/"):
        raise EmailAuthError("SUPABASE_URL에는 https://...supabase.co 프로젝트 주소가 필요합니다.")
    if not key:
        raise EmailAuthError(
            "SUPABASE_ANON_KEY 미설정: Supabase 프로젝트의 공개용 anon 키를 "
            "Streamlit Secrets에 등록해야 합니다. service_role 키는 사용하지 않습니다."
        )
    return EmailAuthConfig(url, key, allowed)


def is_approved(email, config):
    return normal_email(email) in config.allowlist


def _headers(config, access_token=None):
    headers = {"apikey": config.anon_key, "Content-Type": "application/json"}
    if access_token:
        headers["Authorization"] = "Bearer " + access_token
    return headers


def _post_json(config, endpoint, data, *, session=None, timeout=12):
    post = session.post if session is not None else requests.post
    try:
        response = post(
            config.url + endpoint,
            json=data,
            headers=_headers(config),
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise EmailAuthError("인증 서버 접속 실패: " + type(exc).__name__) from None
    return response


def request_email_code(config, email, *, session=None):
    """Send OTP only to an exact admin-approved email; never create other users."""
    email = normal_email(email)
    if not is_approved(email, config) or not _EMAIL.fullmatch(email):
        # Avoid returning the email or disclosing account existence.
        raise EmailAuthError("승인되지 않은 이메일입니다. 관리자에게 접근 권한을 요청하세요.")
    response = _post_json(
        config, "/auth/v1/otp",
        {"email": email, "create_user": True},
        session=session,
    )
    if response.status_code == 429:
        raise EmailAuthError("인증 요청 횟수가 초과됐습니다. 잠시 후 다시 시도하세요.")
    if not response.ok:
        raise EmailAuthError(
            f"이메일 인증번호 발송 실패 (HTTP {response.status_code}). "
            "Supabase Auth 이메일 제공자·발송 제한·인증 설정을 확인하세요."
        )
    return True


def _json_response(response, stage):
    try:
        obj = response.json()
    except (ValueError, TypeError):
        raise EmailAuthError(f"{stage}: 인증 서버가 유효한 JSON을 반환하지 않았습니다.") from None
    if not isinstance(obj, dict):
        raise EmailAuthError(f"{stage}: 인증 서버 응답 형식이 올바르지 않습니다.")
    return obj


def verify_email_code(config, email, code, *, session=None):
    """Verify the email OTP, then obtain authoritative identity via /user."""
    email = normal_email(email)
    if not is_approved(email, config):
        raise EmailAuthError("승인 이메일 목록에 없는 계정입니다.")
    if not isinstance(code, str) or not _CODE.fullmatch(code.strip()):
        raise EmailAuthError("이메일에 전송된 숫자 인증번호를 입력하세요.")
    response = _post_json(
        config, "/auth/v1/verify",
        {"email": email, "token": code.strip(), "type": "email"},
        session=session,
    )
    if not response.ok:
        raise EmailAuthError(
            "인증번호가 유효하지 않거나 만료됐습니다. 메일의 인증번호를 다시 확인하세요."
        )
    payload = _json_response(response, "인증번호 검증")
    token = payload.get("access_token")
    refresh = payload.get("refresh_token")
    if not isinstance(token, str) or not token or not isinstance(refresh, str) or not refresh:
        raise EmailAuthError("인증 세션이 발급되지 않았습니다.")
    session_data = {"access_token": token, "refresh_token": refresh}
    verified = validate_session(config, session_data, session=session)
    if verified is None or verified["email"] != email:
        raise EmailAuthError("서버에서 확인한 이메일 주소가 요청한 계정과 다릅니다.")
    return verified


def get_auth_user(config, access_token, *, session=None):
    """Never trust a decoded JWT or client-provided email without server validation."""
    get = session.get if session is not None else requests.get
    try:
        response = get(
            config.url + "/auth/v1/user",
            headers=_headers(config, access_token),
            timeout=10,
        )
    except requests.RequestException as exc:
        raise EmailAuthError("사용자 인증 확인 실패: " + type(exc).__name__) from None
    if response.status_code in (401, 403):
        return None
    if not response.ok:
        raise EmailAuthError(f"사용자 인증 서버 오류 (HTTP {response.status_code})")
    user = _json_response(response, "사용자 확인")
    email = normal_email(user.get("email"))
    if not email or not isinstance(user.get("id"), str):
        raise EmailAuthError("인증 서버가 사용자 정보를 반환하지 않았습니다.")
    # Return the verified identity even if no longer approved so that
    # validate_session can revoke immediately, without attempting refresh.
    return {"id": user["id"], "email": email}


def _refresh_session(config, refresh, *, session=None):
    response = _post_json(
        config, "/auth/v1/token?grant_type=refresh_token",
        {"refresh_token": refresh},
        session=session,
    )
    if not response.ok:
        return None
    payload = _json_response(response, "인증 세션 갱신")
    access = payload.get("access_token")
    new_refresh = payload.get("refresh_token")
    if not isinstance(access, str) or not access or not isinstance(new_refresh, str) or not new_refresh:
        return None
    return {"access_token": access, "refresh_token": new_refresh}


def validate_session(config, stored, *, session=None):
    """Call Supabase Auth on every view; refresh once if its token expired.

    A stale email in Streamlit session_state is never enough. If the owner
    revokes an address, the very next page rerun denies access immediately.
    """
    if not isinstance(stored, dict):
        return None
    access = stored.get("access_token")
    refresh = stored.get("refresh_token")
    if not isinstance(access, str) or not access:
        return None
    identity = get_auth_user(config, access, session=session)
    if identity is None and isinstance(refresh, str) and refresh:
        renewed = _refresh_session(config, refresh, session=session)
        if renewed:
            access = renewed["access_token"]
            refresh = renewed["refresh_token"]
            identity = get_auth_user(config, access, session=session)
    if identity is None or not is_approved(identity["email"], config):
        return None
    return {
        "access_token": access,
        "refresh_token": refresh,
        "email": identity["email"],
        "user_id": identity["id"],
    }


def clear_auth_state(st):
    """Clear *all* cached page session data before a new user can sign in."""
    st.session_state.clear()


def otp_resend_permitted(session_state, email, now=None):
    now = time.time() if now is None else now
    record = session_state.get(LAST_SENT_KEY, {})
    return not (
        isinstance(record, dict)
        and record.get("email") == normal_email(email)
        and now - float(record.get("time", 0)) < RESEND_WAIT_SECONDS
    )


def email_access_enabled(secrets):
    """Explicit staged rollout; never infer enabled from partial credentials."""
    value = _secret(secrets, "EMAIL_ACCESS_ENABLED")
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def require_approved_email(st):
    """Verify exact allowlisted identity before any of the original app UI.

    Owners first provision Secrets and SMTP, then set EMAIL_ACCESS_ENABLED=true.
    Disabled means the original APP_PASSWORD gate remains unchanged; it is NOT
    an allowlisted setup. Once enabled, missing auth configuration fails closed.
    """
    if not email_access_enabled(st.secrets):
        return

    try:
        config = get_email_auth_config(st.secrets)
    except EmailAuthError as exc:
        st.error("이메일 접근 통제 설정을 완료해야 앱을 열 수 있습니다.")
        st.caption(str(exc))
        st.stop()
        return

    current = st.session_state.get(SESSION_KEY)
    if current:
        try:
            verified = validate_session(config, current)
        except EmailAuthError:
            st.error("인증 서버 확인에 실패했습니다. 데이터를 열지 않고 접근을 차단합니다.")
            st.stop()
            return
        if verified is not None:
            st.session_state[SESSION_KEY] = verified
            with st.sidebar:
                st.caption("✉️ 승인 이메일: " + verified["email"])
                if st.button("이메일 로그아웃", key="_approved_email_logout"):
                    st.session_state.clear()
                    st.rerun()
            return
        st.session_state.pop(SESSION_KEY, None)
        # Do not allow pre-existing password-only sessions from before the
        # activation to bypass the now-required email identity.
        st.session_state.clear()

    st.markdown(
        """
        <style>
        .approved-email-signin {
          max-width:480px;margin:3rem auto 1rem;text-align:center;
          padding:1.5rem;border:1px solid #cbded5;border-radius:14px;
          background:#f8fcfa;color:#203d35;
        }
        .approved-email-signin h2 {color:#008878;font-weight:800}
        </style>
        <div class="approved-email-signin">
          <h2>승인 이메일 인증</h2>
          <p>접근이 허용된 이메일 주소로 발송된 일회용 인증번호를 입력하세요.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    pending = st.session_state.get(PENDING_KEY)
    if pending and not is_approved(pending, config):
        st.session_state.pop(PENDING_KEY, None)
        pending = None
    if not pending:
        with st.form("_approved_email_request_form"):
            email = st.text_input(
                "승인 이메일", placeholder="name@example.com",
                autocomplete="email", max_chars=254,
            )
            submit = st.form_submit_button(
                "인증번호 받기", use_container_width=True
            )
        if submit:
            normalized = normal_email(email)
            if not is_approved(normalized, config):
                st.error("승인되지 않은 이메일입니다.")
            elif not otp_resend_permitted(st.session_state, normalized):
                st.warning("인증번호는 60초 후 다시 요청할 수 있습니다.")
            else:
                try:
                    request_email_code(config, normalized)
                    st.session_state[PENDING_KEY] = normalized
                    st.session_state[LAST_SENT_KEY] = {
                        "email": normalized, "time": time.time()
                    }
                    st.rerun()
                except EmailAuthError as exc:
                    st.error(str(exc))
    else:
        st.caption("인증번호를 보낸 이메일: " + pending)
        with st.form("_approved_email_verify_form", clear_on_submit=True):
            code = st.text_input(
                "이메일 인증번호", type="password",
                placeholder="6자리 인증번호", max_chars=10,
                autocomplete="one-time-code",
            )
            submitted = st.form_submit_button(
                "이메일 확인 후 계속", use_container_width=True
            )
        if submitted:
            try:
                verified = verify_email_code(config, pending, code)
                st.session_state[SESSION_KEY] = verified
                st.session_state.pop(PENDING_KEY, None)
                # Prevent a shared password session created before email
                # activation from being carried across an email identity.
                for old_key in (
                    "_hanav_stock_authenticated",
                    "_corp_authenticated",
                    "_hanav_authenticated",
                    "auth",
                    "_hv_auth_fingerprint",
                ):
                    st.session_state.pop(old_key, None)
                st.rerun()
            except EmailAuthError as exc:
                st.error(str(exc))
        if st.button("다른 이메일 사용", key="_approved_email_change"):
            st.session_state.pop(PENDING_KEY, None)
            st.rerun()
        st.caption("이메일 인증번호가 오지 않으면 스팸함과 메일 발송 설정을 확인하세요.")
    st.stop()
