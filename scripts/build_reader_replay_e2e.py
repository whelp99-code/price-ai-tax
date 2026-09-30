"""Extend the existing isolated owner browser harness without modifying it."""
from pathlib import Path
root=Path(__file__).resolve().parents[1]
source=(root/'scripts/e2e_owner_extended.mjs').read_text()
source=source.replace("'evidence/version-loop/V01'","'evidence/version-loop/reader-replay'")
source=source.replace('evidence/version-loop/V01/','evidence/version-loop/reader-replay/')
source=source.replace('owner-extended-browser-report.json','reader-replay-browser-report.json')
anchor=" await cdp('Page.navigate',{url});"
assert source.count(anchor)==1
setup=r'''
 const xml=path.join(temp,'전자세금계산서_합성.xml'),html=path.join(temp,'통장_HTML_합성.xls'),tablePdf=path.join(temp,'통장_PDF_합성.pdf');
 execFileSync('python3',['-c','import sys;from pathlib import Path;sys.path.insert(0,"tests");from test_source_xml import invoice_xml;from test_source_html import HTML;Path(sys.argv[1]).write_bytes(invoice_xml());Path(sys.argv[2]).write_bytes(HTML)',xml,html],{cwd:root,stdio:'ignore'});
 const bankText='SYNTHETIC PDF BANK TABLE\n계좌번호: 123-456-789012\n통화: KRW\n거래일시    거래내용    입금액    출금액    거래후잔액\n2026-09-01 10:00:00    용역대금    1100    0    1100\n2026-09-02 10:00:00    장비지급    0    220    880\n합계    -    1100    220    -\n';
 await cdp('Page.setDocumentContent',{frameId:frame.frameTree.frame.id,html:'<!doctype html><html lang="ko"><meta charset="utf-8"><style>body{font:12px sans-serif}td,th{text-align:left;padding:10px 20px;white-space:nowrap}td:first-child,th:first-child{min-width:145px}</style><body><p>SYNTHETIC BANK TABLE</p><p>계좌번호: 123-456-789012</p><p>통화: KRW</p><table><tr><th>거래일시</th><th>거래내용</th><th>입금액</th><th>출금액</th><th>거래후잔액</th></tr><tr><td>2026-09-01 10:00:00</td><td>용역대금</td><td>1100</td><td>0</td><td>1100</td></tr><tr><td>2026-09-02 10:00:00</td><td>장비지급</td><td>0</td><td>220</td><td>880</td></tr><tr><td>합계</td><td>-</td><td>1100</td><td>220</td><td>-</td></tr></table></body></html>'});
 await pause(150);await screen('synthetic-pdf-table-source.png');
 const bankPrinted=await cdp('Page.printToPDF',{printBackground:true,displayHeaderFooter:false});fs.writeFileSync(tablePdf,Buffer.from(bankPrinted.data,'base64'));
'''
source=source.replace(anchor,setup+anchor)
anchor=" check('no uncaught JavaScript errors',exceptions.length===0);"
assert source.count(anchor)==1
work=r'''
 await cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
 await click('[data-view="upload"]');await selectFiles([xml,html,tablePdf]);
 await wait(()=>evaluate('document.querySelector("#transfer-label").textContent==="자료 접수 완료"'),'XML HTML PDF batch accepted');
 await wait(async()=>{const h=await home();return h.counts.read===8;},'XML HTML and PDF native readers',25000);
 const formats=await home();check('XML HTML-XLS and generated PDF tables enter the real worker',formats.counts.documents===9&&formats.counts.read===8);
 const pdfDoc=formats.documents.find(d=>d.filename==='통장_PDF_합성.pdf');
 const pdfRows=await evaluate(`request(prefix('/documents/${pdfDoc.id}/extraction'))`);
 check('native PDF table amounts and page provenance verified',pdfRows.total===2&&pdfRows.rows[0].values.gross==='1100'&&pdfRows.rows[0].fields.date.page===1);
 const bankDoc=formats.documents.find(d=>d.filename==='통장내역.csv');
 const priorProfile=await evaluate("request(prefix('/owner/onboarding'))");
 const originalInstruments=priorProfile.facts.instruments;
 const confirmProfile=async instruments=>evaluate(`(async()=>{const c=await request(prefix('/owner/onboarding'));return post('/owner/onboarding',{expected_revision:c.revision,name:c.facts.name,business_number:c.facts.business_number,mode:c.mode,instruments:${q(instruments)},confirmed:true});})()`);
 await confirmProfile([{kind:'bank',identifier:'999999999999',currency:'KRW'}]);
 check('wrong company facts hold previously read documents',(await home()).documents.find(d=>d.id===bankDoc.id).state==='COMPANY_REVIEW');
 await confirmProfile(originalInstruments);await click('#refresh-documents');
 await wait(()=>evaluate(`[...document.querySelectorAll('[data-document="${bankDoc.id}"] button')].some(b=>b.textContent==='다시 읽기')`),'held document retry action visible');
 await evaluate(`(()=>{const e=[...document.querySelectorAll('[data-document="${bankDoc.id}"] button')].find(b=>b.textContent==='다시 읽기');if(!e)throw Error('retry button missing');e.id='retry-under-test';})()`);
 await click('#retry-under-test');
 await wait(async()=>{const h=await home();return h.documents.find(d=>d.id===bankDoc.id).state==='NORMALIZED';},'owner retry finishes with corrected facts');
 const replay=await evaluate(`request(prefix('/documents/${bankDoc.id}/extraction/history'))`);
 check('actual owner retry stores two immutable extraction generations',replay.runs.length===2&&replay.runs[1].generation===2);
 check('unchanged source replay records no fabricated value changes',replay.runs[1].value_changes.length===0);
 await click('#refresh-documents');await click(`[data-document="${bankDoc.id}"] button`);
 await wait(()=>evaluate('document.querySelector("#owner-dialog-content").textContent.includes("다시 읽기 이력")'),'read history shown');
 check('owner sees replay history without accounting input',await evaluate('document.querySelector("#owner-dialog-content").textContent.includes("2회")'));
 await screen('reader-history-desktop.png');await click('#close-owner-dialog');
'''
source=source.replace(anchor,work+anchor)
target=root/'scripts/e2e_reader_replay.mjs'
with target.open('x') as out:out.write(source)
print(str(target.relative_to(root)))
