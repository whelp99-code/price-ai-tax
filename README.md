# price · AI 세무사 — Ubuntu 내부 검증 릴리스 0.2.1

독립 프로젝트: `/home/jm/orca/projects/price` · 프로젝트명/worker명: `price` · 제품은 가격비교가 아니라 **AI 세무사**입니다.

> **현재 작업 트리는 10개 버전 계획의 개발 스냅샷입니다. 최종 1.0.0 인수판이 아닙니다.** 원본 업로드→경제적 거래 연결→업무 사실 질문 흐름과 후속 정책·작업표·제출 상태·운영 검증 구성요소를 추가했습니다. 최신 구현/미완료 범위는 `docs/releases/ENGINEERING_COMPONENTS_20260928.md`, 코드에 결합된 시험 증거는 `evidence/version-loop/snapshots/`를 확인하세요. 대표 기본 화면은 `/`, 기존 전문가 작업실은 `/expert`입니다.

현재는 증빙·거래·분개 제안·독립 검토·원장·대사·마감·VAT 검토 집계·모의 신고·질문·상담 연결을 실제 실행할 수 있는 내부 검증 앱입니다. 법인세·원천세·지방세 전체 법정 규칙, 홈택스/은행 실수집, 공식 신고·납부는 완료되지 않았습니다. 법정 정확성과 실제 회사 장부는 전문가 검증 전입니다.

## 바로 확인
현재 Ubuntu에 설치된 Python 3.14.4와 서버 패키지를 사용합니다. 글로벌 패키지를 추가 설치하지 않았습니다.

```bash
cd /home/jm/orca/projects/price
python3 scripts/runtime.py status
```

프로젝트 로컬 미리보기 실행(이미 실행 중이면 중복 생성하지 않습니다):

```bash
python3 scripts/runtime.py start
```

앱 주소: `http://127.0.0.1:4381` (Ubuntu 자신에서 접근하는 주소). 별도 프로세스 worker도 같은 합성 데모 DB를 사용합니다. 공개 네트워크에 바인드하지 않으며 systemd/재부팅 자동시작과 Orca worker 등록은 하지 않았습니다.

Mac에서 기존 SSH 연결로 접근하는 예:

```bash
ssh -N -L 4381:127.0.0.1:4381 jm@jm-acloud
```

터널이 연결된 동안 Mac 브라우저에서 `http://127.0.0.1:4381`을 엽니다. 기존 SSH 접속에 IP나 다른 Host 별칭을 쓴다면 `jm@jm-acloud` 부분을 동일하게 바꿉니다. 포트가 사용 중이면 기존 프로세스를 종료하지 말고 다른 로컬 포트를 선택하세요.

### 합성 데모 계정
- `demo-owner`: 회사 관리자·자료 입력·전표 확정
- `demo-reviewer`: 독립 검토·승인
- `demo-viewer`: 조회 전용
- 데모 공통 비밀번호: `DemoOnly-Price-2026!`

위 계정은 명시적으로 `init --demo`한 합성 DB에서만 존재합니다. 실제 회사 운영에 재사용하지 마세요. 현재 DB는 `.data/demo.sqlite3`이며 제품 출고 파일에 포함하지 않습니다.

## 사용할 흐름
1. 증빙 보관함에서 TXT/CSV/JSON/PDF/PNG/JPEG 원본을 올립니다. 2 MB 제한. PDF·이미지는 원본 저장만 하며 추출 대기로 표시합니다.
2. 거래 입력 또는 CSV로 매입·매출·은행 거래를 등록하고 증빙을 연결합니다.
3. 분석을 실행하거나 worker 큐에 등록합니다. 부족한 사실과 범위 밖 거래는 질문·보류로 표시합니다.
4. 검증된 분개안은 원거래 작성·사실 편집·전표 작성에 참여하지 않은 검토자가 승인합니다. worker가 만든 전표도 이 조건을 유지합니다.
5. 승인된 전표를 확정합니다. 확정전표와 증빙 원본은 덮어쓰지 않으며 역분개는 별도 전표로 작성·승인합니다.
6. 은행 거래를 청구 거래와 배분하고, 기초자료·수집 범위를 수기 확인한 다음 기간을 마감합니다. 미해결 사건·미지원 거래가 남으면 마감하지 못합니다.
7. 세금 화면은 VAT 계정 검토 집계입니다. 법정 완전 신고서는 아닙니다. 모의 접수/반려/통신장애를 시험하고, 결과가 불명확하면 재전송 대신 접수 재조회합니다.
8. 납부 증빙 기록은 수기 기록이며 실제 은행 납부 확인과 구분합니다. 상담은 모델 미연결 상태를 명확히 표시합니다.

