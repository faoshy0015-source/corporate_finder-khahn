"""Offline tests for the shared per-app email allowlist and OTP gate."""
from contextlib import nullcontext
from unittest.mock import Mock

import pytest

import email_access as auth


SECRETS = {
    "EMAIL_ACCESS_ENABLED": True,
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_ANON_KEY": "public-test-key",
    "APP_ALLOWED_EMAILS": ["owner@example.com", "approved@naver.com"],
}


def config():
    return auth.get_email_auth_config(SECRETS)


def test_staged_switch_off_does_not_break_existing_password_login():
    assert not auth.email_access_enabled({})
    assert not auth.email_access_enabled({"EMAIL_ACCESS_ENABLED": False})
    assert auth.email_access_enabled(SECRETS)
    assert auth.email_access_enabled({"EMAIL_ACCESS_ENABLED": "true"})


def test_exact_email_allowlist_no_wildcards_domains():
    assert auth.is_approved(" OWNER@EXAMPLE.COM ", config())
    assert not auth.is_approved("wrong@example.com", config())
    assert not auth.is_approved("owner@example.com.evil.org", config())
    for candidate in ["*@example.com", "example.com", "owner@", "*"]:
        with pytest.raises(auth.EmailAuthError):
            auth.parse_approved_emails(candidate)


def test_enabled_missing_config_blocks_all_access():
    for broken in (
        {**SECRETS, "APP_ALLOWED_EMAILS": []},
        {**SECRETS, "SUPABASE_ANON_KEY": ""},
        {**SECRETS, "SUPABASE_URL": "http://not-https.example.com"},
    ):
        with pytest.raises(auth.EmailAuthError):
            auth.get_email_auth_config(broken)


def test_unapproved_email_does_not_trigger_otp_delivery():
    transport = Mock()
    with pytest.raises(auth.EmailAuthError):
        auth.request_email_code(config(), "unapproved@example.com", session=transport)
    transport.post.assert_not_called()


def test_allowed_email_only_send_one_time_code_and_do_not_use_admin_key():
    transport = Mock()
    transport.post.return_value = Mock(ok=True, status_code=200)
    assert auth.request_email_code(config(), "owner@example.com", session=transport)
    call = transport.post.call_args
    assert call.kwargs["json"]["email"] == "owner@example.com"
    assert call.kwargs["headers"]["apikey"] == SECRETS["SUPABASE_ANON_KEY"]
    assert "service_role" not in str(call)


def test_supabase_verifies_email_not_client_provided_identity():
    transport = Mock()
    transport.post.return_value = Mock(
        ok=True, status_code=200,
        json=lambda: {"access_token": "ACCESS", "refresh_token": "REFRESH",
                      "user": {"email": "fake@example.com"}},
    )
    transport.get.return_value = Mock(
        ok=True, status_code=200,
        json=lambda: {"id": "verified-id", "email": "owner@example.com"},
    )
    actual = auth.verify_email_code(config(), "owner@example.com", "123456", session=transport)
    assert actual["email"] == "owner@example.com"
    assert actual["user_id"] == "verified-id"
    assert transport.get.call_args.kwargs["headers"]["Authorization"] == "Bearer ACCESS"


def test_revoking_email_invalidates_current_token_on_next_rerun():
    transport = Mock()
    transport.get.return_value = Mock(
        ok=True, status_code=200,
        json=lambda: {"id": "verified-id", "email": "owner@example.com"},
    )
    stored = {"access_token": "ACCESS", "refresh_token": "REFRESH"}
    assert auth.validate_session(config(), stored, session=transport)
    revoked = auth.get_email_auth_config({
        **SECRETS, "APP_ALLOWED_EMAILS": ["approved@naver.com"],
    })
    assert auth.validate_session(revoked, stored, session=transport) is None


def test_wrong_or_expired_token_never_trusts_session_email():
    transport = Mock()
    transport.get.return_value = Mock(ok=False, status_code=401)
    transport.post.return_value = Mock(ok=False, status_code=401)
    assert auth.validate_session(
        config(), {"email": "owner@example.com",
                   "access_token": "FORGED", "refresh_token": "FORGED"},
        session=transport) is None


def test_auth_failure_never_leaks_response_secrets():
    transport = Mock()
    transport.post.return_value = Mock(
        ok=False, status_code=403, text="sensitive private payload",
    )
    with pytest.raises(auth.EmailAuthError) as error:
        auth.request_email_code(config(), "owner@example.com", session=transport)
    assert "sensitive private payload" not in str(error.value)


class Stopped(Exception):
    pass


class FakeSt:
    def __init__(self, secrets):
        self.secrets = secrets
        self.session_state = {}
        self.errors = []
        self.sidebar = nullcontext()
    def error(self, message): self.errors.append(message)
    def caption(self, message): pass
    def stop(self): raise Stopped()


def test_enabled_gate_fails_closed_before_app_password_or_data():
    st = FakeSt({"EMAIL_ACCESS_ENABLED": True})
    with pytest.raises(Stopped):
        auth.require_approved_email(st)
    assert st.errors


def test_inactive_gate_does_not_change_old_login_flow():
    st = FakeSt({})
    auth.require_approved_email(st)
    assert not st.errors
