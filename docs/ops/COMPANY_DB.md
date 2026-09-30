# 실회사 DB (데모와 분리)

데모는 그대로 둔다. `scripts/runtime.py`와 `127.0.0.1:4381` / `.data/demo.sqlite3`를 멈추거나 덮어쓰지 않는다.

- 회사 DB: `/home/jm/.local/share/price/company.sqlite3` (0600)
- 초기화: `python3 -m price --db /home/jm/.local/share/price/company.sqlite3 init --company '주식회사 베를로' --username owner` (`--demo` 아님)
- 사업자번호는 CLI init에 없다. 지원 경로인 owner onboarding confirm(`company_facts`)으로만 넣는다. 출처는 로컬 공개 회사 소개 소스. 로그에는 끝 4자리만.
- 소유자 초기 비밀번호 파일: `~/.config/price/owner-initial-login.txt` (0600). 채팅·이슈에 붙이지 않는다.
- 조회만: `python3 -m price --db /home/jm/.local/share/price/company.sqlite3 status`
- 데모와 다른 포트로 띄울 때(데모를 끄지 않음):

```bash
python3 -m price --db /home/jm/.local/share/price/company.sqlite3 serve --host 127.0.0.1 --port 4382
```

공개 바인드 금지. 거래 적재·신고·납부는 이 단계에 없다.

백업: `python3 scripts/company_db_backup.py` (앱 `backup` = sqlite backup, 14개, 0600). 사용자 타이머 `price-company-backup.timer`. 복원은 새 파일에만 `python3 -m price restore <backup> <newfile> --sha256 <hash>`.
