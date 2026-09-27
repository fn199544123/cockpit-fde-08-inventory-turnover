#!/usr/bin/env python3
"""在 GPU 宿主机执行：python3 smoke-test.py [--url http://127.0.0.1:18808/]。"""
import argparse, csv, hashlib, io, json, threading, functools
from pathlib import Path
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
KEY = 'development-8-inventory-v1'
results = []
def check(name, condition):
    if not condition: raise AssertionError(name)
    results.append({'name':name, 'passed':True})

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--url'); args=parser.parse_args()
    server=None
    if not args.url:
        server=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(SimpleHTTPRequestHandler,directory=str(ROOT)))
        threading.Thread(target=server.serve_forever,daemon=True).start()
    url=args.url or f'http://127.0.0.1:{server.server_port}/'
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
        ctx=browser.new_context(viewport={'width':1440,'height':1100},accept_downloads=True)
        page=ctx.new_page(); errors=[]; external=[]
        page.on('pageerror', lambda e:errors.append(str(e)))
        page.on('request',lambda r:external.append(r.url) if not r.url.startswith(url) else None)
        response=page.goto(url); check('系统 HTTP 200',response.status==200)
        def state(): return page.evaluate('(key)=>JSON.parse(localStorage.getItem(key))',KEY)
        def quantity(bid): return next(b['qty'] for b in state()['batches'] if b['id']==bid)
        def reset(accept=True):
            page.once('dialog',lambda d:d.accept() if accept else d.dismiss());page.click('#reset')
        def search(s):page.fill('#search',s)
        check('演示标识、身份、初始六批次',page.locator('body').inner_text().count('演示数据·仅当前浏览器保存')==1 and len(state()['batches'])==6 and 'douyin' in page.locator('body').inner_text())
        check('初始库存与流水守恒',sum(b['qty'] for b in state()['batches'])==sum(l['delta'] for l in state()['logs'])==1140)
        page.select_option('#shipProduct','速冻水饺')
        fifo=page.locator('#fifoList').inner_text()
        check('FIFO 排除过期批次并按入库时间排序','DEMO-FZ-001' not in fifo and fifo.index('DEMO-FZ-002')<fifo.index('DEMO-FZ-003'))
        before=state();page.fill('#shipQty','999999');page.click('#shipForm button')
        check('超库存整单拒绝',state()==before and '库存不足' in page.locator('#shipError').inner_text())
        page.fill('#shipQty','-1');page.click('#shipForm button');check('负数出库拒绝',state()==before)
        page.fill('#shipQty','0');page.click('#shipForm button');check('零出库拒绝',state()==before)
        page.fill('#shipQty','1.5');page.click('#shipForm button');check('非整箱出库拒绝',state()==before)
        page.fill('#shipQty','270');page.click('#shipForm button')
        check('FIFO 跨批扣减 240+30 且过期库存不变',quantity('DEMO-FZ-002')==0 and quantity('DEMO-FZ-003')==270 and quantity('DEMO-FZ-001')==160)
        check('出库两条流水',[(l['batch'],l['delta']) for l in state()['logs'][-2:]]==[('DEMO-FZ-002',-240),('DEMO-FZ-003',-30)])
        page.reload();check('刷新后库存持久化',quantity('DEMO-FZ-003')==270)
        reset();page.select_option('#statusFilter','near');check('临期筛选',page.locator('#batchBody tr').count()==2 and 'DEMO-FZ-001' not in page.locator('#batchBody').inner_text())
        page.select_option('#statusFilter','stale');check('呆滞筛选',page.locator('#batchBody tr').count()==4)
        page.select_option('#statusFilter','expired');check('过期筛选',page.locator('#batchBody tr').count()==1 and 'DEMO-FZ-001' in page.locator('#batchBody').inner_text())
        page.select_option('#statusFilter','');page.select_option('#roomFilter','一车间');page.select_option('#productFilter','速冻水饺');search('002')
        check('组合筛选',page.locator('#batchBody tr').count()==1 and page.locator('#stockKpi').inner_text()=='240')
        with page.expect_download() as di:page.click('#exportB')
        rows=list(csv.reader(io.StringIO(Path(di.value.path()).read_text(encoding='utf-8-sig'))))
        check('台账 CSV 与筛选一致',len(rows)==2 and rows[1][1]=='DEMO-FZ-002')
        reset();page.click('#add')
        values={'bId':'DEMO-TEST-001','bProduct':'测试速冻食品','bLocation':'T-01','bEntry':page.evaluate('today()'),'bExpiry':page.evaluate('ago(-10)'),'bQty':'10'}
        for k,v in values.items():page.fill('#'+k,v)
        page.click('#batchForm button.primary')
        check('新增批次与初始入库流水',quantity('DEMO-TEST-001')==10 and state()['logs'][-1]['delta']==10)
        search('DEMO-TEST-001');page.locator('[data-edit="DEMO-TEST-001"]').click();page.fill('#bQty','-1');page.fill('#bReason','盘点修正');page.click('#batchForm button.primary');check('负库存编辑被拒绝',quantity('DEMO-TEST-001')==10)
        page.fill('#bQty','15');page.fill('#bLocation','T-02');page.click('#batchForm button.primary');check('编辑库存修正记录差额 5',quantity('DEMO-TEST-001')==15 and state()['logs'][-1]['delta']==5 and 'T-02' in page.locator('#batchBody').inner_text())
        page.locator('[data-in="DEMO-TEST-001"]').click();page.fill('#inQty','7');page.click('#inForm button.primary');check('补充入库真实增加',quantity('DEMO-TEST-001')==22 and state()['logs'][-1]['type']=='入库')
        page.select_option('#shipProduct','测试速冻食品');page.fill('#shipQty','2');page.click('#shipForm button');check('新批次出库',quantity('DEMO-TEST-001')==20)
        page.locator('details summary').click();page.fill('#periodDays','1');page.fill('#nearDays','5');page.fill('#staleDays','1');page.click('#settingsForm button')
        check('周转公式：2 / 当日结存20 = 0.10',page.locator('#turnKpi').inner_text()=='0.10' and '日均 20.00' in page.locator('#turnHint').inner_text())
        check('预警参数修改立即生效',page.locator('#nearKpi').inner_text()=='0')
        with page.expect_download() as di:page.click('#exportL')
        rows=list(csv.reader(io.StringIO(Path(di.value.path()).read_text(encoding='utf-8-sig'))));check('流水 CSV 包含入库、调整、出库',len(rows)==5 and {r[3] for r in rows[1:]}=={'初始入库','库存调整','入库','出库'})
        search('不存在');check('无数据周转率为横线',page.locator('#turnKpi').inner_text()=='—' and '无有效基数' in page.locator('#turnHint').inner_text())
        search('DEMO-TEST-001');page.reload();check('编辑与参数刷新保存',quantity('DEMO-TEST-001')==20 and state()['settings']['period']==1)
        snapshot=state();reset(False);check('取消重置保留数据',state()==snapshot)
        reset();check('确认重置恢复六批次与参数',len(state()['batches'])==6 and state()['settings']['period']==30)
        page.click('#add')
        for k,v in {**values,'bId':'DEMO-FZ-001'}.items():page.fill('#'+k,v)
        page.click('#batchForm button.primary');check('重复编号拒绝','重复' in page.locator('#formError').inner_text() and len(state()['batches'])==6)
        page.fill('#bId','DEMO-TEST-002');page.fill('#bExpiry','2000-01-01');page.click('#batchForm button.primary');check('无效到期范围拒绝','到期日期' in page.locator('#formError').inner_text())
        page.click('#cancelBatch');page.locator('[data-in="DEMO-FZ-001"]').click();page.fill('#inQty','1');page.click('#inForm button.primary');check('过期批次禁止补充入库','过期' in page.locator('#inError').inner_text());page.click('#cancelIn')
        check('全批次账实数量守恒',all(b['qty']==sum(l['delta'] for l in state()['logs'] if l['batch']==b['id']) for b in state()['batches']))
        page.click('#add')
        for k,v in {**values,'bId':'DEMO-BOUNDARY-001','bProduct':'边界测试食品','bExpiry':page.evaluate('today()'),'bQty':'3'}.items():page.fill('#'+k,v)
        page.click('#batchForm button.primary');page.select_option('#shipProduct','边界测试食品')
        check('到期当天仍属于临期且允许推荐','DEMO-BOUNDARY-001' in page.locator('#fifoList').inner_text() and '临期 0 天' in page.locator('#fifoList').inner_text())
        page.fill('#shipQty','3');page.click('#shipForm button');check('整批清空并从推荐移除',quantity('DEMO-BOUNDARY-001')==0 and '没有可出库' in page.locator('#fifoList').inner_text())
        page.select_option('#shipProduct','速冻水饺');page.select_option('#shipRoom','三车间');snapshot=state();page.fill('#shipQty','1');page.click('#shipForm button');check('限定车间无库存时拒绝出库',state()==snapshot and '仅有 0 箱' in page.locator('#shipError').inner_text())
        page.select_option('#shipRoom','')
        page.locator('[data-edit="DEMO-FZ-003"]').click();page.fill('#bEntry',page.evaluate('today()'));page.fill('#bReason','日期边界测试');page.click('#batchForm button.primary');check('入库日期不能晚于历史出库流水','已有流水日期' in page.locator('#formError').inner_text());page.click('#cancelBatch')
        snapshot=state();page.evaluate("() => {window.originalSetItem=Storage.prototype.setItem;Storage.prototype.setItem=function(){throw new Error('test quota')}}")
        page.fill('#shipQty','1');page.click('#shipForm button');check('存储失败不扣库存且提示错误',state()==snapshot and '保存失败' in page.locator('#shipError').inner_text());page.evaluate('() => {Storage.prototype.setItem=window.originalSetItem}')
        reset();search('DEMO-FZ-002');check('有库存无出库显示零周转率',page.locator('#turnKpi').inner_text()=='0.00');search('')
        page.locator('#notice').wait_for(state='hidden')
        page.screenshot(path=str(ROOT/'acceptance-desktop.png'),full_page=True)
        page.set_viewport_size({'width':390,'height':844});check('390px 手机无页面横向溢出',page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        page.click('#add');check('手机弹窗不溢出',page.locator('#batchDialog').bounding_box()['width']<=390);page.click('#cancelBatch')
        page.screenshot(path=str(ROOT/'acceptance-mobile.png'),full_page=True)
        check('浏览器无 JS 错误',not errors);check('页面没有外部请求',not external)
        check('仅使用规定前缀存储',page.evaluate('Object.keys(localStorage).every(k=>k.startsWith("development-8-"))'))
        browser.close()
    report={'status':'passed','tested_at':datetime.now(timezone.utc).isoformat(),'url':url,'runtime':'GPU 宿主机 fangnangpu / Playwright Chromium','html_sha256':hashlib.sha256((ROOT/'index.html').read_bytes()).hexdigest(),'tests':results,'screenshots':['acceptance-desktop.png','acceptance-mobile.png']}
    (ROOT/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'status':'passed','checks':len(results),'url':url},ensure_ascii=False))
    if server:server.shutdown()
if __name__=='__main__':main()
