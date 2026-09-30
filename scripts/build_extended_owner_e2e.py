"""Extend the existing owner test with actual XLSX and Korean PDF fixtures."""
from pathlib import Path
root=Path(__file__).resolve().parents[1]
source=(root/'scripts/e2e_owner.mjs').read_text(encoding='utf-8')
changes=[
(" const db=path.join(temp,'source.sqlite3');", " const db=path.join(temp,'source.sqlite3');"),
(" await cdp('Page.navigate',{url});await wait(()=>evaluate('!!document.querySelector(\"#owner-login-form\")'),'owner login');",
''' const registration=path.join(temp,'사업자등록증_합성.pdf');
 const frame=await cdp('Page.getFrameTree');
 await cdp('Page.setDocumentContent',{frameId:frame.frameTree.frame.id,html:'<!doctype html><html lang="ko"><head><meta charset="utf-8"></head><body style="font:18px sans-serif;padding:40px"><h1>사업자등록증</h1><p>법인명(단체명): 주식회사 합성테스트</p><p>등록번호 : 111-11-11111</p><p>SYNTHETIC TEST DATA ONLY. NOT AN OFFICIAL CERTIFICATE.</p></body></html>'});
 await pause(200);const printed=await cdp('Page.printToPDF',{printBackground:true,displayHeaderFooter:false});fs.writeFileSync(registration,Buffer.from(printed.data,'base64'));
 const workbook=path.join(temp,'통장_XLSX_합성.xlsx');
 execFileSync('python3',['-c','import sys;from pathlib import Path;sys.path.insert(0,"tests");from test_source_workbooks import make_workbook,BANK_ROWS;Path(sys.argv[1]).write_bytes(make_workbook([("은행내역",BANK_ROWS)]))',workbook],{cwd:root,stdio:'ignore'});
 await cdp('Page.navigate',{url});await wait(()=>evaluate('!!document.querySelector("#owner-login-form")'),'owner login');'''),
(" await selectFiles([bank,card,invoice,bad]);", " await selectFiles([bank,card,invoice,bad,registration,workbook]);"),
("first.counts.documents===4", "first.counts.documents===6"),
("first.counts.reading===3", "first.counts.reading===5"),
("h.counts.read===3&&h.counts.source_cases===5", "h.counts.read===5&&h.counts.source_cases===7"),
(" await selectFiles([invoice,card,bank,bad]);", " await selectFiles([workbook,registration,invoice,card,bank,bad]);"),
("duplicate.counts.documents===4&&duplicate.counts.source_cases===5", "duplicate.counts.documents===6&&duplicate.counts.source_cases===7"),
(" await click('#company-facts-form [name=\"confirmed\"]');", " check('Korean PDF text-layer populates company facts without manual entry',await evaluate('document.querySelector(\"#company-facts-form [name=business_number]\").value===\"1111111111\" && document.querySelector(\"#company-facts-form [name=name]\").value===\"주식회사 합성테스트\"'));\n await click('#company-facts-form [name=\"confirmed\"]');"),
("report:'evidence/version-loop/V01/owner-browser-report.json'", "report:'evidence/version-loop/V01/owner-extended-browser-report.json'"),
("path.join(out,'owner-browser-report.json')", "path.join(out,'owner-extended-browser-report.json')")
]
for old,new in changes:
    if source.count(old)!=1: raise RuntimeError('Expected browser test block not found: '+old[:70])
    source=source.replace(old,new,1)
target=root/'scripts/e2e_owner_extended.mjs'
with target.open('x',encoding='utf-8') as file:file.write(source)
print('Extended owner browser suite generated; original suite retained.')
