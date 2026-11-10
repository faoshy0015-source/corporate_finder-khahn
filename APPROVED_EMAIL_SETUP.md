# Corporate Finder · 승인 이메일 접속 설정

이 앱에는 다섯 HanaV 금융 웹앱에 공통으로 쓰는 서버측 이메일 인증 모듈 `approved_email_gate.py`가 연결되어 있습니다.

- 중앙 승인 목록: **HanaV-Finder Supabase** 프로젝트의 `public.hanav_app_access` 테이블
- 앱 식별 코드: `corporate-finder`
- **현재 기본값**: `EMAIL_ACCESS_MODE = "legacy"` — 기존 앱 비밀번호 로그인만 유지. 관리자를 잠그지 않기 위한 단계적 도입.
- 이메일 인증 강제: SQL 테이블, Supabase Auth 사용자, 외부 SMTP와 OTP 템플릿, Streamlit Secrets 설정까지 완료한 후 `EMAIL_ACCESS_MODE = "enforced"` 로 변경.
- 인증이 활성화되면 **승인된 이메일 OTP + 기존 앱 비밀번호** 둘 다 확인됩니다. 기존 관리자 비밀번호는 변경하지 않습니다.
- 등록된 이메일이라도 중앙 테이블에서 이 앱 승인이 없거나 철회되면 접근할 수 없습니다.
- 공개 URL은 로그인 화면까지 접근할 수 있으므로 플랫폼 Private 앱 설정과는 다릅니다.

전체 설정 가이드: [Stock Analyzer 공통 승인 이메일 설정 문서](https://github.com/faoshy0015-source/stock-analyzer/blob/main/docs/APPROVED_EMAIL_SETUP.md)

중요: 인증 이메일 전송을 위해 Supabase 프로젝트에 사용자 관리자가 설정한 **custom SMTP**가 필요합니다. GitHub 저장소에 Supabase 인증키나 이메일을 입력하지 마세요.
