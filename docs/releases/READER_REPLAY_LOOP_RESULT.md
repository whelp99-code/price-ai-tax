# price — 원본 읽기·재처리 개발 루프 실행 결과

기준: 2026-09-28 실제 Ubuntu `/home/jm/orca/projects/price`. 이 기록은 V01~V10 전체 인수서가 아니다. 개발 후보 코드와 실제 검증 범위, 미구현·외부 의존을 구분한다. 같은 작업 트리에 새로 추가된 다른 구현은 덮어쓰지 않고 통합 시험했다.

## 수행한 루프

1. 현재 소스·업로드 우선 요구사항을 확인하고 기준선 전체 회귀시험을 실행했다. 재개 시점에는 이전 채팅보다 업로드·원본 파서·대표 화면 구현이 더 진행돼 있었으며, 기준선은 314 PASS였다.
2. 없는 XML 파서, 재처리 충돌·이전 결과 오표시, 잔액 및 통화 대조 누락, 재처리 API 바인딩, 수정 XML의 일반 거래 오인, 이력 조회 무제한 문제를 각각 실패 시험으로 재현했다.
3. 원본·확정 장부·기존 승인 통제를 보존하는 작은 수정으로 문제를 해결했다.
4. 각 변경의 회귀시험과 실제 HTTP/API·Chrome 대표 과업을 재실행했다. assertion이나 기존 회귀시험을 삭제해 통과시키지 않았다.
5. 현재 전체 회귀시험은 `evidence/version-loop/current-full-regression.xml`: **494 PASS / 실패·오류·생략 0**, 프레임워크 deprecation 경고 2건이다. 세법 정확도나 실기관 인수를 뜻하지 않는다.
6. `scripts/e2e_reader_replay.mjs`: 실제 Chrome **23개 확인 PASS**, 원본 파일 선택부터 worker 읽기, 회사 사실 정정, 실제 다시 읽기 버튼, 보존된 이력 확인까지 실행했다. 합성 자료만 사용했다.

## 이번에 연결·보강한 범위

- KEC TaxInvoice XML: 정확한 namespace·중복 필드·필수 금액·품목 합계·수정 정보·원본 경로를 보존한다. XML 존재를 전자서명 검증·기관 접수·세액 공제 판단으로 취급하지 않는다.
- HTML로 저장된 `.xls`: 비실행 flat table 파서로 실제 셀을 읽는다. script·외부 참조·이벤트·위험한 CSS·중첩 표·병합 모호성은 격리 또는 실패다. 바이너리 OLE XLS와 구분한다.
- 텍스트 PDF: 명확하게 분리된 원본 열만 읽고 페이지/행을 남긴다. 흐린 스캔이나 모호한 열을 임의 복구했다고 표시하지 않는다.
- 은행 잔액 대조: 동일 계좌·통화 내 전후 잔액과 입출금만 비교한다. 다른 통화 합계를 하나의 숫자로 검증하지 않는다. 기초잔액과 전체 수집 완전성은 별도 판정이다.
- 재처리: 입력 revision·요청 멱등키·원본 해시·작업 lease를 확인한다. 이전 결과는 보존하고 새 generation을 만들며, 재처리 중 과거 결과를 현재 완료로 표시하지 않는다.
- 영향 분석: 후속 정책 실행 허가는 원본 변경 시 재검증되고, 이미 확정된 전표는 그대로 보존하며 정정 검토 사건만 만든다.
- 대표 화면: 같은 회사·권한 아래 쉬운 오류와 다시 읽기, 결과 이력을 제공한다. 계정과목 선택이나 검토자 계정 전환이 필요하지 않다.
- 이력 부하: 전체 원본 값 반복 전송 대신 페이지 단위 요약과 제한된 변경 목록을 반환한다. 원본 전체 snapshot은 보존한다.
- 최종 인수 증거: 기능 파일 몇 개만 아니라 인증·DB·파서·UI·스키마·의존성 입력을 모두 source manifest에 포함한다. 중복 JSON 필드·NaN/Infinity는 거절한다. 실제 서명·인수 자료는 생성하지 않았다.
- 프로세스 복구: API/worker 중 하나만 살아 있는 경우 살아 있는 프로세스를 종료하지 않고 빠진 구성만 복구하도록 수정하고 시험했다.

