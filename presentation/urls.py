"""route URL 어댑터 — presentation이 Flask route 구조/url_for에 직접 의존하지 않게(GPT).

각 앱이 프리픽스 부착 함수 `u`(예: web/api.py의 _u=script_root 인식)를 주입한다. 전환기에는
/studio(/ui·/api·/img) 경로를 반환하고, 7B에서 대시보드 canonical route(/scripts/...)로
교체하려면 이 어댑터의 구현만 바꾸면 된다(presentation·template은 불변)."""


class StudioUrls:
    """현행 /studio 경로를 만드는 어댑터. u: 프리픽스 부착 콜러블, slug: 병원 slug."""

    def __init__(self, u, slug):
        self._u = u
        self.slug = slug

    def version(self, version_id):
        return self._u(f"/ui/h/{self.slug}/versions/{version_id}")

    def edit(self, script_id):
        return self._u(f"/ui/h/{self.slug}/scripts/{script_id}/edit")

    def approve(self, version_id):
        return self.version(version_id) + "/approve"

    def reject(self, version_id):
        return self.version(version_id) + "/reject"

    def revoke(self, version_id):
        return self.version(version_id) + "/revoke"

    def self_approve(self, version_id):
        return self.version(version_id) + "/self-approve"

    def export(self, script_id, version_id):
        return self._u(f"/api/h/{self.slug}/scripts/{script_id}/versions/{version_id}/export")

    def diff(self, version_id, from_version_id):
        return self._u(f"/api/h/{self.slug}/versions/{version_id}/diff") + f"?from={from_version_id}"

    def review(self, claim_id):
        return self._u(f"/ui/h/{self.slug}/claims/{claim_id}/review")

    def img(self, block_key):
        return self._u(f"/img/h/{self.slug}/{block_key}")

    def regen(self, version_id, block_key):
        return self._u(f"/ui/h/{self.slug}/versions/{version_id}/blocks/{block_key}/regen-image")

    def revert(self, version_id, block_key):
        return self._u(f"/ui/h/{self.slug}/versions/{version_id}/blocks/{block_key}/revert-image")

    def logout(self):
        return self._u("/logout")

    def dashboard(self):
        return "/"


class DashboardUrls:
    """대시보드 canonical route에서 공유 버전페이지를 렌더할 때 쓰는 어댑터(전환기 하이브리드, 7B).

    읽기·nav는 대시보드 경로(/scripts/<slug>/<version_id>)로, 쓰기·자산은 아직 /studio compat
    엔드포인트로 보낸다(두 앱 세션·CSRF 공유). Step 9에서 쓰기까지 대시보드로 옮기면
    _studio 위임을 대시보드 자체 route로 교체한다(presentation·template은 불변)."""

    def __init__(self, slug):
        self.slug = slug
        self._studio = StudioUrls(lambda p: "/studio" + p, slug)   # diff(디버그 JSON)만 잔여 compat

    # 읽기·nav — 대시보드 canonical
    def version(self, version_id):
        return f"/scripts/{self.slug}/{version_id}"

    def dashboard(self):
        return "/"

    def logout(self):
        return "/logout"

    # 쓰기·자산 — 대시보드가 직접 소유(studio 미경유, Step 9 선반영)
    def edit(self, script_id):
        return f"/scripts/{self.slug}/{script_id}/edit"

    def approve(self, version_id):
        return f"/scripts/{self.slug}/versions/{version_id}/approve"

    def reject(self, version_id):
        return f"/scripts/{self.slug}/versions/{version_id}/reject"

    def revoke(self, version_id):
        return f"/scripts/{self.slug}/versions/{version_id}/revoke"

    def self_approve(self, version_id):
        return f"/scripts/{self.slug}/versions/{version_id}/self-approve"

    def export(self, script_id, version_id):
        return f"/scripts/{self.slug}/{script_id}/versions/{version_id}/export"

    def review(self, claim_id):
        return f"/scripts/{self.slug}/claims/{claim_id}/review"

    def img(self, block_key):
        return f"/scripts/{self.slug}/img/{block_key}"

    def regen(self, version_id, block_key):
        return f"/scripts/{self.slug}/versions/{version_id}/blocks/{block_key}/regen-image"

    def revert(self, version_id, block_key):
        return f"/scripts/{self.slug}/versions/{version_id}/blocks/{block_key}/revert-image"

    def diff(self, version_id, from_version_id):
        return self._studio.diff(version_id, from_version_id)   # 디버그 JSON — 잔여 compat
