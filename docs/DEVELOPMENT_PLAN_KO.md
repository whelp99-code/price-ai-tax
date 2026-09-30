# price · AI 세무사 — Ubuntu 개발 재개 계획

## 1. 목표와 범위
프로젝트와 worker 이름은 price, 제품은 AI 세무사다. 가격비교 앱이 아니다. 사용자가 새로 생성한 /home/jm/orca/projects/price가 유일한 작업 경계다. CORP-OS와 다른 프로젝트는 변경하지 않는다. 2026-09-24 원래 AI 세무사 기획과 2026-09-26 독립 0.2.0 패키지의 기능 계약을 기준으로 Ubuntu 네이티브 실행·회귀검증·개선을 진행한다. 패키지의 전체 파일이 원격에 자동 마운트된 것은 아니다. 원본을 읽고 core db/schema/models/auth부터 이식했다. 원본의 과거 시험 수 97/21을 이번 시험 수로 재사용하지 않는다. 원본 패키지는 채팅 첨부에 보존되어 있다.

## 2. 완료 기준 구분
A: 독립 디렉터리·Git 발견 및 소스 구성. B: 실제 Ubuntu 단위/통합 시험 통과. C: 실제 HTTP와 브라우저 과업 검증. D: loopback 내부 검증 서비스와 proposal worker 실행. E: 실제 회사 장부·법정 세목·전문가 정답셋·실신고·금융연계·운영 보안 검증. A~D가 완료되어도 E와 전체 AI 세무사 완성이라고 표시하지 않는다. 지금 모든 외부 신고·납부·송금은 차단한다.

## 3. 아키텍처
Python 3.11+ 모듈형 단일 앱, FastAPI/Pydantic API, SQLite 영속 DB, 정적 한국어 웹 UI, 독립 제안 worker. AI 해석과 결정론적 회계·계산·권한을 분리한다. 이미 이식한 Database/Auth/models/schema의 회사별 scope, revision CAS, append-only audit, immutable posted journal와 evidence를 보존한다. 새 서버 구현은 지원 API를 문서화하고 실제 테스트로 기능을 입증한다. 모델은 Ollama 옵션이며 연결 안 된 상태를 규칙 기반/미연결로 표시한다.

## 4. 데이터 모델
회사·사용자·멤버십·세션, 원본 BLOB와 해시, 외부 ID와 원본 해시, transaction/case/question/journal/approval/allocation/period/authority/calculation/filing/submission/payment/consultation objects, 이전 revision history, append-only audit, durable jobs. 금액은 bool/float를 거부하는 정수 KRW, 세율·배분 계산은 Decimal. 데이터베이스는 price 전용 schema version 검사 후 열고 다른 DB를 변경하지 않는다. 기존 파일과 DB 초기화를 덮어쓰지 않는다.

## 5. 인증·권한
owner/preparer/reviewer/viewer. password PBKDF2, 랜덤 HttpOnly SameSite session, CSRF 헤더, 만료·disabled 사용자·역할 재검사. 작성자 자기 승인을 금지한다. owner도 자신이 작성한 전표/신고안은 다른 검토자의 승인이 필요하다. worker는 원장 전기/승인/외부실행 권한이 없다. 서버 기본 bind는 127.0.0.1. 데모 DB는 외부 인터페이스 bind 금지. 요청 본문 제한, 안전한 응답 헤더, 사용자 텍스트 DOM textContent 또는 escape. 공개 URL·systemd 활성화·커밋·push는 추가 명시 요청 없이 하지 않는다.

## 6. 원본·거래 수집
TXT/CSV/JSON/PDF/PNG/JPEG 원본, 확장자·서명·크기·파일명 확인, 원본 SHA-256 및 회사별 중복. PDF/이미지는 원본 보관만 해도 정확히 EXTRACTION_PENDING/OCR_NOT_RUN으로 표시. CSV 전량 원자 입력, 실패 시 rollback. 외부ID+body hash 동일 재수집은 기존 반환, 같은ID 다른 내용은 충돌. 정상 반복거래를 금액·날짜가 같다는 이유로 삭제하지 않는다. 실제 은행·홈택스 수집 완료를 주장하지 않는다.

