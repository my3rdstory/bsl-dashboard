# BSL Dashboard

Bitcoin Social Layer(이하 BSL) 회비·활동·정산 운영 대시보드입니다.  
핵심 스택은 Django + Bulma CSS + HTMX이며, 현재 코드베이스는 PostgreSQL 기반으로 운영됩니다.

문서 목적: 로컬 설치, Render 배포, 사이드바 메뉴 사용 방법을 코드 기준으로 정리해, 인수인계가 가능한 수준으로 정리.

---

## 코드 베이스 핵심 구성

- `config/`
  - `config/settings.py`: Django 설정 전부 관리
  - `config/urls.py`: 루트 라우팅
- `core/`
  - `core/urls.py`: 앱 라우팅
  - `core/views/`: 화면별 뷰 + 권한 체크
  - `core/context_processors.py`: 메뉴 표시 조건 계산
  - `core/models.py`, `core/forms.py`, `core/services/`: 도메인 데이터/폼/서비스 로직
- `templates/`
  - `templates/base.html`: 사이드바/공통 레이아웃
  - `templates/core/...`, `templates/registration/...`: 화면 템플릿
- `static/`
  - `static/css/`, `static/js/`, `static/vendor/`: 정적 자산(Bulma, 아이콘, 차트, 공통 스타일)
- `docs/`
  - 운영 메뉴얼, 외부 API 레퍼런스, 인수인계 문서 정리

---

## 1) 사전 준비

- Python 3.12+ 권장
- PostgreSQL (로컬은 Docker Compose 제공, 운영은 Render PostgreSQL 사용)
- UV(`uv`) 사용 환경(권장)

```bash
cp .env.example .env
```

`cp` 후 `.env`에 운영/로컬 값만 맞춰서 수정합니다.

---

## 2) 로컬 설치/실행(uv 기준)

```bash
uv sync
uv run python manage.py migrate
uv run python manage.py runserver localhost:10001
```

- 기본 DB 연결은 `.env`의 `POSTGRES_*` 또는 `DATABASE_URL` 값 기반입니다.
- 기본값은 PostgreSQL(`POSTGRES_*`)이므로 로컬 실행도 PostgreSQL 기준으로 맞춰 주세요.
- 개발 로그인은 라이트닝 인증 흐름이며, 아래처럼 `/accounts/login/`로 이동합니다.

로그인 관련 경로:

- 라이트닝 로그인: `/accounts/login/` (실제 동작은 `core:lightning-login`)
- 수퍼어드민 비밀번호 로그인: `/superadmin/login/` (실무 운영에서 관리자 접근)

관리자 전용 빠른 로그인 계정 생성:

```bash
uv run python manage.py createsuperuser
```

---

## 3) 배포( Render ) 가이드

두 가지 방식이 가능합니다.

### 3-1) Render Python 서비스 방식 (추천)

1) Render Web Service 생성
2) Build Command:

```bash
./build.sh
```

3) Start Command:

```bash
uv run gunicorn config.wsgi:application --bind 0.0.0.0:$PORT
```

4) 환경 변수(필수)

- `DJANGO_SECRET_KEY`
- `DJANGO_DEBUG=False`
- `DJANGO_ALLOWED_HOSTS=.onrender.com`
- `DJANGO_CSRF_TRUSTED_ORIGINS=https://<service-name>.onrender.com`
- `DATABASE_URL=<Render PostgreSQL 연결 문자열>`
- `BLINK_GRAPHQL_ENDPOINT` (운영 기본: `https://api.blink.sv/graphql`)
- `BLINK_API_KEY_ENCRYPTION_KEY` (없으면 `DJANGO_SECRET_KEY` 기반 처리)
- `IWINV_OBJECT_STORAGE_ACCESS_KEY`, `IWINV_OBJECT_STORAGE_SECRET_KEY`(첨부파일 업로드 사용 시)

5) 배포 후 마이그레이션

```bash
uv run python manage.py migrate
```

필요 시 정적 파일은 빌드 스크립트(`./build.sh`)의 `collectstatic`로 선행됩니다.

### 3-2) Render Docker 배포 방식

- 기존 `Dockerfile`은 uv 기반 빌드를 사용합니다.
- `pip` + `requirements.txt`를 사용하는 기존 문서 스타일과 다르게, uv 동작에 맞춰 `CMD`에서 Gunicorn 실행됩니다.
- Render에서 Docker를 쓰는 경우 `DATABASE_URL`이 자동 주입되는지 확인하고, 필요 시 환경변수로 명시합니다.

---

## 4) 라우트 기준 메뉴 사용법

사이드바 메뉴와 실제 라우트를 묶어 정리합니다.

- 라우트 출처: `core/urls.py`
- 메뉴 표시 조건 출처: `core/context_processors.py`
- 메뉴 렌더링: `templates/base.html`

### 4-1) 로그인 상태/권한 변수

`nav_profile` 컨텍스트는 다음 변수를 제공합니다.

- `nav_is_backoffice_admin`: `Member.is_backoffice_admin`
- `nav_is_active_member`: `Member.status == "active"`
- `nav_can_access_member_menus`: 활성 회원 또는 백오피스 관리자

### 4-2) 사이드바 항목별 사용법

