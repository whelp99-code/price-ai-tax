# price — Ubuntu 구현·검증 상태 0.2.1

기준일: 2026-09-28. 실제 서버: jm-acloud. 독립 경로: `/home/jm/orca/projects/price`. 프로젝트·worker명: price. 제품: AI 세무사. Git branch main, 커밋·push 미실행. CORP-OS 등 다른 프로젝트는 변경하지 않았다.

## 실행 상태
프로젝트 로컬 API와 제안 worker를 `.data/demo.sqlite3` 합성 DB로 실행했다. `scripts/runtime.py status`로 현재 생존과 health를 재확인할 수 있다. Ubuntu loopback 주소는 `http://127.0.0.1:4381`. 공개 바인드·systemd 설치·재부팅 자동실행·Orca worker 등록은 하지 않았다. worker.json은 기능 선언이고, 별도 Python worker 프로세스의 실행과 Orca 등록은 다른 상태다.

실제 큐 작업은 SUCCEEDED를 반환했고 처리 전후 POSTED 전표 수는 각각 3건으로 같았다. 원장 중복 확정은 없었다. 모델 미연결, 실제 홈택스·은행·신고·납부 요청 0건이다.

## 구현과 검증
| 영역 | 이번 코드·결과 | 한계 |
|---|---|---|
| 독립 프로젝트 | Python 패키지/API/UI/worker/CLI, 원본 0.2.0 코어 이식 및 Ubuntu 통합 | 과거 ZIP 전체의 자동 전송이 아니라 직접 소스 이식·신규 구현 |
| 원본·거래 | 원본 BLOB/해시, 수기·CSV 원자 입력, 회사별 접근, 중복/충돌 | PDF·이미지 항목 추출·OCR 미연결, 금융·홈택스 수집 미연결 |
| 기장·승인 | 검증 분개, 원거래/사실편집자까지 독립 승인 검사, 확정 불변, 역분개 | 비정형·외화·관련자·자산 등 지원 밖은 보류 |
| 대사·마감 | 부분·합산 배분, 초과/방향 검증, 수집·기초 수기 확인, 미해결 마감 차단 | 실제 은행 조회·자동 대사 완전성 검증은 아님 |
| 세금·신고 | 확정 원장 VAT 검토 집계, 승인 스냅샷, 모의 접수/반려/결과불명/재조회 | 법정 완전 VAT 계산·서식, 법인세·원천세·지방세 규칙 미완성 |
| 상담·worker | 구조화 Ollama 어댑터, 근거ID·출력검사, 미연결 표시, 영속 제안 큐 | 실제 모델 호출·전문 세무상담 품질 미검증 |
| 보안·운영 | 회사/RBAC/세션/CSRF, 감사체인·변경이력, 백업/복원, worker 내용 로그 제거 | 외부 감사 고정·침투테스트·상용 운영 검증 미완성 |
| 화면 | 9개 메뉴, 거래/사실/승인/대사/마감/모의신고/근거/상담/사용자 폼 | 현재는 합성 데이터 기반 내부 검증 |

## 새로 재현하고 수정한 결함
기존 전표 작성자만 비교하던 독립 승인에서 worker 또는 다른 분석자를 경유하면 원거래 작성자·사실 수정자가 승인할 수 있는 3개 경로를 새 시험으로 재현했다. `Service._journal_contributors`가 회사별 불변 감사이력과 원거래 작성자를 확인하도록 수정했고, 승인뿐 아니라 전표 확정 시에도 다시 검사한다. 원본 DB나 확정 전표를 변경하지 않았다. 별도로 worker CLI 로그에서 거래·증빙 결과 본문을 제거하고 상태만 출력하도록 수정했다.

## 실제 시험 결과
- 단위/통합/보안/운영/설치·로그 회귀: **102 PASS, 0 FAIL, 0 SKIP**. 증거 `evidence/ubuntu/final-tests.xml`.
- 실제 loopback HTTP: **21 PASS**. 증거 `evidence/ubuntu/http-e2e-report.json`.
- 실제 Chrome 브라우저: **22 PASS**. 로그인, 9개 메뉴, 거래 입력, HTML 문자열 비실행, 자기승인 거부·타인 승인·확정, 390px 화면 넘침, JavaScript 예외 없음. 증거 `evidence/ubuntu/browser-report.json`.
- 화면: `browser-home-desktop.png`, `browser-ledger-desktop.png`, `browser-mobile.png` (모두 evidence/ubuntu 아래).
- 실제 별도 worker 처리: `runtime-job-report.json`, SUCCEEDED, 확정 전표 3→3.
- wheel 생성 및 독립 추출 import/API/정적자산/스키마: PASS, `wheel-report.json`. pip가 없어서 setuptools build_wheel을 사용했고 pip 설치 시험이라고 표시하지 않는다.
- Python·JavaScript 문법검사 PASS. 프레임워크 사용중단 예정 경고 2건은 남아 있다.

위 수치는 이번 Ubuntu에서 다시 실행한 결과다. 과거 패키지의 97개 자동시험/21개 HTTP 숫자를 재사용한 것이 아니다. 모든 시험은 합성 자료이며 실제 회사 장부·세법 정확도·상용 성능·절세 효과를 입증하지 않는다.

## 남은 개발 및 출시 차단
법인세·원천세·지방세·급여·자산·세무조정·이월의 전체 규칙, 법정 신고서/제출 채널, 실제 금융·홈택스·문서 추출, 근거 자동갱신·전문 승인, 실제 모델 품질, 실제 월·연간 장부와 전문가 검증이 필요하다. 실제 신고·납부/송금은 fail-closed이며 모의 접수를 실제 신고로 표시하지 않는다. 상담/일부 수학함수 또는 전체 테스트 PASS가 완전한 AI 세무사 개발 완료를 의미하지 않는다.

## 실행 방식 참고
자동 Codex 에이전트 프로세스 호출은 별도 승인 게이트로 실행되지 않았다. 허용된 프로젝트 파일 편집과 실제 Ubuntu 검증 명령으로 직접 구현했다. 초기 E2E 서버 helper는 접속확인에 실패했으나 실제 HTTP 시험을 독립 프로세스로 통과했고, 이후 프로젝트 로컬 runtime supervisor로 앱과 worker 실행을 확인했다. `docs/IMPLEMENT_NOW.md`는 미실행 에이전트용 작업 지시서일 뿐 실행 결과가 아니다.
