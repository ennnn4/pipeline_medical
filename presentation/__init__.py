"""공유 presentation 계층(물리 통합 Step 7A) — /studio와 대시보드가 함께 쓰는 렌더링.

경계(GPT): 여기서는 Flask request/session/redirect/DB/service를 절대 import하지 않는다.
화면에 필요한 값(데이터·URL 어댑터·csrf 토큰·branding)을 인자로만 받아 HTML 문자열을 반환한다.
Flask 관련 값 주입은 각 앱의 route adapter(web/api.py 등)가 담당한다."""
