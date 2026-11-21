const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({headless:true,channel:'chrome'});
  try {
    const page = await browser.newPage({httpCredentials:{username:'ci',password:'ci-test-password'}});
    const commands=[];
    await page.route('**/api/command', async route => {
      commands.push(route.request().postDataJSON());
      await route.fulfill({status:202,contentType:'application/json',body:'{"accepted":true}'});
    });
    await page.goto('http://127.0.0.1:8080/');
    const add=page.locator('#add');
    await add.getByLabel('Label',{exact:true}).fill('Media Wii U');
    await add.getByLabel('Bluetooth address',{exact:false}).fill('40:D2:8A:43:4E:C8');
    await add.getByRole('button',{name:'Add console',exact:true}).click();
    const wii=page.locator('article').filter({has:page.getByRole('heading',{name:'Media Wii U · Default'})});
    for (const [label, action] of [['Pair console','pair'],['Power on','on'],['Power off','off']]) {
      await wii.getByRole('button',{name:label,exact:true}).click();
      await page.waitForFunction(() => document.querySelector('#notice').textContent === 'Command accepted');
      const sent=commands.at(-1);
      if (sent.command!==action || !sent.console_id) throw Error(`Button ${label} sent wrong target/action`);
      await page.evaluate(()=>document.querySelector('#notice').textContent='');
    }
    await add.getByLabel('Label',{exact:true}).fill('Living room Switch');
    await add.getByLabel('Console type').selectOption('switch');
    await add.getByRole('button',{name:'Add console',exact:true}).click();
    const sw=page.locator('article').filter({has:page.getByRole('heading',{name:'Living room Switch',exact:true})});
    await sw.getByRole('button',{name:'HOME / Wake',exact:true}).click();
    await page.waitForFunction(() => document.querySelector('#notice').textContent === 'Command accepted');
    if(commands.at(-1).console_id===commands[0].console_id)throw Error('Console target did not change');
    if(await sw.getByRole('button',{name:'Power off',exact:true}).count())throw Error('Switch has unsupported power off button');
    console.log('Console creation and targeted Wii/Switch buttons passed');
  } finally { await browser.close(); }
})().catch(err=>{console.error(err);process.exit(1)});
