import {chromium} from 'playwright';

const BASE='http://127.0.0.1:18245';
const browser=await chromium.launch();
const page=await browser.newPage({viewport:{width:1440,height:1000}});
const errors=[];
page.on('pageerror',e=>errors.push(String(e)));
page.on('console',m=>{if(m.type()==='error')errors.push(m.text())});
async function shot(name){await page.screenshot({path:`/workspace/test-artifacts/gb-0041/screens/${name}.png`,fullPage:true})}

await page.goto(BASE+'/#login');
await page.fill('input[name=email]','admin@acoustic-bayes.demo');
await page.fill('input[name=password]','Research2026!');
await page.click('button[type=submit]');
await page.waitForSelector('.masthead');
await shot('01-overview');

await page.goto(BASE+'/#source');
await page.waitForSelector('#record-form');
await page.selectOption('#source-select',{index:0});
await page.waitForTimeout(600);
const badge=await page.textContent('.file-note');
console.log('source badge:',badge.replace(/\s+/g,' ').trim());

await page.goto(BASE+'/#schema');
await page.waitForSelector('#migration-form');
await shot('02-schema-before');
const sourceId=await page.$eval('#migration-form select[name=source_id]',el=>el.value);
await page.click('#migration-form button[type=submit]');
await page.waitForSelector('.migration-card');
await page.waitForFunction(()=>/可切换|已阻塞/.test(document.querySelector('.migration-card')?.textContent||''),null,{timeout:30000});
console.log('card state:',(await page.textContent('.migration-card')).replace(/\s+/g,' ').slice(0,220));
await shot('03-schema-ready');

// new-version sample during rollover from the source page
await page.goto(BASE+'/#source');
await page.waitForSelector('#record-form');
await page.fill('input[name=reference]','TX-UI-001');
const inputs=await page.$$('#record-form input[name=rms],#record-form input[name=centroid],#record-form input[name=kurtosis],#record-form input[name=label]');
const values=['1.9','1.31','3.1','正常'];
for(let i=0;i<inputs.length;i++)await inputs[i].fill(values[i]);
await page.click('#record-form button[type=submit]');
await page.waitForTimeout(1200);
const notice=await page.textContent('#notice');
console.log('ingest notice:',notice);

// a study cannot be frozen on a mixed batch without an explicit version
await page.goto(BASE+'/#config');
await page.waitForSelector('#run-form');
await page.selectOption('select[name=source_id]',sourceId);
await page.waitForTimeout(400);
await page.fill('input[name=name]','界面混版研究');
await page.click('#run-form button[type=submit]');
await page.waitForTimeout(900);
console.log('mixed study blocked:',await page.textContent('#notice'));
await shot('04-config-mixed-blocked');
await page.selectOption('select[name=schema_version]','v1');
await page.fill('input[name=name]','界面旧口径研究');
await page.click('#run-form button[type=submit]');
await page.waitForURL(/#results/,{timeout:10000});
await page.waitForTimeout(1500);
console.log('pinned study:',(await page.textContent('.run-title')).replace(/\s+/g,' ').slice(0,120));
await shot('05-results-pinned-v1');

await page.goto(BASE+'/#schema');
await page.waitForSelector('.migration-card');
await page.waitForFunction(()=>/可切换|已阻塞/.test(document.querySelector('.migration-card')?.textContent||''),null,{timeout:30000});
const button=await page.$('[data-switch]');
console.log('switch enabled:',button?await button.isEnabled():false);
page.once('dialog',d=>d.accept());
await button.click();
await page.waitForTimeout(2500);
console.log('after switch:',(await page.textContent('.migration-card')).replace(/\s+/g,' ').slice(0,180));
await shot('06-schema-switched');

await page.goto(BASE+'/#source');
await page.waitForSelector('#record-form');
await page.waitForTimeout(700);
console.log('source badge after:',(await page.textContent('.file-note')).replace(/\s+/g,' ').trim());
await shot('07-source-after');

await page.setViewportSize({width:390,height:900});
await page.goto(BASE+'/#schema');
await page.waitForSelector('.migration-card');
await shot('08-schema-mobile');

console.log('page errors:',errors);
await browser.close();