## 7. 회사 사실·사건·분개
회사 맥락과 거래 목적·세무구분·공제가능성·증빙 누락은 질문으로 확인한다. unsupported 외화/관련자/자산/불명 거래는 BLOCKED_SCOPE/NEEDS_EXPERT/NEEDS_FACTS. invoice만 수익/비용의 매입매출 전표 후보를 만들고 bank record를 비용으로 중복 기록하지 않는다. sale: receivable gross debit, revenue net credit, vat_output tax credit. purchase 공제 가능: expense 또는 inventory net debit, vat_input tax debit, payable gross credit; 불공제 tax는 비용·재고원가 포함. 0금액 라인은 생략. 새로운 세무 판단 자체를 임의 추정하지 않는다. 최소 2줄 차대 일치 검증.

## 8. 승인·확정·역분개
전표 초안/검증→다른 reviewer 승인→owner 또는 승인된 권한자 확정. approval actor, role, expiry, snapshot hash, transaction revision, period lock을 매 실행마다 재확인. 원거래 변경 후 이전 approval 무효. 반복 실행은 중복 전표를 만들지 않는다. POSTED 원본은 SQL trigger로 수정/삭제 불가; reversal은 새로운 전표와 반대 방향 라인을 만들고 독립 승인 후 전기한다. 이미 reversal 진행/전기된 전표의 중복 역분개는 차단.

## 9. 대사·월마감
bank receipt↔sale, payment↔purchase 배분. 회사·통화·방향 일치, 양쪽 gross 한도 초과 금지, 부분/합산 허용. 가능한 한 미확정 청구와 배분 과다/차액을 마감 blockers로 보고한다. 기간 생성, required_sources, opening_reviewed, sources_confirmed, evidence_note; 사용자 수기 확인은 자동 수집 완전성 보장과 구분. 미지원·미기장·미해결 사건을 숨기지 않는다. 기간 close는 blockers=0일 때만, reopen은 사유·권한·감사 기록을 요구.

## 10. 세무근거·세금 검토
공식 HTTPS law.go.kr/nts.go.kr 계열 출처와 효력기간, 원문 발췌, 타인 검토, revision 보존. 사용자 제공 URL 자동 방문 금지. VAT는 확정 원장의 vat_output와 vat_input 검토용 집계와 원본 추적이며 법정 전체 계산/서식이 아니다. CORPORATE/WITHHOLDING/LOCAL_TAX 완전규칙이 없으면 명시적 UNSUPPORTED로 차단. 숫자 수학함수(정률/누진/정액기간배분)는 법정 세무 규칙과 구분.

## 11. 신고·납부
DRAFT→REVIEWED/VALIDATED→APPROVED→SUBMITTING→SIM_RECEIVED/REJECTED/SUBMISSION_UNKNOWN. 샌드박스 SIM- 접수번호만 생성. 통신 timeout_after_accept를 시뮬레이션하여 unknown에서 재제출 금지·기존 원격 결과 재조회만 허용. 승인대상 계산/원장 snapshot 변경·기초기간 reopen·권한철회·승인만료는 제출 차단. production/live 제출은 모든 경로에서 fail closed. 납부/환급 상태는 신고와 별개이며 수기 납부 증빙도 은행 검증 완료라고 표시하지 않는다.

## 12. 상담·worker
근거 유효기간·승인상태 검사. 질문→부족한 사실→근거 ID→결론→위험·다음행동 계약. LLM 응답은 제안만; DB 자유 SQL·shell·송금 도구 없음. 실제 모델 없을 때 규칙 기반 설명과 질문 반환, 모델 응답 위장 금지. Ollama 호스트 허용·timeout·출력 크기·schema 검사·허용되지 않은 citation 거부. 영속 큐는 (company,transaction,input_revision) unique, lease/fencing/retries, 원거래변경 STALE_JOB, worker는 approve/post 금지.

