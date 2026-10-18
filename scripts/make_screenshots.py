#!/usr/bin/env python3
"""UI previews from the real frontend source using synthetic demo data (no live accounts)."""
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parent.parent
html=(root/'app/static/index.html').read_text().replace('<link rel="stylesheet" href="/static/style.css">','').replace('<script src="/static/app.js" defer></script>','')
css=(root/'app/static/style.css').read_text()
js=(root/'app/static/app.js').read_text().replace("const DEMO = window.location.protocol === 'file:' || window.location.pathname.endsWith('/preview-local.html');",'const DEMO = true;')
out=root/'docs/images';out.mkdir(parents=True,exist_ok=True)
with sync_playwright() as p:
  browser=p.chromium.launch(headless=True,executable_path='/usr/bin/chromium',args=['--no-sandbox','--disable-gpu','--disable-dev-shm-usage'])
  page=browser.new_page(viewport={'width':1440,'height':930},device_scale_factor=1)
  page.set_content(html,wait_until='domcontentloaded')
  page.add_style_tag(content=css)
  page.add_script_tag(content=js)
  page.wait_for_timeout(300)
  for tab,name in [('dashboard','dashboard.png'),('connections','connections.png'),('sync','synchronization.png')]:
    page.locator(f'[data-tab="{tab}"]').click()
    page.screenshot(path=str(out/name),full_page=True,animations='disabled')
  mobile=browser.new_page(viewport={'width':390,'height':844},device_scale_factor=1,is_mobile=True,has_touch=True)
  mobile.set_content(html,wait_until='domcontentloaded')
  mobile.add_style_tag(content=css)
  mobile.add_script_tag(content=js)
  mobile.wait_for_timeout(300)
  mobile.screenshot(path=str(out/'mobile.png'),full_page=True,animations='disabled')
  browser.close()
print('Created',*[str(p.relative_to(root)) for p in sorted(out.glob('*.png'))],sep='\n - ')
