"""Unit contract for centrally approved HanaV email identity across apps."""
from unittest.mock import Mock

import pytest

import approved_email_gate as gate


SECRETS = {
    "EMAIL_ACCESS_MODE": "enforced",
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_ANON_KEY": "anon-publishable-value",
}
CFG = {"url": "https://example.supabase.co", "anon": "anon-publishable-value"}


def response(code, data=None):
    return Mock(status_code=code, ok=200 <= code < 300, json=lambda: data)


def fake_http(*responses):
    http = Mock()
    http.request.side_effect = list(responses)
    return http


def verify_response(email="owner@example.com"):
    return {"access_token": "jwt-verified", "refresh_token": "new-refresh"}


def identity_response(email="owner@example.com"):
    return {
        "id": "user-uuid",
        "email": email,
        "email_confirmed_at": "2026-11-10T01:00:00+00:00",
    }


def grant_response(app="stock-analyzer", enabled=True):
    return [{"app_code": app, "enabled": enabled}]


def test_default_safe_rollout_preserves_existing_apps_until_configured():
    assert gate.email_mode({}) == "legacy"
    with pytest.raises(gate.AccessError, match="EMAIL_ACCESS_MODE"):
        gate.email_mode({"EMAIL_ACCESS_MODE": "unknown"})


def test_missing_auth_key_fails_closed_only_in_enforced_mode():
    with pytest.raises(gate.AccessError, match="SUPABASE_ANON_KEY"):
        gate.auth_config({"SUPABASE_URL": CFG["url"]})


def test_email_code_does_not_create_public_users():
    http = fake_http(response(200, {}))
    assert gate.send_code(CFG, "OWNER@EXAMPLE.COM", http=http)
    args, kwargs = http.request.call_args
    assert kwargs["json"] == {"email": "owner@example.com", "create_user": False}
    assert kwargs["headers"]["apikey"] == CFG["anon"]
    assert "Authorization" not in kwargs["headers"]
    assert "jwt" not in args[1]
    assert "anon-publishable-value" not in args[1]


def test_user_identity_must_be_verified_by_supabase_auth():
    http = fake_http(response(200, identity_response()))
    identity = gate.auth_identity(CFG, "jwt-verified", http=http)
    assert identity == {"email": "owner@example.com", "id": "user-uuid"}
    assert http.request.call_args.kwargs["headers"]["Authorization"] == "Bearer jwt-verified"


@pytest.mark.parametrize("detail", [
    {"id": "u", "email": "owner@example.com", "email_confirmed_at": None},
    {"id": "u", "email": None, "email_confirmed_at": "yes"},
    {"email": "owner@example.com", "email_confirmed_at": "yes"},
])
def test_unconfirmed_or_incomplete_user_identity_is_rejected(detail):
    http = fake_http(response(200, detail))
    assert gate.auth_identity(CFG, "jwt-verified", http=http) is None


def test_unapproved_email_cannot_enter_app_even_after_otp():
    http = fake_http(
        response(200, verify_response()),
        response(200, identity_response("unauthorized@example.com")),
        response(200, []),
    )
    with pytest.raises(gate.AccessError, match="승인"):
        gate.verify_code(CFG, "unauthorized@example.com", "123456",
                         "stock-analyzer", http=http)


def test_otp_email_must_match_supabase_verified_user():
    http = fake_http(
        response(200, verify_response()),
        response(200, identity_response("owner@example.com")),
        response(200, grant_response()),
    )
    with pytest.raises(gate.AccessError, match="승인"):
        gate.verify_code(CFG, "other@example.com", "123456",
                         "stock-analyzer", http=http)


def test_verified_identity_and_active_grant_allows_session():
    http = fake_http(
        response(200, verify_response()),
        response(200, identity_response()),
        response(200, grant_response()),
    )
    verified = gate.verify_code(CFG, "owner@example.com", "123456",
                                "stock-analyzer", http=http)
    assert verified["email"] == "owner@example.com"
    assert verified["access_token"] == "jwt-verified"
    assert http.request.call_args.kwargs["params"]["email"] == "eq.owner@example.com"


def test_central_grant_for_all_apps():
    http = fake_http(response(200, grant_response("all")))
    assert gate.has_grant(CFG, "jwt", "owner@example.com", "private-portfolio",
                          http=http)


def test_per_app_deny_overrides_global_grant():
    http = fake_http(response(200, [
        {"app_code": "all", "enabled": True},
        {"app_code": "private-portfolio", "enabled": False},
    ]))
    assert not gate.has_grant(
        CFG, "jwt", "owner@example.com", "private-portfolio", http=http)


def test_different_app_permission_must_not_be_reused():
    http = fake_http(response(200, [{"app_code": "corporate-finder", "enabled": True}]))
    assert not gate.has_grant(CFG, "jwt", "owner@example.com",
                              "stock-analyzer", http=http)


def test_deleted_access_is_denied_on_next_rerun():
    old = {"access_token": "jwt", "refresh_token": "refresh"}
    http1 = fake_http(response(200, identity_response()),
                      response(200, grant_response()))
    assert gate.validate_session(CFG, old, "stock-analyzer", http=http1)
    http2 = fake_http(response(200, identity_response()), response(200, []))
    assert gate.validate_session(CFG, old, "stock-analyzer", http=http2) is None


def test_expired_access_token_is_refreshed():
    http = fake_http(
        response(401, {"error": "expired"}),
        response(200, {"access_token": "new", "refresh_token": "rotated"}),
        response(200, identity_response()),
        response(200, grant_response()),
    )
    user = gate.validate_session(
        CFG, {"access_token": "expired", "refresh_token": "old"},
        "stock-analyzer", http=http)
    assert user["access_token"] == "new"
    assert user["refresh_token"] == "rotated"


def test_bad_json_and_api_errors_do_not_expose_secrets_or_auto_allow():
    http = fake_http(Mock(status_code=200, ok=True,
                          json=Mock(side_effect=ValueError("token secretly leaked"))))
    with pytest.raises(gate.AccessError) as e:
        gate.has_grant(CFG, "jwt", "owner@example.com", "stock-analyzer",
                       http=http)
    assert "token secretly leaked" not in str(e.value)
    http = fake_http(response(404, {"error": "secret"}))
    with pytest.raises(gate.AccessError, match="HTTP 404"):
        gate.has_grant(CFG, "jwt", "owner@example.com", "stock-analyzer",
                       http=http)


def test_any_forged_local_session_without_access_token_fails():
    assert gate.validate_session(CFG, {"email": "owner@example.com"},
                                 "stock-analyzer") is None


def test_every_app_code_is_supported_and_unknown_app_denied():
    assert gate.APPS == frozenset({
        "stock-analyzer", "hanav-trading", "corporate-finder",
        "pension-tax", "private-portfolio"
    })
    assert gate.has_grant(CFG, "jwt", "owner@example.com", "fake-app") is False


class Stop(Exception):
    pass


class FakeApp:
    def __init__(self, secrets, state=None):
        self.secrets = secrets
        self.session_state = {} if state is None else state
        self.errors = []
    def error(self, message):
        self.errors.append(message)
    def stop(self):
        raise Stop()


def test_enforced_mode_fails_closed_before_rendering_if_missing_config():
    app = FakeApp({"EMAIL_ACCESS_MODE": "enforced"})
    with pytest.raises(Stop):
        gate.require_approved_email(app, "stock-analyzer", "Stock Analyzer")
    assert app.errors


def test_legacy_mode_keeps_existing_password_gate_unchanged():
    app = FakeApp({})
    gate.require_approved_email(app, "stock-analyzer", "Stock Analyzer")
    assert not app.errors
