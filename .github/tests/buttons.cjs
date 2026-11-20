const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage({httpCredentials:{username:'ci',password:'ci-test-password'}});
    const commands=[];
    await page.route('**/api/command', async route => {
      commands.push(route.request().postDataJSON().command);
      await route.fulfill({status:202,contentType:'application/json',body:'{"accepted":true}'});
    });
    await page.goto('http://127.0.0.1:8080/');
    for (const [label, action] of [['Pair console','pair'],['Power on','on'],['Power off','off']]) {
      await page.getByRole('button',{name:label,exact:true}).click();
      await page.waitForFunction(() => document.querySelector('#notice').textContent === 'Command accepted');
      if (commands.at(-1)!==action) throw Error(`Button ${label} did not send ${action}`);
      await page.evaluate(()=>document.querySelector('#notice').textContent='');
    }
    console.log('All three buttons sent the correct commands');
  } finally { await browser.close(); }
})().catch(err=>{console.error(err);process.exit(1)});
