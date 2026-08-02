"""공통 애플리케이션 service 계층 — 라우트(대시보드·/studio)가 공유하는 단일 업무 규칙.

라우트는 request 파싱 + actor context 구성 + service 호출 + 응답만 담당하고,
권한·상태전이·트랜잭션 경계·승인 규칙은 여기(services)에만 둔다(GPT 아키텍처).
"""
