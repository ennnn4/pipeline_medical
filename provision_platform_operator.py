"""platform operator 계정 프로비저닝(대행사 전 병원 접근). owner 권한으로 실행.

  OWNER_URL=postgresql://<owner>:<pw>@host/db \
  PLATFORM_EMAIL=admin@ourmarketing.com PLATFORM_PW=<비번> \
  python provision_platform_operator.py

PLATFORM_PW 미지정 시 랜덤 생성 후 1회 출력. 이메일 기반 PG 로그인으로 대시보드+스튜디오 단일 로그인.
reseed(--reset) 후에는 데이터가 지워지므로 재실행 필요(또는 deploy_bootstrap의 SEED_PLATFORM_* 사용)."""
import os
import sys
import secrets
from store.db import make_engine
from store.platform_ops import ensure_platform_admin_user


def main():
    url = os.environ.get("OWNER_URL") or os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("환경변수 OWNER_URL(권장) 또는 DATABASE_URL 필요")
    email = os.environ.get("PLATFORM_EMAIL", "admin@ourmarketing.com")
    pw = os.environ.get("PLATFORM_PW") or secrets.token_urlsafe(12)
    uid = ensure_platform_admin_user(make_engine(url), email, pw, name=os.environ.get("PLATFORM_NAME"))
    print(f"[platform operator] {email} 생성/갱신 완료 (user_id={uid}) — 전 병원 접근 grant active.")
    if not os.environ.get("PLATFORM_PW"):
        print(f"[임시 비밀번호] {pw}\n(대시보드 로그인 칸에 이메일+이 비번. 첫 로그인 후 변경 권장.)")


if __name__ == "__main__":
    main()
