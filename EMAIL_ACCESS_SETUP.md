# 승인 이메일 접근 통제 설정 (Public Streamlit 앱)

이 앱은 기존 앱 비밀번호(`APP_PASSWORD`)를 유지하면서, 선택적으로
**Supabase Auth의 이메일 1회용 인증번호(OTP)와 정확한 승인 주소 목록**을
추가합니다. `EMAIL_ACCESS_ENABLED = true`를 설정해야 이메일 제한이
실제로 적용됩니다.

## 1. 사전 준비

1. Supabase 프로젝트에서 **Authentication → Providers → Email**을
   활성화합니다. 앱마다 별도 Supabase 프로젝트를 만들 필요는 없지만
   같은 프로젝트를 공유하면 사용자 계정·메일 발송 한도도 공유합니다.
2. **Authentication → Email Templates**의 Magic Link/OTP 템플릿 본문에
   `{{ .Token }}`을 넣어 6자리 인증번호가 발송되게 설정합니다.
   신규 사용자는 **Confirm signup** 템플릿도 확인하세요.
3. **Authentication → SMTP Settings**에서 본인이 관리하는 SMTP
   발송 서비스를 설정합니다. Supabase 기본 SMTP는 프로젝트 조직의
   승인된 이메일만 발송할 수 있으며, 외부 직원 이메일에는 사용할 수 없습니다.
   SMTP 자격증명은 GitHub에 커밋하지 마세요.
4. 회사 업무용 계정 인증 시 회사 전산·보안 규정 및 필요한 승인 여부를
   확인하세요. 메일 인증은 직원의 신원·재직 상태를 자동 확인하지 않습니다.

## 2. Streamlit Cloud → 앱 → Settings → Secrets

다음 내용을 **기존 APP_PASSWORD 및 시장데이터 비밀 설정을 지우지 않고**
추가하세요. 예시 이메일/URL/키를 그대로 사용하지 마세요.

```toml
SUPABASE_URL = "https://YOUR-PROJECT.supabase.co"
SUPABASE_ANON_KEY = "YOUR_SUPABASE_PUBLISHABLE_ANON_KEY"
APP_ALLOWED_EMAILS = ["your-email@example.com", "approved-employee@example.com"]

# 테스트 및 인증 메일 수신 확인 후 마지막으로 활성화
EMAIL_ACCESS_ENABLED = true
```

기존 `SUPABASE_SERVICE_ROLE_KEY`가 있는 앱에서도 인증에는 별도의
`SUPABASE_ANON_KEY`를 사용합니다. **service_role 키는 브라우저,
메일, GitHub 파일에 넣지 마세요.**

### 안전한 배포 순서

- **1단계:** 신규 인증 코드만 배포. `EMAIL_ACCESS_ENABLED`가
  없거나 `false`이면 기존 앱 비밀번호 로그인이 그대로 유지됩니다.
  이 단계에서는 이메일별 접근 제한이 **아직 적용되지 않습니다**.
- **2단계:** 앱에 사용 중인 Supabase 프로젝트 URL, anon 키,
  실제 승인 이메일 주소를 등록하고 SMTP/OTP 템플릿을 점검합니다.
  초기에 관리자 본인 이메일을 반드시 포함하세요.
- **3단계:** `EMAIL_ACCESS_ENABLED = true`로 저장한 뒤
  **허가된 주소·허가되지 않은 주소 각각으로 실제 테스트**합니다.
  인증 서버나 설정이 잘못되면 기존 비밀번호만으로 우회되지 않고
  앱이 접근을 차단합니다.
- **4단계:** 승인 목록을 변경하려면 Streamlit Secrets의
  `APP_ALLOWED_EMAILS` 주소를 추가/삭제합니다.
  이미 접속한 사용자는 다음 화면 실행 시 해당 목록을 재확인합니다.

## 로그인 및 기능 범위

인증이 활성화되면 모든 방문자는 먼저 승인된 이메일로
인증번호를 받은 뒤, **기존 앱 비밀번호도 입력**해야 분석 화면을 열 수
있습니다. 인증되지 않은 주소로는 인증 메일도 요청하지 않습니다.
로그인 성공 후에도 각 화면 재실행 시 Supabase Auth에서 세션과
승인 목록을 확인합니다. **이메일 로그아웃**은 세션/분석 상태를 함께
초기화합니다.

각 앱의 승인 목록은 별도 Streamlit Secrets에 있으므로 **목록을
앱 사이에 자동 복제하거나 하나의 앱에서 통합 관리하지 않습니다**.
동일한 인증 방식이라도 각 앱마다 처음 사용할 때 인증이 필요합니다.

Streamlit Community Cloud에서 이 앱의 Sharing이 **Public**이라면
앱 주소와 로그인 화면은 누구나 열 수 있습니다. 다만 서버측
이메일 OTP 및 기존 비밀번호를 통과하지 못하면 앱 데이터에 접근할
수 없도록 보호합니다. 이는 플랫폼의 Private 앱과 다릅니다.

공식 문서:
- https://supabase.com/docs/guides/auth/auth-email-passwordless
- https://supabase.com/docs/guides/auth/auth-smtp
- https://docs.streamlit.io/deploy/streamlit-community-cloud/share-your-app
