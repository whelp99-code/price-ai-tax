# f8b0b319 릴리스 준비와 별도 운영 전환 절차

> 과거 기록: 파일 이름의 `4b26`과 아래 `7469` 폴더 venv 문장은 이전 준비(PR #21 기준)의 기록이다. 이번 대상은 `f8b0b319`, 롤백 대상은 `9abb308e`다.

이 문서는 `jm-omachy`의 준비 작업과 Cursor 검수 후 수행할 운영 전환을 구분한다. 준비 스크립트는 릴리스 폴더만 생성한다. unit, 서비스, 회사 DB, 계정, 키, Notion은 변경하지 않는다.

## 대상과 이전 방식

- 대상 소스: `f8b0b3192ce3dbc4f63eff7cf333dd0fa39f7d4a`
- 현재 운영 소스 및 이번 전환의 롤백 대상: `9abb308e3d9358b6f733c799ac30f88b1119fe1a`
- 릴리스 루트: `/home/jm/.local/share/price/releases`
- 서비스: 사용자 `price-api.service`, 운영 포트 `127.0.0.1:4382`
- WorkingDirectory override: `~/.config/systemd/user/price-api.service.d/80-pr15-release.conf`
- 공유 실행기 override: `90-venv.conf`; 실행기는 `779a8391915d2b6af5a5295b32e25d2d768980ac/.venv/bin/python` 그대로다.

이전 기록: [Issue #10 마지막 배포·롤백 댓글](https://github.com/whelp99-code/price-ai-tax/issues/10#issuecomment-6085523274).
(과거 기록) `7469` 폴더에는 자체 `.venv`가 없었다. 현재 운영 `9abb308e` 폴더에도 자체 `.venv`가 없다. 이번 준비도 새 venv나 키를 만들지 않는다. 새 폴더를 가리키는 symlink나 service override도 만들지 않는다.

## 운영 코드 diff

```sh
git diff --stat 9abb308e3d9358b6f733c799ac30f88b1119fe1a f8b0b3192ce3dbc4f63eff7cf333dd0fa39f7d4a
git diff --numstat 9abb308e3d9358b6f733c799ac30f88b1119fe1a f8b0b3192ce3dbc4f63eff7cf333dd0fa39f7d4a -- price pyproject.toml requirements.txt requirements-test.txt requirements-readers.lock
```

두 커밋 트리의 직접 비교다. `9abb308e`는 `f8b0b319`의 조상이 아니므로 선형 이력이 아니라 트리 차이로 읽는다.

- 운영 경로(`price/`, `pyproject.toml`, `requirements*`): 22파일, 849줄 추가·99줄 삭제.
- `price/api.py`(+1)와 `price/service.py`(+9/−1)는 다르다.
- `price/db.py`와 `price/schema.sql`은 두 커밋에서 blob 해시가 같다.
- 운영 diff 본문에서 `CREATE`/`ALTER`/`DROP`/migration 문자열을 grep한 결과는 0건이다. 이는 문자열 검사이며 실행 경로 검토는 아니다.
- `pyproject.toml`(+2/−2)과 `requirements-test.txt`(+1)도 바뀐다. `requirements.txt`는 같다.
- 변경 파일별 의미 검토는 이 문서에서 하지 않는다. 각 병합 PR의 검수 기록을 따른다.
- 현재 운영 폴더 `releases/9abb308e…`의 `price/`·`pyproject.toml`·`requirements.txt` 88개 파일은 커밋 `9abb308e`의 blob 해시와 모두 같다(2026-11-20 확인).

(과거 기록) PR #21 때의 「세 파일, 20줄 추가·11줄 삭제」「`api.py` diff 없음」 설명은 `7469`→`4b26` 비교였으며 이번 대상에는 적용하지 않는다.

이 준비 PR의 스크립트는 검수된 작업 트리에서 실행한다. 만들어지는 운영 소스는 준비 PR의 head가 아니라 위에 고정한 `f8b0b319`이다.

## 전환 없는 준비

```sh
cd /home/jm/orca/lanes/price
python scripts/prepare_release.py \
  --repo /home/jm/orca/lanes/price \
  --releases-root /home/jm/.local/share/price/releases \
  --commit f8b0b3192ce3dbc4f63eff7cf333dd0fa39f7d4a
```

Git의 정확한 커밋 archive만 풀고 `.price-release.json`으로 소스 해시를 묶는다. 작업 트리의 미추적 DB·세션·환경 파일은 복사하지 않는다. archive에 그런 파일이나 symlink가 있으면 거부한다.
기존 대상 폴더나 깨진 symlink도 `TARGET_EXISTS`로 거부하며 덮어쓰지 않는다. 이미 준비한 폴더는 다음 실제 해시 검증만 실행한다.

추출의 `filter` 인자를 지원하지 않는 인터프리터(Python 3.11.0–3.11.3)는 `EXTRACTION_FILTER_UNSUPPORTED`로 거부하고 이번에 생성한 대상 폴더를 삭제한다. 필터 없는 추출로 재시도하지 않는다.

```sh
python scripts/prepare_release.py \
  --repo /home/jm/orca/lanes/price \
  --releases-root /home/jm/.local/share/price/releases \
  --commit f8b0b3192ce3dbc4f63eff7cf333dd0fa39f7d4a \
  --verify-only
```

운영 Python이 생성하는 `__pycache__` 안의 파일과 `.pyc`는 소스 대조에서 제외한다. 나머지 파일과 manifest는 고정 Git archive와 계속 대조하므로, 바이트코드가 존재해도 원본 `.py` 변경은 `RELEASE_SOURCE_MISMATCH`로 거부한다. symlink 거부도 유지한다.

## 읽기 전용 health 확인

```sh
/home/jm/.local/share/price/releases/779a8391915d2b6af5a5295b32e25d2d768980ac/.venv/bin/python \
  scripts/release_health_smoke.py \
  --repo /home/jm/orca/lanes/price \
  --release /home/jm/.local/share/price/releases/f8b0b3192ce3dbc4f63eff7cf333dd0fa39f7d4a \
  --python /home/jm/.local/share/price/releases/779a8391915d2b6af5a5295b32e25d2d768980ac/.venv/bin/python
```

스크립트는 회사 DB를 인자로 받지 않는다. 임시 디렉터리 안에서만 실제 대상 `price.api.create_app`으로 빈 fixture의 전체 스키마를 준비한다. 업로드 같은 추가 모듈은 API 생성 중 `BEGIN IMMEDIATE`와 버전 marker 삽입을 하므로 기본 `schema.sql`만 먼저 만든 읽기 전용 파일로는 실제 API를 생성할 수 없다.

준비 후 회사·사용자 행이 없고 DB 외의 추가 파일이 생기지 않았음을 확인한다. journal을 DELETE로 닫고 `0400`으로 전환한 뒤, 같은 실제 API 객체의 DB 연결을 SQLite URI `mode=ro`와 `query_only`로 제한하여 listen한다. fixture 준비 쓰기는 서버 listen 전에 임시 DB에만 수행한다. 운영 CLI의 `init`은 사용하지 않으며 새 owner·회사·키를 만들지 않는다.

연결마다 임시 fixture 경계를 확인한다. URI 보호는 `query_only`를 끈 상태에서도 쓰기 거부로 검증하며, 서버의 실제 DB 객체들도 같은 연결 경계를 사용한다. readonly schema marker만 흉내 내거나 SQL 쓰기를 성공한 것처럼 무시하는 방식이 아니다.

자식은 비밀 환경 변수를 전달받지 않고 대상 소스 경로를 검증한다. 임시 loopback 포트의 bound-server 로그를 이벤트로 기다린다. 선택한 URL을 기록한 후 curl로 `GET /health`를 한 번만 호출하며 HTTP 상태·헤더·본문을 보존한다. 재시도나 고정 sleep은 없다.
이후 SIGTERM·종료 대기·임시 DB 삭제를 수행한다. `status=200`, readonly 증명, fixture 불변 및 종료·삭제 결과가 모두 확인되어야 통과다.

## Cursor 검수 후 별도 배포 담당자가 수행할 전환

아래 단계는 준비/검증 작업에서는 실행하지 않는다. 기존 회사 백업의 성공 상태와 현 운영 WorkingDirectory를 먼저 확인한다. 전환 직전 WorkingDirectory가 `releases/9abb308e3d9358b6f733c799ac30f88b1119fe1a`가 아니면 전환을 멈추고 롤백 대상을 다시 정한다.

```sh
systemctl --user show price-company-backup.service -p Result -p ExecMainStatus
systemctl --user show price-api.service -p MainPID -p ActiveState -p WorkingDirectory
```

검수·운영 전환 승인이 있고 소스 검증 및 임시 health가 통과한 경우에만 `80-pr15-release.conf`를 다음 내용으로 변경한다. `90-venv.conf`와 base unit은 그대로 둔다.

```ini
[Service]
WorkingDirectory=/home/jm/.local/share/price/releases/f8b0b3192ce3dbc4f63eff7cf333dd0fa39f7d4a
```

```sh
systemctl --user daemon-reload
systemctl --user restart price-api.service
systemctl --user show price-api.service -p MainPID -p ActiveState -p WorkingDirectory
curl --include --max-time 10 http://127.0.0.1:4382/health
```

일반 운영 `serve`는 기존 SQLite 초기화 경로를 수행한다. 따라서 이 재기동은 운영 DB 쓰기가 승인된 별도 배포 단계다. 이번 target은 `db.py`·`schema.sql`이 롤백 대상과 같고 위 문자열 검사에서 DDL이 0건이며, `init`, owner 재생성, 키 생성은 필요하지 않다.

## 롤백

기동 실패 또는 승인된 점검 실패 시 `80-pr15-release.conf`를 기존 운영 폴더로 돌린다. 공유 실행기는 계속 기존 `779a839`의 Python이다.

```ini
[Service]
WorkingDirectory=/home/jm/.local/share/price/releases/9abb308e3d9358b6f733c799ac30f88b1119fe1a
```

그다음 위의 `daemon-reload`, `restart`, 상태 조회와 운영 `/health` 확인을 수행한다. `db.py`·`schema.sql`이 같으므로 소스 롤백을 이유로 회사 DB를 초기화·덮어쓰기·복원하지 않는다.
새 릴리스 폴더는 준비된 소스로 남겨 조사할 수 있으며, 기존 릴리스를 삭제하지 않는다.
