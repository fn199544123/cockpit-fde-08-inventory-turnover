"""Standalone business demo backend: SQLite transactions, validation and audit.

Distributed as backend.py in each development project; Python 3.10+, stdlib only.
The process listens on loopback. The publisher gateway supplies user access control.
"""
import argparse
import copy
import json
import math
import os
import re
import sqlite3
import time
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


class Invalid(ValueError):
    pass


def need(condition, message):
    if not condition:
        raise Invalid(message)


def number(value, minimum=0, maximum=1e12, integer=False):
    need(type(value) in (int, float) and math.isfinite(value)
         and minimum <= value <= maximum and (not integer or int(value) == value), '数值超出范围或格式不正确')
    return value


def text(value, maximum=100):
    need(isinstance(value, str) and 0 < len(value.strip()) <= maximum, '文字字段为空或过长')


def day(value):
    need(isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value), '日期格式错误')
    try:
        date.fromisoformat(value)
    except ValueError:
        raise Invalid('日期无效')


def rows(data, key=None, identity='id'):
    items = data if key is None else data.get(key)
    need(isinstance(items, list) and len(items) <= 100000, '台账必须为记录列表')
    out = {}
    for row in items:
        need(isinstance(row, dict), '记录格式错误')
        value = row.get(identity)
        text(value, 100)
        need(value not in out, '编号重复')
        out[value] = row
    return out