## 증거

- `evidence/version-loop/current-full-regression.xml`
- `evidence/version-loop/reader-replay-source-snapshot.json` — 이전 안정 소스 468 PASS 실행의 파일 해시. 더 최신 494개 시험과 혼동하지 않는다.
- `evidence/version-loop/source-api-verified.xml`
- `evidence/version-loop/source-policy-after.xml`
- `evidence/version-loop/history-limits-after.xml`
- `evidence/version-loop/release-binding-before.xml` / `release-binding-after.xml`
- `evidence/version-loop/reader-replay/reader-replay-browser-report.json`
- 동일 디렉터리 데스크톱·모바일·원본 PDF 표·재처리 이력 캡처 6장.

## 실행 상태

현재 소스와 기존 DB를 로컬 checkpoint로 보존한 뒤 기존 합성 demo DB로 API와 제안 worker를 다시 실행했다. 확인 시점 API health 정상, API PID 3986959, worker PID 3986971, loopback `127.0.0.1:4381`, `live_filing=false`, 표시 버전 0.2.1이다. systemd·공개 포트·재부팅 자동시작·Orca 등록·Git commit/push는 수행하지 않았다. 이 프로세스는 앱 작업을 처리하며, 별도 개발 에이전트를 백그라운드로 예약했다는 뜻이 아니다.

## 실제로 남은 범위

V01의 실제 기관 원본 묶음 인수는 확보되지 않았다. V02의 바이너리 OLE XLS, 스캔/사진, 암호 파일, 기관별 원본 정답 매트릭스는 완료가 아니다. 현재 파서는 합성 fixture와 일부 공개 구조를 기준으로 검증했으며 기관 인증을 주장하지 않는다.

현재 작업 트리에 거래 연결·사실 질문·전문 검토·정책 실행·월별 계산/보고·급여/법인세 검토지·제출 게이트·인수 게이트 코드가 있다. 코드 존재와 합성 시험은 해당 V03~V10의 실제 전문·기관·연간 인수를 의미하지 않는다. 정책 실행과 live 제출은 기본 비활성이다. 완전한 세목별 법정 규칙·공식 서식, 실제 모델/전문 검토자, 실제 수집·발행·제출·접수·납부, 실제 3개월/1사업연도 및 30일 관찰은 대체 증거로 채우지 않는다. **최종 1.0.0은 인수하지 않았다.**

## 외부 패키지 설치 승인 경계

바이너리 XLS 읽기에 사용할 공개 `xlrd==2.0.2` wheel을 해시 고정한 `requirements-readers.lock`을 작성했다. 시스템 Python을 바꾸지 않는 프로젝트 `.venv` 설치 요청은 실제 `APPROVAL_REQUIRED`를 반환해 실행되지 않았다. 확인 결과 `.venv/pyvenv.cfg`는 없다.

요청 ID: `b044122c-189b-4019-96fb-17bec3243968`. 해당 요청은 만료 시간이 있는 일회성 승인 요청이며, 이 문서의 존재를 승인으로 취급하지 않는다. 승인 전 다른 다운로드 경로·권한 변경·인코딩 등으로 설치를 우회하지 않는다. 이 설치 승인이 모든 세법·기관·최종판 인수를 해결한다는 뜻도 아니다.

서명 증거 처리 관련 큰 소스 수정 명령 한 건에는 `SECRET_BLOCKED`가 반환됐다. 실제 비밀파일을 읽거나 승인 설정을 바꾸지 않고, 별도의 순수 source manifest 모듈과 공개 JSON 입력 검증만 구현·통합했다. 보호된 runtime private 자료는 열람하지 않았다.
