"""Keep all existing expert acceptance assertions at the explicit expert route."""
from pathlib import Path
root = Path(__file__).resolve().parents[1]
path = root / 'scripts' / 'e2e_browser.mjs'
source = path.read_text(encoding='utf-8')
before = " await cdp('Page.navigate',{url});await wait(()=>evaluate('!!document.querySelector(\"#login-form\")'),'app login document');"
after = " await cdp('Page.navigate',{url:url+'/expert'});await wait(()=>evaluate('!!document.querySelector(\"#login-form\")'),'expert login document');"
if source.count(before) != 1:
    raise RuntimeError('Expected route not found; script not modified')
path.write_text(source.replace(before, after, 1), encoding='utf-8')
print('Legacy expert assertions preserved; only initial URL changed to /expert.')