def validate(n, data, old=None, context=None):
    """Validate client data independently; bypassing JavaScript cannot bypass rules."""
    need(isinstance(data, (dict, list)), '业务数据格式错误')
    if n in (6, 7, 8, 10, 12, 14):
        need(isinstance(data, dict), '业务数据必须为对象')
    if n == 6:
        ts = rows(data, 'tasks'); bs = rows(data, 'berths'); ys = rows(data, 'yards'); vs = rows(data, 'vehicles')
        previous = rows(old, 'tasks') if old else {}
        for collection in (ts, bs, ys, vs):
            for r in collection.values():
                need(re.fullmatch(r'[A-Z]{2,5}-\d{3,8}', r['id']), '请使用虚构编号')
                text(r.get('name'), 40)
        for y in ys.values():
            number(y.get('capacity'), 1, 1000000, True); number(y.get('used'), 0, y['capacity'], True)
        active = []
        for t in ts.values():
            number(t.get('qty'), 1, 1000000, True)
            need(t.get('yard') in ys, '货位不存在')
            try:
                start, end = datetime.fromisoformat(t['start']), datetime.fromisoformat(t['end'])
                need(start < end, '排班结束须晚于开始')
            except (KeyError, TypeError, ValueError):
                raise Invalid('排班时间无效')
            status = t.get('status')
            need(status in ('待派发', '已派发', '执行中', '已完成'), '作业状态无效')
            if status != '待派发':
                need(t.get('berth') in bs and t.get('vehicle') in vs, '泊位或车辆不存在')
            prior = previous.get(t['id'])
            if old:
                need(prior is not None or status == '待派发', '新增作业必须待派发')
            if prior:
                allowed = {'待派发': ('待派发', '已派发'), '已派发': ('已派发', '执行中'), '执行中': ('执行中', '已完成'), '已完成': ('已完成',)}
                need(status in allowed[prior['status']], '非法作业状态跳转')
                if prior['status'] != '待派发':
                    need(all(t.get(k) == prior.get(k) for k in ('qty', 'yard', 'start', 'end', 'berth', 'vehicle', 'name')), '已派发作业不可修改资源或数量')
            if status in ('已派发', '执行中'):
                active.append(t)
        assigned = [t for t in ts.values() if t['status'] != '待派发']
        for i, t in enumerate(assigned):
            for other in assigned[i + 1:]:
                same = t['berth'] == other['berth'] or t['vehicle'] == other['vehicle']
                need(not (same and t['start'] < other['end'] and other['start'] < t['end']), '排班时间重叠')
        for resource in ('berth', 'vehicle'):
            need(len({t[resource] for t in active}) == len(active), '资源重复占用')
        for y in ys.values():
            need(y['used'] + sum(t['qty'] for t in active if t['yard'] == y['id']) <= y['capacity'], '货位库存与预留超容量')
        if old:
            need(set(previous) <= set(ts), '作业历史不得删除')
            for y in ys.values():
                finished = sum(t['qty'] for t in ts.values() if t['yard'] == y['id'] and t['status'] == '已完成' and previous.get(t['id'], {}).get('status') == '执行中')
                if finished:
                    need(y['used'] == rows(old, 'yards')[y['id']]['used'] + finished, '完成作业必须联动入库')
    elif n == 7:
        ms = rows(data, 'materials'); orders = rows(data, 'orders')
        queue = data.get('queue')
        need(isinstance(queue, list) and all(isinstance(x, str) for x in queue) and len(set(queue)) == len(queue), '排产队列无效')
        need(set(queue) == {o['id'] for o in orders.values() if o.get('status') == 'scheduled'}, '排产状态与队列不一致')
        datetime.fromisoformat(data['start'])
        reserved = {k: 0 for k in ms}
        for m in ms.values():
            text(m.get('name')); number(m.get('stock'))
        for o in orders.values():
            number(o.get('qty'), 0.001); day(o.get('due')); text(o.get('product'))
            need(o.get('priority') in (1, 2, 3) and o.get('workshop') in (0, 1, 2) and o.get('status') in ('pending', 'scheduled'), '订单状态或优先级无效')
            need(isinstance(o.get('bom'), dict) and o['bom'], 'BOM 不能为空')
            for mid, amount in o['bom'].items():
                need(mid in ms, 'BOM 物料不存在'); number(amount, 0.001)
                if o['status'] == 'scheduled':
                    reserved[mid] += amount * o['qty']
        for mid, amount in reserved.items():
            need(amount <= ms[mid]['stock'] + 1e-6, '物料预留超过库存')
    elif n in (8, 12):
        batches = rows(data, 'batches'); logs = rows(data, 'logs')
        if n == 8:
            for k in ('near', 'stale', 'period'):
                number(data.get('settings', {}).get(k), 0 if k == 'near' else 1, 3650 if k == 'stale' else 365, True)
        barcodes = set()
        for b in batches.values():
            number(b.get('qty'), 0, 1000000000, True); day(b.get('expiry'))
            if n == 8:
                text(b.get('product')); day(b.get('entry')); need(b['entry'] <= b['expiry'], '到期日期早于入库')
            else:
                for key in ('barcode', 'name', 'place', 'unit'):
                    text(b.get(key))
                need(b['barcode'] not in barcodes, '条码重复'); barcodes.add(b['barcode']); number(b.get('min'), 0, 1000000, True)
        balances = {k: 0 for k in batches}
        for entry in logs.values():
            need(entry.get('batch') in batches, '库存流水引用不存在的批次')
            if n == 8:
                day(entry.get('date')); delta = number(entry.get('delta'), -1e9, 1e9, True)
            else:
                need(entry.get('type') in ('in', 'out'), '出入库类型无效')
                quantity = number(entry.get('qty'), 1, 1e9, True)
                delta = quantity if entry['type'] == 'in' else -quantity
                number(entry.get('before'), 0, 1e9, True); number(entry.get('after'), 0, 1e9, True)
                need(entry['after'] == entry['before'] + delta, '库存流水前后数量不平')
                need(entry['before'] == balances[entry['batch']], '出入库流水不连续')
            balances[entry['batch']] += delta
        for key, b in batches.items():
            need(abs(balances[key] - b['qty']) < 1e-6, '库存与出入库流水不一致')
        if old:
            oldlogs = rows(old, 'logs')
            for k, v in oldlogs.items():
                candidate = logs.get(k)
                # Inventory UI supports correcting registration date; keep all other history immutable.
                if n == 8 and v.get('type') == '初始入库' and candidate:
                    need({a: b for a, b in candidate.items() if a != 'date'} == {a: b for a, b in v.items() if a != 'date'}, '历史库存流水不可改写')
                    need(candidate['date'] == batches[v['batch']]['entry'], '初始入库日期须与批次一致')
                else:
                    need(candidate == v, '历史库存流水不可改写或删除')
            for k, entry in logs.items():
                if k not in oldlogs and (entry.get('delta', 0) < 0 or entry.get('type') == 'out'):
                    need(batches[entry['batch']]['expiry'] >= date.today().isoformat(), '过期批次禁止出库')
            if n == 8:
                withdrawals = [r for k, r in logs.items() if k not in oldlogs and r.get('type') == '出库']
                if withdrawals:
                    context = context or {}
                    product, room = context.get('product'), context.get('room', '')
                    need(isinstance(product, str) and room in ('', '一车间', '二车间', '三车间'), '缺少FIFO出库范围')
                    candidates = sorted((b for b in old['batches'] if b['product'] == product and (not room or b['room'] == room) and b['qty'] > 0 and b['expiry'] >= date.today().isoformat()), key=lambda b: (b['entry'], b['id']))
                    remaining = sum(-r['delta'] for r in withdrawals)
                    number(remaining, 1, 1000000, True)
                    expected = {}
                    for b in candidates:
                        amount = min(remaining, b['qty'])
                        if amount: expected[b['id']] = amount
                        remaining -= amount
                    actual = {}
                    for r in withdrawals:
                        need(r['delta'] < 0, '出库数量须为负变动')
                        actual[r['batch']] = actual.get(r['batch'], 0) - r['delta']
                    need(remaining == 0 and expected == actual, '出库必须按FIFO扣减且不可超过库存')
    elif n == 9:
        records = rows(data)
        for r in records.values():
            need(re.fullmatch(r'DEMO-[A-Z0-9-]{1,35}', r['id']), '批次编号无效')
            need(type(r.get('type')) is int and 0 <= r['type'] <= 3, '批次环节无效')
            text(r.get('product'), 40); number(r.get('quantity'), 0.001, 1000000); day(r.get('date')); text(r.get('location'))
            parents = r.get('parents')
            need(isinstance(parents, list) and all(isinstance(p, str) for p in parents) and len(set(parents)) == len(parents), '上游关系无效')
            need(bool(parents) == (r['type'] > 0), '缺少上游或原料不应有上游')
            for key in parents:
                need(key in records and records[key]['type'] == r['type'] - 1, '孤儿引用或跨环节关联')
                need(records[key]['date'] <= r['date'], '下游日期早于上游')
    elif n == 10:
        devices = rows(data, 'devices'); parts = rows(data, 'parts'); orders = rows(data, 'orders')
        pending = set()
        for d in devices.values():
            text(d.get('name')); need(d.get('shop') in ('一车间', '二车间', '三车间'), '车间无效')
            for k in ('hours', 'next', 'interval', 'tempMax', 'vibMax'):
                number(d.get(k), 0 if k in ('hours', 'next') else 0.001)
            need(isinstance(d.get('readings'), list), '采集记录无效')
            last = -1
            for r in d['readings']:
                number(r.get('hours')); number(r.get('temp'), -100, 1000); number(r.get('vib'))
                need(last <= r['hours'] <= d['hours'], '运行时长不得倒退'); last = r['hours']
        for part in parts.values():
            text(part.get('name')); number(part.get('stock'), 0, 1e9, True); number(part.get('min'), 0, 1e9, True)
        for order in orders.values():
            need(order.get('device') in devices and order.get('status') in ('待处理', '已完成'), '保养工单无效')
            if order['status'] == '待处理':
                need(order['device'] not in pending, '重复保养派单'); pending.add(order['device'])
        if old:
            prior = rows(old, 'orders'); oldparts = rows(old, 'parts')
            need(set(prior) <= set(orders), '工单历史不得删除')
            for k, o in prior.items():
                need(o['status'] != '已完成' or orders[k] == o, '已完成工单不可改写')
                if o['status'] == '待处理' and orders[k]['status'] == '已完成':
                    device = devices[o['device']]
                    need(device['next'] == device['hours'] + device['interval'], '完成保养须推进下次时长')
            oldlogs = old.get('logs', []); newlogs = data.get('logs', [])
            need(newlogs[:len(oldlogs)] == oldlogs, '备件历史流水不可改写')
            for k in oldparts:
                need(k in parts, '备件不可删除')
                delta = sum(number(log.get('delta'), -1e9, 1e9, True) for log in newlogs[len(oldlogs):] if log.get('part') == k)
                need(parts[k]['stock'] == oldparts[k]['stock'] + delta, '备件库存与流水不一致')
    elif n == 11:
        for a in rows(data, identity='code').values():
            need(re.fullmatch(r'XN-[A-Za-z0-9-]+', a['code']), '资产编号无效'); text(a.get('name'), 40)
            need(a.get('floor') in ('一楼', '二楼', '三楼') and a.get('category') in ('电子设备', '教学设备', '办公家具', '其他资产'), '资产分类或楼层无效')
            number(a.get('book'), 1, 999999, True)
            if a.get('actual') is not None:
                number(a['actual'], 0, 999999, True)
    elif n == 13:
        records = rows(data); prior = rows(old) if old else {}
        for o in records.values():
            need(re.fullmatch(r'WT-\d{4,12}', o['id']) and re.fullmatch(r'YP-\d{4,12}', o.get('sample', '')), '委托或样品编号无效')
            need(o.get('company') == '某企业' and o.get('category') in ('材料检测', '产品认证', '性能检测'), '企业或检测类别无效')
            number(o.get('stage'), 0, 4, True)
            need(o.get('result') in ('待判定', '通过', '不通过'), '检测结果无效')
            need(o['stage'] < 2 or o['result'] != '待判定', '请先判定检测结果')
            need(o['stage'] < 3 or o['result'] == '通过', '不通过不得发证')
            history = o.get('history'); need(isinstance(history, list) and len(history) == o['stage'] + 1, '流转历史不完整')
            last = 0
            for i, event in enumerate(history):
                need(event.get('stage') == i, '历史环节不连续'); number(event.get('at'), last, 1e15); last = event['at']
            if old:
                p = prior.get(o['id'])
                need(p is not None or o['stage'] == 0, '新委托须从收样开始')
                if p:
                    need(o['stage'] in (p['stage'], p['stage'] + 1) and history[:len(p['history'])] == p['history'], '流转须逐级且不得改写历史')
        if old:
            need(set(prior) <= set(records), '委托历史不可删除')
    elif n == 14:
        projects = rows(data, 'projects'); people = rows(data, 'people'); entries = rows(data, 'entries')
        for p in projects.values():
            text(p.get('name')); number(p.get('budget'), 0, 1e9)
        for p in people.values():
            need(isinstance(p.get('name'), str) and re.fullmatch(r'化名[\u4e00-\u9fffA-Za-z0-9]+', p['name']), '人员须使用化名'); number(p.get('rate'), 0, 1e6)
        totals = {}
        prior = rows(old, 'entries') if old else {}
        for e in entries.values():
            need(e.get('person') in people and e.get('project') in projects, '项目或人员不存在')
            day(e.get('date')); number(e.get('hours'), 0.01, 24); number(e.get('rate'), 0, 1e6)
            key = (e['person'], e['date']); totals[key] = totals.get(key, 0) + e['hours']
            need(totals[key] <= 24 + 1e-8, '同一人员单日累计工时超过 24 小时')
            if old:
                p = prior.get(e['id']); expected = p['rate'] if p and p['person'] == e['person'] else people[e['person']]['rate']
                need(e['rate'] == expected, '登记时薪不可任意改写')
    elif n == 15:
        for r in rows(data).values():
            need(re.fullmatch(r'XS-[A-Za-z0-9-]+', r['id']), '销售编号无效'); day(r.get('date'))
            need('2000-01-01' <= r['date'] <= '2099-12-31', '销售日期超范围')
            need(r.get('store') in ('一门店', '二门店', '三门店') and r.get('category') in ('食品饮料', '日用百货', '服饰配件'), '门店或品类无效')
            number(r.get('cents'), 1, 9999999999, True)
    else:
        raise Invalid('项目不受支持')