## 13. UX
로그인, 오늘의 업무, 증빙·거래(원본 업로드/CSV/분석), 질문·사건(답변), 전표·검토(승인/확정/역분개), 대사·마감(배분/차액/blocker), 세금·신고(검토금액/모의상태/납부구분), 상담·근거, 연결·감사·작업 큐. 실제 API에 연결하고 빈 데이터·실패·401·권한 오류·저장 상태를 명시한다. 모바일 가로 넘침 방지, 한국어, 접근성 label. 데모는 synthetic 및 실신고 불가 배너. seed_demo는 명시적인 init --demo에서만, 초기 owner/reviewer/viewer와 합성 거래; 일반 init에는 데모 사용자를 만들지 않는다.

## 14. 운영
설치 requirements/pyproject, .gitignore, README, worker.json(Orca 등록 여부 정직하게), docs/OpenAPI, backup/restore(no overwrite, hash/integrity, sessions revoke, external submission replay 금지), /health, CLI status, verification scripts. service examples만 작성 가능, systemd 활성화/공개 포트 금지. 로그에 비밀번호/세션/증빙 원문 기록 금지. 실제 실행 증거에는 runtime·시각·합성자료 여부·시험수·실패·미검증 범위 기록.

## 15. WBS
P01 독립 설치/CLI; P02 DB·revision·history; P03 인증·회사격리; P04 증빙·해시; P05 transaction/CSV; P06 질문·facts·scope; P07 원장·독립승인; P08 대사; P09 기간마감; P10 세법근거; P11 VAT 검토; P12 수학함수; P13 신고안 snapshot; P14 sandbox 제출/재조회; P15 납부기록; P16 durable worker; P17 상담/Ollama; P18 UI; P19 백업/복구/export; P20 배치/가동검증; P21 회귀/HTTP/브라우저; P22 실제 수집(미연결); P23 법인세/원천세/지방세/급여/자산/이월 완전규칙(미완성); P24 법정서식/실신고(미연결); P25 검증된 무인자동확정(미완성); P26 실제 전문가/실장부/연간완결 평가(미검증). P01~P21의 지원범위를 실제 구현/검증한 후 후속으로 확장한다. TODO만 넣고 구현이라고 표시하지 않는다.

## 16. 회귀검증
기존 0.2 패키지는 별도 환경의 97개 자동시험/21개 HTTP를 보고했으나 현재 체크아웃에는 그 전체 파일이 자동 복사된 것이 아니므로 해당 숫자 인용 금지. 이 체크아웃의 실제 시험 수를 계산한다. 새 시험은 positive+negative: strict integer, unbalanced lines, auth/CSRF/tenant/RBAC, self approval, stale/revoked/expired approvals, immutable evidence/posted records, revised source, CSV rollback/duplicate, bank allocation overrun/direction/FX, closing blockers/lock/reopen, unsupported tax, invalid authorities, untrusted LLM output, unknown submission retry, bank-unverified payment, durable worker stale job/lease, audit tamper, backup/restore. 테스트를 통과시키기 위해 안전 요구사항이나 assertion을 약화하지 않는다.

## 17. HTTP·브라우저 검증
실제 loopback 서버를 임시 합성 DB로 띄워 로그인→증빙→거래→분개→자기승인거부→다른검토자승인→전기→대사→마감→VAT검토→모의신고 unknown→재조회→납부증빙을 검증. 브라우저는 설치된 Chromium/Playwright로 전체 화면/클릭/XSS text/mobile overflow/console error를 확인하고 screenshot 저장. 관리자 정책 때문에 차단이면 정책을 변경/우회하지 않고 차단 증거를 남긴다. HTTP 시험은 별도 수행한다. 기존 앱의 포트와 프로세스를 종료하지 않는다.

## 18. 현재 인수 범위와 후속
이번 목표는 새 폴더에서 실제 코드·검증·loopback 가동까지 진전시키는 것이다. 전체 AI 세무사·전문 자격·정식 신고·절세 정확도·상용 준비 완료를 주장하지 않는다. 후속은 실제 수집/원본 추출, 유효기간 있는 근거·규칙 배포, 세목별 전문가 정답검증, 공식 서식·제출지원, 실제 데이터의 월/연간 마감이다. 준비된 소스 없이 임의 코드를 전체 0.2 원본 배치라고 표시하지 않는다. 최종 보고서는 현재 만들어진 파일·실행 명령·결과·접속방법·남은 사항을 포함한다.