## 검증 재실행

```bash
python3 -m pytest -q
python3 scripts/e2e_http.py
node scripts/e2e_browser.mjs
node --check price/static/app.js
python3 -m compileall -q price
```

2026-09-28 이 Ubuntu 체크아웃 결과: 자동시험 102개 PASS, 실제 HTTP 확인 21개 PASS, Chrome 브라우저 확인 22개 PASS. 사용 데이터는 합성 자료입니다. 테스트 프레임워크의 사용중단 예정 경고 2건은 남아 있으며 실패나 생략된 시험은 없습니다. 과거 패키지의 97/21 결과를 재사용한 수치가 아닙니다.

브라우저 스크립트는 Node 기본 WebSocket과 설치된 Google Chrome을 사용합니다. 관리자 정책이나 브라우저 sandbox를 비활성화하지 않습니다. 시험 프로세스만 종료합니다. 코드 수정 후에는 위 시험을 다시 실행하세요.

## 가동·정지·백업

```bash
python3 scripts/runtime.py status
python3 scripts/runtime.py stop
python3 -m price --db .data/demo.sqlite3 status
python3 -m price --db .data/demo.sqlite3 backup .data/backups/demo-20260928.sqlite3
```

백업 대상이 존재하면 덮어쓰지 않습니다. 복원은 새 파일에만 하며 SHA-256 확인, 세션 폐기, 외부 제출 재실행 금지를 적용합니다. `scripts/runtime.py stop`은 자신이 생성·기록한 프로젝트 로컬 프로세스만 정지합니다. 이 README 작성 시 stop을 실행한 것은 아닙니다.

직접 실행:

```bash
python3 -m price --db .data/demo.sqlite3 serve --host 127.0.0.1 --port 4381
python3 -m price.worker --db .data/demo.sqlite3 --once
```

실제 회사용 신규 초기화는 별도의 DB 경로에 `python3 -m price --db .data/company.sqlite3 init --company '법인명' --username owner`로 수행하며 터미널에서 관리자 비밀번호를 입력합니다. 현재 개발 작업에서는 실제 회사 DB를 생성하지 않았습니다.

## 패키지와 API
`pyproject.toml`, 고정 버전 requirements, wheel에 Python 모듈·SQLite 스키마·정적 화면이 포함됩니다. Ubuntu에 pip가 없으므로 설치된 setuptools의 build_wheel로 빌드했고, 독립 디렉터리에 wheel만 풀어 import/API/정적자산을 검증했습니다. 이는 pip 설치 시험과 다릅니다.

- `artifacts/price_tax_agent-0.2.1-py3-none-any.whl`
- `docs/openapi.json`: 현재 코드에서 생성한 API 명세
- `docs/DEVELOPMENT_PLAN_KO.md`: 18개 절·26개 작업 개발 계획
- `docs/IMPLEMENTATION_STATUS_KO.md`: 구현/검증/미지원 범위
- `evidence/ubuntu/`: 회귀·HTTP·브라우저·가동·wheel 검증 증거
- `worker.json`: worker 기능 선언. Orca 등록 성공을 뜻하지 않습니다.

## 남은 개발
모든 세목의 법정 완전 규칙·서식·세무조정·이월, 실제 금융/홈택스 수집, 세무 근거의 자동 갱신/전문 승인, 문서 항목 추출, 실제 LLM 품질평가, 전문 세무사·실장부 검증, 정식 신고·납부 채널, 상용 보안 운영은 후속 과제입니다. 금융·신고 어댑터 미연결과 제품 기능 미구현을 구분합니다. 공개 배포·커밋·push는 이번 작업에서 하지 않았습니다.