class Store:
    def __init__(self, root, database=None):
        self.root = Path(root)
        self.n = int(json.loads((self.root / 'backend-config.json').read_text())['project'])
        self.seed = json.loads((self.root / 'seed.json').read_text())
        validate(self.n, self.seed)
        self.path = Path(database or self.root / 'data' / 'business.sqlite3')
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.collections = (['records'] if isinstance(self.seed, list) else
                            [k for k, v in self.seed.items() if isinstance(v, list) and k != 'queue'])
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS metadata (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, fields TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS entities (collection TEXT NOT NULL, entity_id TEXT NOT NULL, position INTEGER NOT NULL, document TEXT NOT NULL CHECK(json_valid(document)), PRIMARY KEY(collection,entity_id));
                CREATE TABLE IF NOT EXISTS audit (revision INTEGER PRIMARY KEY, action TEXT NOT NULL, actor TEXT NOT NULL, at REAL NOT NULL, previous_state TEXT NOT NULL CHECK(json_valid(previous_state)));
                PRAGMA user_version=1;
            ''')
            if not db.execute('SELECT 1 FROM metadata').fetchone():
                db.execute('INSERT INTO metadata VALUES(1,0,\'{}\')')
                self._write(db, self.seed)
        os.chmod(self.path, 0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def _read(self, db):
        revision, raw = db.execute('SELECT revision,fields FROM metadata WHERE id=1').fetchone()
        data = json.loads(raw)
        for key in self.collections:
            data[key] = [json.loads(r[0]) for r in db.execute('SELECT document FROM entities WHERE collection=? ORDER BY position', (key,))]
        return (data['records'] if isinstance(self.seed, list) else data), revision

    def _write(self, db, data):
        source = {'records': data} if isinstance(data, list) else data
        need(set(source) == (set(self.seed) if isinstance(self.seed, dict) else {'records'}), '顶层字段与项目结构不一致')
        db.execute('DELETE FROM entities')
        for key in self.collections:
            for position, row in enumerate(source[key]):
                identity = row.get('id', row.get('code', str(position)))
                db.execute('INSERT INTO entities VALUES(?,?,?,?)', (key, identity, position, json.dumps(row, ensure_ascii=False, allow_nan=False)))
        db.execute('UPDATE metadata SET fields=? WHERE id=1', (json.dumps({k: v for k, v in source.items() if k not in self.collections}, ensure_ascii=False, allow_nan=False),))

    def read(self):
        with self.connect() as db:
            db.execute('BEGIN')
            data, revision = self._read(db)
            return {'data': data, 'revision': revision, 'project': self.n, 'seed': self.seed}

    def save(self, payload, actor='local'):
        need(isinstance(payload, dict) and type(payload.get('revision')) is int, '缺少数据版本')
        action = payload.get('action', 'save')
        need(action in ('save', 'reset'), '操作不受支持')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old, revision = self._read(db)
            if payload['revision'] != revision:
                return {'error': '数据已被其他窗口更新。请刷新后重新操作，未覆盖任何数据。'}, 409
            data = copy.deepcopy(self.seed) if action == 'reset' else payload.get('data')
            validate(self.n, data, None if action == 'reset' else old, payload.get('context'))
            self._write(db, data)
            revision += 1
            db.execute('UPDATE metadata SET revision=? WHERE id=1', (revision,))
            db.execute('INSERT INTO audit VALUES(?,?,?,?,?)', (revision, action, actor, time.time(), json.dumps(old, ensure_ascii=False)))
            return {'ok': True, 'revision': revision}, 200


def handler(store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Do not log request bodies, credentials or user-entered data.

        def reply(self, status, body, kind='application/json; charset=utf-8'):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in ('/', '/index.html'):
                return self.reply(200, (store.root / 'index.html').read_bytes(), 'text/html; charset=utf-8')
            if path == '/api/state':
                return self.reply(200, store.read())
            if path == '/api/health':
                with store.connect() as db:
                    ok = db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
                return self.reply(200 if ok else 503, {'ok': ok, 'project': store.n, 'database': 'sqlite', 'schema': 1})
            return self.reply(404, {'error': '资源不存在'})

        def do_POST(self):
            if urlsplit(self.path).path != '/api/state':
                return self.reply(404, {'error': '接口不存在'})
            origin = self.headers.get('Origin')
            if origin and urlsplit(origin).netloc != self.headers.get('Host'):
                return self.reply(403, {'error': '不允许跨站写入'})
            if self.headers.get('X-Requested-With') != 'XMLHttpRequest' or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                return self.reply(403, {'error': '必须通过应用接口提交'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                need(0 < length <= 8 * 1024 * 1024, '请求大小无效')
                payload = json.loads(self.rfile.read(length), parse_constant=lambda _: (_ for _ in ()).throw(Invalid('不允许非有限数值')))
                result, status = store.save(payload, self.headers.get('X-Demo-Actor', 'local')[:100])
                self.reply(status, result)
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as e:
                self.reply(422, {'error': str(e) if isinstance(e, Invalid) else '业务字段格式不正确'})
            except sqlite3.Error:
                self.reply(503, {'error': '数据库暂不可写，本次修改未保存'})
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--database')
    args = parser.parse_args()
    os.umask(0o077)
    store = Store(Path(__file__).resolve().parent, args.database)
    ThreadingHTTPServer(('127.0.0.1', args.port), handler(store)).serve_forever()


if __name__ == '__main__':
    main()