| 메뉴 그룹 | 항목 | URL | View | 표시 조건 |
|---|---|---|---|---|
| 공통/회원 | 홈 | `/dashboard/` | `core:home` | `nav_can_access_member_menus` |
| 공통/회원 | 회원 목록 | `/members/` | `core:staff-members` | `nav_can_access_member_menus` |
| 공통/회원 | 마이페이지 | `/me/` | `core:my-dashboard` | 로그인 회원 전부 |
| 관리 | 승인대기 회원 | `/members/pending/` | `core:staff-members-pending` | `nav_is_backoffice_admin` |
| 관리 | 회비요청 회원 | `/members/dues-request/` | `core:staff-members-dues-request` | `nav_is_backoffice_admin` |
| 관리 | 반려 회원 | `/members/rejected/` | `core:staff-members-rejected` | `nav_is_backoffice_admin` |
| 관리 | 탈퇴 회원 | `/members/withdrawn/` | `core:staff-members-withdrawn` | `nav_is_backoffice_admin` |
| 활동/정산 | 활동 현황 | `/activities/` | `core:staff-activities` | `nav_can_access_member_menus` |
| 활동/정산 | 정산 현황 | `/settlements/` | `core:staff-settlements` | `nav_can_access_member_menus` |
| 활동/정산 | 정산 처리 | `/settlements/process/` | `core:staff-settlement-process` | `nav_is_backoffice_admin` |
| 로그 | 처리 타임라인 | `/logs/timeline/` | `core:logs-timeline` | `nav_can_access_member_menus` |
| 설정 | 주소 등록 | `/settings/address/` | `core:settings-address` | `nav_is_backoffice_admin` |
| 설정 | 기본 설정 | `/settings/basic/` | `core:settings-basic` | `nav_is_backoffice_admin` |
| 설정 | 메시지 공유 | `/settings/message-share/` | `core:settings-message-share` | `nav_is_backoffice_admin` |

### 4-3) 회원 상태별 접근 규칙 요약

- 일반 회원이 승인 전(대기/회비확인 대기 등)이면 사이드바는 제한적으로 보입니다.  
- 백오피스 관리자(`is_backoffice_admin=True`)는 관리/설정 메뉴를 모두 볼 수 있으며 대부분의 staff 뷰 접근이 가능합니다.
- 수퍼어드민(`superuser`) 로그인은 `/superadmin/login/` 전용 경로로 처리되며, 필요 시 `Member`가 자동 생성됩니다.

### 4-4) 주요 메뉴 사용 예시

- 일반 승인회원 운영 흐름:
  - 대시보드(`/dashboard/`)에서 회비/회원 지표 확인
  - 회원 목록(`/members/`)에서 회비 입금 완료 회원 관리
  - 활동/정산(`/activities/`, `/settlements/`)에서 활동비 등록·정산 상태 관리
  - 타임라인(`/logs/timeline/`)에서 시스템 이력 확인
- 회비확인 대상 처리:
  - 승인대기/회비요청 메뉴에서 각 회원을 승인/회비 요청 전환
  - 정산 처리 메뉴(`/settlements/process/`)는 관리자만 접근 가능
- 탈퇴/반려 이력 확인:
  - 반려 회원 메뉴는 반려 상태 목록 조회
  - 탈퇴 회원 메뉴는 탈퇴 이력 조회 전용
- 설정 계열:
  - 주소 등록/회비 기본 설정/메시지 공유는 관리 권한에서만 노출되며 설정값 변경 및 저장

---

## 5) 주요 엔드포인트 (코드 기준)

- 루트: `config/urls.py`
  - `/back-office/admin/`: Django 관리자
  - `/accounts/login/`: 라이트닝 로그인 진입점
  - `/superadmin/login/`: 수퍼어드민 로그인
  - `/accounts/`: Django auth 기본 라우트(로그아웃 포함)

- 앱 라우팅: `core/urls.py`
  - 인증: `/lightning-login/`, `/auth/lightning/callback/`, `/lightning-login/start/`, `/lightning-login/poll/<uuid>`, `/lightning-login/fake/<uuid>`
  - 마이페이지: `/me/`, `/me/nickname/`, `/me/withdraw/*`, `/me/dues-payment/*`
  - 회원 관리: `/members/*`, `/members/<id>/*`
  - 정산: `/settlements/*`
  - 활동: `/activities/*`
  - 설정: `/settings/*`

---

## 6) 정적 자원/아이콘 정책

- Bulma: `static/vendor/bulma/bulma.min.css`
- 로컬 Griddy 아이콘: `static/vendor/griddy-icons/griddy-icons-sprite.svg`
- 사이드바 아이콘은 `templates/core/components/griddy_icon.html`을 통해 라우트별로 `icon_name`을 주입해 사용합니다.
- Tailwind/React, Next 등 프레임워크 바인딩은 사용하지 않습니다.

---

## 7) 운영 체크리스트

1. `.env` 값 갱신
2. `uv sync` 또는 배포 빌드로 종속성 확보
3. 마이그레이션 실행
4. 정적 파일 수집 (`collectstatic`)
5. 관리자 계정/초기 설정 값 점검
6. Render 로그에서 CSRF/호스트/DB 에러 확인
7. 화면/권한 변경 시 `core/tests.py:SidebarMenuVisibilityTests`와 `core/tests.py:SuperAdminLoginRouteTests`로 동작 점검

---

## 8) 라이선스

이 프로젝트는 **MIT 라이선스**로 배포됩니다.  
라이선스 전문은 루트 `LICENSE` 파일을 확인하세요.
