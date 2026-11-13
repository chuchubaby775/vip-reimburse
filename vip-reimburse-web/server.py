#!/usr/bin/env python3
"""Private, single-event VIP reimbursement collection server."""
from __future__ import annotations
import argparse
import base64
from collections import defaultdict, deque
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit
import zipfile
from ocr import capabilities, recognize

ROOT = Path(__file__).resolve().parent
MAX_FILE = 15 * 1024 * 1024
MAX_FILES = 12
DRAFT_LIFETIME = 30 * 24 * 3600
DEFAULT_INVITE_CODE = '01AI2026VIP'
CATEGORIES = [{'id':'rail','label':'高铁 / 火车'}, {'id':'flight','label':'机票'}, {'id':'taxi','label':'打车 / 市内交通'}, {'id':'hotel','label':'住宿'}, {'id':'other','label':'其他费用'}]
STATUSES = {'pending','approved','needs_info','paid'}
STATUS_LABELS = {'pending':'待审核','approved':'已通过','needs_info':'待补充','paid':'已打款'}


def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')
def digest(value): return hashlib.sha256(value.encode()).hexdigest()
def password_hash(value, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + ':' + hashlib.pbkdf2_hmac('sha256', value.encode(), bytes.fromhex(salt), 260000).hex()
def password_ok(value, encoded):
    try: return secrets.compare_digest(password_hash(value, encoded.split(':')[0]), encoded)
    except Exception: return False


class Problem(Exception):
    def __init__(self, status, message, code='invalid_request', details=None):
        self.status, self.message, self.code, self.details = status, message, code, details or {}


class App:
    def __init__(self, data_dir=None):
        self.data = Path(data_dir or os.environ.get('DATA_DIR', ROOT/'data')).resolve()
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.files = self.data/'files'; self.files.mkdir(exist_ok=True, mode=0o700)
        self.dbpath = self.data/'records.sqlite3'
        self.config_path = self.data/'config.json'
        self.lock = threading.Lock()
        self.rates = defaultdict(deque)
        self.event_name = os.environ.get('EVENT_NAME','零一万物2026西南生态大会')
        self.secure = os.environ.get('COOKIE_SECURE','0') == '1'
        self.trust_proxy_headers = os.environ.get('TRUST_PROXY_HEADERS','0') == '1'
        self.generated = {}
        config = json.loads(self.config_path.read_text()) if self.config_path.exists() else {}
        for env, key, label, length in [('ADMIN_PASSWORD','admin_hash','管理员密码',20),('GUEST_INVITE_CODE','invite_hash','嘉宾邀请码',10)]:
            supplied = os.environ.get(env)
            if supplied:
                if env == 'ADMIN_PASSWORD' and len(supplied) < 12: raise ValueError('ADMIN_PASSWORD 至少需要 12 位')
                if env == 'GUEST_INVITE_CODE' and len(supplied) < 6: raise ValueError('GUEST_INVITE_CODE 至少需要 6 位')
                if not config.get(key) or not password_ok(supplied,config[key]): config[key] = password_hash(supplied)
            elif key not in config:
                supplied = DEFAULT_INVITE_CODE if env=='GUEST_INVITE_CODE' else secrets.token_urlsafe(length)
                config[key] = password_hash(supplied)
                self.generated[label] = supplied
        self.config_path.write_text(json.dumps(config), encoding='utf8'); self.config_path.chmod(0o600)
        self.config = config
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS uploads (id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL, mime TEXT NOT NULL, hash TEXT NOT NULL, ocr TEXT NOT NULL, created_at TEXT NOT NULL, submission_id TEXT);
            CREATE TABLE IF NOT EXISTS submissions (id TEXT PRIMARY KEY, reference TEXT UNIQUE NOT NULL, owner TEXT NOT NULL, applicant TEXT NOT NULL, note TEXT NOT NULL, total_cents INTEGER NOT NULL, status TEXT NOT NULL, review_note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, idempotency_key TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS idempotency ON submissions(owner,idempotency_key) WHERE idempotency_key IS NOT NULL;
            CREATE TABLE IF NOT EXISTS expenses (id TEXT PRIMARY KEY, submission_id TEXT NOT NULL, file_id TEXT UNIQUE NOT NULL, category TEXT NOT NULL, cents INTEGER NOT NULL, invoice_number TEXT NOT NULL, invoice_date TEXT NOT NULL, buyer_name TEXT NOT NULL, buyer_tax_id TEXT NOT NULL, tax_cents INTEGER, note TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, subject TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS drafts (owner TEXT PRIMARY KEY, content TEXT NOT NULL, revision INTEGER NOT NULL, client_id TEXT NOT NULL, sequence INTEGER NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS expense_payment_proofs (expense_id TEXT NOT NULL, file_id TEXT UNIQUE NOT NULL, PRIMARY KEY(expense_id,file_id));
            ''')
            # Add invoice metadata without replacing existing submissions or files.
            columns={row['name'] for row in db.execute('PRAGMA table_info(expenses)')}
            for name,definition in [('seller_name',"TEXT NOT NULL DEFAULT ''"),('seller_tax_id',"TEXT NOT NULL DEFAULT ''"),('net_cents','INTEGER')]:
                if name not in columns:db.execute(f'ALTER TABLE expenses ADD COLUMN {name} {definition}')
            columns={row['name'] for row in db.execute('PRAGMA table_info(submissions)')}
            if 'payment' not in columns:db.execute("ALTER TABLE submissions ADD COLUMN payment TEXT NOT NULL DEFAULT '{}'")
            if 'deleted_at' not in columns:db.execute("ALTER TABLE submissions ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''")
            columns={row['name'] for row in db.execute('PRAGMA table_info(uploads)')}
            for name,definition in [('purpose',"TEXT NOT NULL DEFAULT 'invoice'"),('client_ref',"TEXT NOT NULL DEFAULT ''")]:
                if name not in columns:db.execute(f'ALTER TABLE uploads ADD COLUMN {name} {definition}')
        self.dbpath.chmod(0o600)

    def db(self):
        db = sqlite3.connect(self.dbpath,timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def rate(self, key, limit=12, window=60):
        with self.lock:
            queue = self.rates[key]; cutoff = time.time()-window
            while queue and queue[0] < cutoff: queue.popleft()
            if len(queue) >= limit: raise Problem(429,'尝试次数过多，请稍后再试。','rate_limit')
            queue.append(time.time())

    def session(self, handler, kind, required=True):
        cookies = SimpleCookie()
        try: cookies.load(handler.headers.get('Cookie',''))
        except Exception: pass
        name = 'vip_'+kind
        token = cookies[name].value if name in cookies else ''
        with self.db() as db:
            row = db.execute('SELECT * FROM sessions WHERE token=? AND kind=? AND expires>?',(digest(token),kind,time.time())).fetchone() if token else None
        if not row and required: raise Problem(401,'草稿会话已失效，请重新进入填报页面。' if kind=='draft' else '请先登录管理端。' if kind=='admin' else '请先验证嘉宾邀请码。','unauthorized')
        return dict(row) if row else None

    def cookie(self,kind,token,lifetime):
        return f'vip_{kind}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={lifetime}' + ('; Secure' if self.secure else '')

    def issue(self, kind, owner=None):
        token = secrets.token_urlsafe(32); lifetime = DRAFT_LIFETIME if kind=='draft' else 8*3600 if kind=='admin' else 24*3600
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE expires<?',(time.time(),))
            db.execute('DELETE FROM drafts WHERE updated_at<?',(datetime.fromtimestamp(time.time()-DRAFT_LIFETIME,timezone.utc).isoformat(timespec='seconds'),))
            db.execute('INSERT INTO sessions VALUES (?,?,?,?)',(digest(token),kind,owner or secrets.token_hex(16),time.time()+lifetime))
        return self.cookie(kind,token,lifetime)

    def renew_draft(self,handler,session):
        cookies=SimpleCookie();cookies.load(handler.headers.get('Cookie',''))
        with self.db() as db:db.execute('UPDATE sessions SET expires=? WHERE token=? AND kind=?',(time.time()+DRAFT_LIFETIME,session['token'],'draft'))
        return self.cookie('draft',cookies['vip_draft'].value,DRAFT_LIFETIME)

    def draft_response(self,owner,handler,db=None):
        if db is None:
            with self.db() as connection:return self.draft_response(owner,handler,connection)
        cutoff=datetime.fromtimestamp(time.time()-DRAFT_LIFETIME,timezone.utc).isoformat(timespec='seconds')
        db.execute('DELETE FROM drafts WHERE updated_at<?',(cutoff,))
        row=db.execute('SELECT * FROM drafts WHERE owner=?',(owner,)).fetchone()
        guest=self.session(handler,'guest',False)
        return {'draft':sanitize_draft(json.loads(row['content']),owner,db) if row else None,'revision':row['revision'] if row else 0,'updatedAt':row['updated_at'] if row else None,
                'authenticated':bool(guest and guest['owner']==owner),'uploads':[upload_metadata(u) for u in db.execute('SELECT * FROM uploads WHERE owner=? AND submission_id IS NULL ORDER BY created_at,rowid',(owner,))]}

    def audit(self, db, action, subject):
        db.execute('INSERT INTO audit(action,subject,created_at) VALUES (?,?,?)',(action,subject,now()))

    def validate_upload_slot(self,db,owner,hashvalue):
        if db.execute('SELECT COUNT(*) FROM uploads WHERE owner=? AND submission_id IS NULL',(owner,)).fetchone()[0]>=MAX_FILES:raise Problem(400,f'每次最多上传 {MAX_FILES} 个文件。')
        if db.execute("SELECT 1 FROM uploads u WHERE owner=? AND hash=? AND (submission_id IS NULL OR EXISTS (SELECT 1 FROM submissions s WHERE s.id=u.submission_id AND s.deleted_at=''))",(owner,hashvalue)).fetchone():raise Problem(409,'这份票据已上传，请勿重复添加。','duplicate_file')
        if self.active_hash_exists(db,hashvalue):raise Problem(409,'这份票据已提交，请联系工作人员核实。','duplicate_file')

    def active_hash_exists(self,db,hashvalue):
        return db.execute("SELECT 1 FROM uploads u JOIN submissions s ON s.id=u.submission_id WHERE u.hash=? AND s.deleted_at=''",(hashvalue,)).fetchone() is not None

    def submission(self, row, db):
        result = {'id':row['id'],'reference':row['reference'],'applicant':json.loads(row['applicant']),'payment':json.loads(row['payment']),'note':row['note'],'totalAmount':row['total_cents']/100,'status':row['status'],'reviewNote':row['review_note'],'createdAt':row['created_at'],'updatedAt':row['updated_at'],'deletedAt':row['deleted_at']}
        expenses = []
        for e in db.execute('SELECT e.*,u.name,u.size,u.mime,u.ocr FROM expenses e JOIN uploads u ON u.id=e.file_id WHERE e.submission_id=? ORDER BY e.rowid',(row['id'],)):
            proofs=[{'id':u['id'],'name':u['name'],'size':u['size'],'mime':u['mime']} for u in db.execute('SELECT u.* FROM uploads u JOIN expense_payment_proofs p ON p.file_id=u.id WHERE p.expense_id=? ORDER BY p.rowid',(e['id'],))]
            expenses.append({'id':e['id'],'fileId':e['file_id'],'category':e['category'],'amount':e['cents']/100,'invoiceNumber':e['invoice_number'],'date':e['invoice_date'],'buyerName':e['buyer_name'],'buyerTaxId':e['buyer_tax_id'],'taxAmount':e['tax_cents']/100 if e['tax_cents'] is not None else '', 'sellerName':e['seller_name'],'sellerTaxId':e['seller_tax_id'],'netAmount':e['net_cents']/100 if e['net_cents'] is not None else '', 'note':e['note'],'file':{'id':e['file_id'],'name':e['name'],'size':e['size'],'mime':e['mime']},'ocr':json.loads(e['ocr']),'paymentProofs':proofs})
        result['expenses']=expenses;result['expenseCount']=len(expenses)
        result['supportingFiles']=[{'id':u['id'],'name':u['name'],'size':u['size'],'mime':u['mime']} for u in db.execute('SELECT u.* FROM uploads u WHERE u.submission_id=? AND NOT EXISTS (SELECT 1 FROM expenses e WHERE e.file_id=u.id) AND NOT EXISTS (SELECT 1 FROM expense_payment_proofs p WHERE p.file_id=u.id) ORDER BY u.created_at',(row['id'],))]
        return result

    def list_items(self, query, trashed=False):
        status = query.get('status',[''])[0]; search = query.get('q',[''])[0].strip().lower()[:100]
        with self.db() as db:
            rows = db.execute("SELECT * FROM submissions WHERE (deleted_at!='')=? ORDER BY created_at DESC,rowid DESC",(int(trashed),)).fetchall()
            items = [self.submission(row,db) for row in rows]
        return [item for item in items if (not status or item['status']==status) and (not search or search in ' '.join([item['reference'],*item['applicant'].values()]).lower())]

    def excel(self, items):
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font,PatternFill,Alignment
        except ImportError: raise Problem(503,'Excel 导出依赖尚未安装，请安装 requirements.txt。','dependency_missing')
        workbook = Workbook(); sheet=workbook.active;sheet.title='报销明细'
        payment_headers=['收款方式','收款人','账号','银行','支行']
        headers=['提交编号','嘉宾姓名','联系电话','单位','对接人','费用类型','报销金额（元）','发票号码','开票日期','购买方名称','购买方税号','税额（元）','票据文件名','费用备注','审核状态','审核备注','提交时间','销售方名称','销售方税号','不含税金额（元）',*payment_headers,'付款凭证截图文件名']
        sheet.append(headers)
        labels={x['id']:x['label'] for x in CATEGORIES}
        for item in items:
            a=item['applicant']
            for e in item['expenses']:
                sheet.append([item['reference'],a['name'],a['mobile'],a['organization'],a['contactPerson'],labels[e['category']],e['amount'],e['invoiceNumber'],e['date'],e['buyerName'],e['buyerTaxId'],e['taxAmount'],e['file']['name'],e['note'],STATUS_LABELS[item['status']],item['reviewNote'],item['createdAt'],e['sellerName'],e['sellerTaxId'],e['netAmount'],*payment_cells(item.get('payment',{})),'\n'.join(p['name'] for p in e.get('paymentProofs',[]))])
        # Treat all user-supplied strings as literal Excel text to prevent formula injection.
        for row in sheet:
            for cell in row:
                if isinstance(cell.value,str): cell.data_type='s'
        for cell in sheet[1]: cell.font=Font(color='FFFFFF',bold=True);cell.fill=PatternFill('solid',fgColor='18765C')
        sheet.freeze_panes='A2';sheet.auto_filter.ref=sheet.dimensions
        for col in sheet.columns:
            from openpyxl.utils import get_column_letter
            sheet.column_dimensions[get_column_letter(col[0].column)].width=22 if col[0].column not in (1,10,13,14,16,17) else 32
        for row in sheet.iter_rows(min_row=2):
            row[6].number_format='#,##0.00';row[11].number_format='#,##0.00';row[19].number_format='#,##0.00'
        summary=workbook.create_sheet('申请汇总')
        summary.append(['申请编号','嘉宾姓名','联系电话','合计金额（元）','费用笔数','补充附件','嘉宾留言','审核状态','审核备注',*payment_headers,'付款凭证截图文件名'])
        for item in items:
            summary.append([item['reference'],item['applicant']['name'],item['applicant']['mobile'],item['totalAmount'],item['expenseCount'],'\n'.join(f['name'] for f in item['supportingFiles']),item['note'],STATUS_LABELS[item['status']],item['reviewNote'],*payment_cells(item.get('payment',{})),'\n'.join(p['name'] for e in item['expenses'] for p in e.get('paymentProofs',[]))])
        for row in summary:
            for cell in row:
                if isinstance(cell.value,str):cell.data_type='s'
                cell.alignment=Alignment(vertical='top',wrap_text=True)
        for cell in summary[1]:cell.font=Font(color='FFFFFF',bold=True);cell.fill=PatternFill('solid',fgColor='174F43')
        summary.freeze_panes='A2';summary.auto_filter.ref=summary.dimensions
        for col in summary.columns:summary.column_dimensions[col[0].column_letter].width=26
        output=io.BytesIO();workbook.save(output);return output.getvalue()


def text_field(value, label, maximum, required=False):
    if not isinstance(value,str): raise Problem(400,f'{label}格式不正确。')
    value=value.strip()
    if required and not value: raise Problem(400,f'请填写{label}。')
    if len(value)>maximum: raise Problem(400,f'{label}过长。')
    if any(ord(c)<32 and c not in '\n\t' for c in value): raise Problem(400,f'{label}包含不支持的字符。')
    return value


def cents(value, label, allow_zero=False):
    if isinstance(value,bool) or value is None: raise Problem(400,f'请填写有效的{label}。')
    try:
        num=Decimal(str(value))
        if not num.is_finite() or num<0 or (not allow_zero and num==0) or num>Decimal('100000') or num.as_tuple().exponent < -2: raise ValueError()
        return int(num*100)
    except (InvalidOperation, ValueError,TypeError): raise Problem(400,f'{label}需为大于 0、最多两位小数且不超过 100000 元的金额。')


def payment_fields(value):
    if not isinstance(value,dict):raise Problem(400,'请填写收款信息。')
    method=value.get('method')
    if method not in ('bank','alipay'):raise Problem(400,'请选择银行卡或支付宝收款。')
    name=text_field(value.get('accountName',''),'收款人实名',80,True)
    account=text_field(value.get('accountNumber',''),'收款账号',254,True)
    if any(c in name for c in '\n\t'):raise Problem(400,'收款人实名不能包含换行或制表符。')
    bank=branch=''
    if method=='bank':
        account=re.sub(r'\s+','',account)
        if not re.fullmatch(r'[0-9]{12,25}',account):raise Problem(400,'银行卡号应为 12–25 位数字，可使用空格分隔。')
        bank=text_field(value.get('bankName',''),'开户银行',120,True)
        branch=text_field(value.get('bankBranch',''),'开户支行',160)
        if any(c in bank+branch for c in '\n\t'):raise Problem(400,'银行信息不能包含换行或制表符。')
    else:
        phone=re.fullmatch(r'(?:\+?86)?1[3-9][0-9]{9}',account)
        email=re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}",account)
        if not phone and not (email and not account.startswith('.') and '..' not in account.split('@')[0] and not account.split('@')[0].endswith('.')):
            raise Problem(400,'支付宝账号应为中国大陆手机号（可带 +86 或 86）或有效邮箱。')
    return {'method':method,'accountName':name,'accountNumber':account,'bankName':bank,'bankBranch':branch}


def payment_cells(payment):
    return [{'bank':'银行卡','alipay':'支付宝'}.get(payment.get('method'),''),*[payment.get(key,'') for key in ('accountName','accountNumber','bankName','bankBranch')]]


def public_payment(payment):
    if not payment:return {}
    account=payment.get('accountNumber','')
    masked=(account[:1]+'***@'+account.split('@',1)[1]) if '@' in account else '****'+account[-4:]
    name=payment.get('accountName','')
    return {'method':payment.get('method',''),'accountName':name[:1]+'**' if len(name)>1 else '*','accountNumber':masked,'bankName':payment.get('bankName',''),'bankBranch':''}


def redact_account(value,account,masked):
    """Also cover a saved account repeated in guest notes or review comments."""
    if not account:return value
    if isinstance(value,str):
        pattern=r'\s*'.join(re.escape(c) for c in account)
        return re.sub(pattern,lambda match:masked,value)
    if isinstance(value,list):return [redact_account(item,account,masked) for item in value]
    if isinstance(value,dict):return {key:redact_account(item,account,masked) for key,item in value.items()}
    return value


def upload_metadata(row):
    return {'id':row['id'],'name':row['name'],'size':row['size'],'mime':row['mime'],'ocr':json.loads(row['ocr']),'purpose':row['purpose'],'clientRef':row['client_ref']}


def normalize_mobile(value):
    mobile=re.sub(r'[\s-]+','',text_field(value,'手机号',30,True))
    chinese=re.fullmatch(r'(?:\+?86)?(1[3-9][0-9]{9})',mobile)
    if chinese:return chinese.group(1)
    if not re.fullmatch(r'\+?[0-9]{7,20}',mobile):raise Problem(400,'请输入有效的联系电话。')
    return mobile


def mobile_matches(stored,mobile):
    try:return secrets.compare_digest(normalize_mobile(stored),mobile)
    except Problem:return False


def progress_record(row,db):
    # Phone-only lookup returns a deliberately small status summary. Financial
    # comments can contain copied accounts or internal references, so redact them.
    note=row['review_note'];payment=json.loads(row['payment']);applicant=json.loads(row['applicant'])
    private=[payment.get('accountNumber',''),payment.get('accountName',''),row['id'],row['reference'],applicant.get('name',''),applicant.get('organization',''),applicant.get('mobile','')]
    private.extend(u['id'] for u in db.execute('SELECT id FROM uploads WHERE submission_id=?',(row['id'],)))
    for value in sorted(filter(None,private),key=len,reverse=True):note=redact_account(note,value,'[已隐藏]')
    return {'status':row['status'],'totalAmount':row['total_cents']/100,
            'expenseCount':db.execute('SELECT COUNT(*) FROM expenses WHERE submission_id=?',(row['id'],)).fetchone()[0],
            'createdAt':row['created_at'],'updatedAt':row['updated_at'],'reviewNote':note}


def uuid_field(value,label,required=True):
    if not isinstance(value,str) or (value and not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}',value)) or (required and not value):
        raise Problem(400,f'{label}格式不正确。')
    return value.lower()


def sanitize_draft(value,owner,db):
    if not isinstance(value,dict):raise Problem(400,'草稿内容格式不正确。')
    clean={}
    for key in ('version','step'):
        if key in value:
            if isinstance(value[key],bool) or not isinstance(value[key],int) or not 0<=value[key]<=10:raise Problem(400,'草稿步骤或版本无效。')
            clean[key]=value[key]
    for key,fields in [('applicant',{'name':60,'mobile':20,'organization':120,'contactPerson':60}),('payment',{'method':20,'accountName':80,'accountNumber':254,'bankName':120,'bankBranch':160})]:
        if key not in value:continue
        if not isinstance(value[key],dict):raise Problem(400,'草稿基本信息格式不正确。')
        clean[key]={k:text_field(value[key][k],'草稿字段',maximum) for k,maximum in fields.items() if k in value[key]}
    for key,maximum in [('note',2000),('submissionKey',100)]:
        if key in value:clean[key]=text_field(value[key],'草稿说明' if key=='note' else '提交标记',maximum)
    if clean.get('submissionKey') and not re.fullmatch(r'[A-Za-z0-9_-]+',clean['submissionKey']):raise Problem(400,'提交标记无效。')
    files={u['id']:u for u in db.execute('SELECT * FROM uploads WHERE owner=? AND submission_id IS NULL',(owner,))}
    seen=set()
    def reference(file_id,kind):
        if not isinstance(file_id,str):raise Problem(400,'草稿附件格式不正确。')
        file=files.get(file_id)
        if not file or file_id in seen:return None
        if kind=='invoice' and file['purpose']!='invoice':return None
        if kind=='proof' and (file['purpose']=='invoice' or file['mime'] not in ('image/jpeg','image/png')):return None
        if kind=='support' and file['purpose']=='payment-proof':return None
        seen.add(file_id);return file_id
    if 'expenses' in value:
        if not isinstance(value['expenses'],list) or len(value['expenses'])>MAX_FILES:raise Problem(400,'草稿费用数量超出限制。')
        clean['expenses']=[]
        limits={'category':30,'invoiceNumber':40,'date':10,'buyerName':160,'buyerTaxId':30,'sellerName':160,'sellerTaxId':30,'note':1000}
        for item in value['expenses']:
            if not isinstance(item,dict):raise Problem(400,'草稿费用格式不正确。')
            expense={k:text_field(item[k],'草稿费用',maximum) for k,maximum in limits.items() if k in item}
            if 'uid' in item:expense['uid']=uuid_field(item['uid'],'费用标记',False)
            for key in ('amount','netAmount','taxAmount'):
                if key not in item:continue
                amount=item[key]
                if isinstance(amount,str):expense[key]=text_field(amount,'草稿金额',30)
                elif not isinstance(amount,bool) and isinstance(amount,(int,float)) and Decimal(str(amount)).is_finite() and abs(amount)<=100000:expense[key]=amount
                elif amount is None:expense[key]=''
                else:raise Problem(400,'草稿金额格式不正确。')
            if item.get('fileId'):
                file_id=reference(item['fileId'],'invoice')
                if file_id:expense['fileId']=file_id
            if 'paymentProofIds' in item:
                proof_ids=item['paymentProofIds']
                if not isinstance(proof_ids,list) or len(proof_ids)>3:raise Problem(400,'每笔费用最多添加 3 张付款截图。')
                expense['paymentProofIds']=[valid for file_id in proof_ids if (valid:=reference(file_id,'proof'))]
            clean['expenses'].append(expense)
    if 'supportingFiles' in value:
        if not isinstance(value['supportingFiles'],list) or len(value['supportingFiles'])>MAX_FILES:raise Problem(400,'草稿附件数量超出限制。')
        clean['supportingFiles']=[valid for file_id in value['supportingFiles'] if (valid:=reference(file_id,'support'))]
    if len(seen)>MAX_FILES:raise Problem(400,f'每份草稿最多保留 {MAX_FILES} 个文件。')
    return clean


class Handler(BaseHTTPRequestHandler):
    server_version='VIPService/1.0'
    protocol_version='HTTP/1.1'
    @property
    def app(self): return self.server.app
    @property
    def client_ip(self):
        # Opt in only behind a private proxy that overwrites this header.
        if self.app.trust_proxy_headers:
            try:return str(ipaddress.ip_address(self.headers.get('X-Real-IP','').strip()))
            except ValueError:pass
        return self.client_address[0]
    def log_message(self,format,*args):
        # Do not log query strings, bodies, names or private file paths.
        print(f'[{now()}] {self.command} {urlsplit(self.path).path} {args[1] if len(args)>1 else ""}',flush=True)
    def headers_common(self,mime):
        self.send_header('Content-Type',mime)
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Permissions-Policy','camera=(), microphone=(), geolocation=()')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.send_header('Cache-Control','no-store' if self.path.startswith('/api/') or urlsplit(self.path).path=='/healthz' else 'no-cache')
    def respond(self,status,body,mime='application/json; charset=utf-8',headers=None):
        if not isinstance(body,bytes):body=json.dumps(body,ensure_ascii=False,allow_nan=False).encode()
        self.send_response(status);self.headers_common(mime)
        if status >= 400:
            self.send_header('Connection','close');self.close_connection=True
        self.send_header('Content-Length',str(len(body)))
        for key,value in (headers or {}).items():self.send_header(key,value)
        self.end_headers()
        if self.command!='HEAD':self.wfile.write(body)
    def body(self,limit=256*1024):
        try:length=int(self.headers.get('Content-Length','0'))
        except ValueError:raise Problem(400,'请求长度不正确。')
        if self.headers.get('Transfer-Encoding'):raise Problem(400,'不支持此上传方式。')
        if length<0 or length>limit:raise Problem(413,'文件或请求过大。')
        data=self.rfile.read(length)
        if len(data)!=length:raise Problem(400,'上传未完成，请重试。')
        return data
    def json_body(self):
        if self.headers.get_content_type()!='application/json':raise Problem(415,'请使用 JSON 提交。')
        try:
            result=json.loads(self.body())
            if not isinstance(result,dict):raise ValueError()
            return result
        except (json.JSONDecodeError,UnicodeDecodeError,ValueError):raise Problem(400,'提交内容格式不正确。')
    def origin(self):
        if self.headers.get('Sec-Fetch-Site')=='cross-site':raise Problem(403,'不允许跨站提交。','forbidden')
        origin=self.headers.get('Origin')
        if origin:
            parsed=urlsplit(origin)
            if parsed.scheme not in ('http','https') or parsed.netloc!=self.headers.get('Host'):raise Problem(403,'不允许跨站提交。','forbidden')
    def dispatch(self):
        try:
            if self.command in ('POST','PATCH','DELETE'):self.origin()
            self.route()
        except Problem as e:
            self.respond(e.status,{'error':e.code,'message':e.message,**e.details})
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception as e:
            print(f'[{now()}] server error: {type(e).__name__}',flush=True)
            try:self.respond(500,{'error':'server_error','message':'服务暂时无法完成操作，请稍后重试。'})
            except (BrokenPipeError,ConnectionResetError):pass
    do_GET=dispatch;do_POST=dispatch;do_PATCH=dispatch;do_DELETE=dispatch;do_HEAD=dispatch
    def route(self):
        path=urlsplit(self.path).path;query=parse_qs(urlsplit(self.path).query);method=self.command
        if method in ('GET','HEAD') and path=='/healthz':
            with self.app.db() as db:db.execute('SELECT 1').fetchone()
            self.respond(200,{'ok':True});return
        if method=='GET' and path=='/api/config':
            self.respond(200,{'eventName':self.app.event_name,'helpText':'报销范围与标准以大会财务确认的信息为准。请填写本人银行卡或支付宝收款信息，仅用于本次大会报销核对与汇款。','ocr':capabilities(),'maxFileSize':MAX_FILE,'maxFiles':MAX_FILES,'categories':CATEGORIES});return
        if method=='POST' and path=='/api/guest/draft/session':
            self.json_body();self.app.rate((self.client_ip,path),60,60)
            guest=self.app.session(self,'guest',False);draft=self.app.session(self,'draft',False)
            if draft and (not guest or draft['owner']==guest['owner']):
                owner=draft['owner'];cookie=self.app.renew_draft(self,draft)
            else:
                owner=guest['owner'] if guest else secrets.token_hex(16);cookie=self.app.issue('draft',owner)
            self.respond(200,self.app.draft_response(owner,self),headers={'Set-Cookie':cookie});return
        if path=='/api/guest/draft' and method in ('GET','POST'):
            draft=self.app.session(self,'draft')
            if method=='GET':self.respond(200,self.app.draft_response(draft['owner'],self));return
            self.save_draft(draft);return
        if method=='POST' and path in ('/api/guest/session','/api/admin/login'):
            self.app.rate((self.client_ip,path),8,60);body=self.json_body();admin=path.endswith('/login');value=body.get('password' if admin else 'inviteCode','')
            if not isinstance(value,str) or len(value)>200 or not password_ok(value,self.app.config['admin_hash' if admin else 'invite_hash']):raise Problem(401,'管理员密码不正确。' if admin else '邀请码不正确，请联系您的会务对接人。','invalid_credentials')
            # Preserve the guest's owner when returning to step 1, so existing uploads remain usable.
            existing=self.app.session(self,'guest',False) if not admin else None
            draft=self.app.session(self,'draft',False) if not admin else None
            cookie=self.app.issue('admin' if admin else 'guest',draft['owner'] if draft else None) if not existing or (draft and existing['owner']!=draft['owner']) else None
            if admin:
                with self.app.db() as db:self.app.audit(db,'admin_login','admin')
            self.respond(200,{'authenticated':True},headers={'Set-Cookie':cookie} if cookie else {});return
        if method=='GET' and path in ('/api/admin/session','/api/guest/session'):
            self.app.session(self,'admin' if '/admin/' in path else 'guest');self.respond(200,{'authenticated':True});return
        if method=='POST' and path=='/api/admin/logout':
            session=self.app.session(self,'admin')
            self.json_body()
            with self.app.db() as db:db.execute('DELETE FROM sessions WHERE token=?',(session['token'],))
            self.respond(200,{'ok':True},headers={'Set-Cookie':'vip_admin=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0'});return
        if method=='POST' and path=='/api/uploads':self.upload();return
        match=re.fullmatch(r'/api/uploads/([a-f0-9]{32})/recognize',path)
        if match and method=='POST':
            guest=self.app.session(self,'guest');self.json_body();self.app.rate((guest['owner'],'recognize'),12,60)
            with self.app.db() as db:row=db.execute('SELECT * FROM uploads WHERE id=? AND owner=?',(match[1],guest['owner'])).fetchone()
            if not row:raise Problem(404,'票据不存在或无权识别。','not_found')
            if row['submission_id']:raise Problem(409,'已提交的票据不能重新识别。')
            if row['purpose']!='invoice':raise Problem(400,'补充材料和付款截图无需发票识别。')
            result=recognize(self.app.files/row['id'],row['mime'])
            with self.app.db() as db:db.execute('UPDATE uploads SET ocr=? WHERE id=? AND submission_id IS NULL',(json.dumps(result,ensure_ascii=False),row['id']))
            self.respond(200,{'ocr':result});return
        match=re.fullmatch(r'/api/(?:files|uploads)/([a-f0-9]{32})',path)
        if match and method in ('GET','DELETE'):
            with self.app.db() as db:row=db.execute('SELECT * FROM uploads WHERE id=?',(match[1],)).fetchone()
            if not row:raise Problem(404,'文件不存在。','not_found')
            admin=self.app.session(self,'admin',False);guest=self.app.session(self,'guest',False)
            if not admin and (not guest or guest['owner']!=row['owner']):raise Problem(403,'您没有权限查看该文件。','forbidden')
            if not admin and row['submission_id']:
                with self.app.db() as db:visible=db.execute("SELECT 1 FROM submissions WHERE id=? AND deleted_at=''",(row['submission_id'],)).fetchone()
                if not visible:raise Problem(404,'文件不存在。','not_found')
            if method=='DELETE':
                if row['submission_id']:raise Problem(409,'已提交的票据不能删除。')
                with self.app.db() as db:db.execute('DELETE FROM uploads WHERE id=?',(row['id'],))
                (self.app.files/row['id']).unlink(missing_ok=True);self.respond(200,{'ok':True});return
            raw=(self.app.files/row['id']).read_bytes()
            self.respond(200,raw,row['mime'],{'Content-Disposition':f"inline; filename*=UTF-8''{quote(row['name'])}"});return
        if method=='POST' and path=='/api/submissions':self.submit();return
        if method=='POST' and path=='/api/guest/lookup':
            self.app.rate((self.client_ip,path),8,60);body=self.json_body()
            ref=text_field(body.get('reference',''),'提交编号',40);mobile=normalize_mobile(body.get('mobile',''))
            with self.app.db() as db:
                if not ref:
                    rows=db.execute("SELECT * FROM submissions WHERE deleted_at='' ORDER BY created_at DESC,rowid DESC").fetchall()
                    items=[progress_record(row,db) for row in rows if mobile_matches(json.loads(row['applicant']).get('mobile',''),mobile)]
                    self.respond(200,{'items':items});return
                row=db.execute("SELECT * FROM submissions WHERE reference=? AND deleted_at=''",(ref.upper(),)).fetchone()
                if not row or not mobile_matches(json.loads(row['applicant'])['mobile'],mobile):raise Problem(404,'提交编号或手机号不匹配。','not_found')
                item=self.app.submission(row,db)
            payment=item.get('payment',{});item['payment']=public_payment(payment)
            proof_ids=[p['id'] for e in item['expenses'] for p in e.get('paymentProofs',[])]
            item['applicant']['mobile']=mobile[:3]+'****'+mobile[-4:]
            item['applicant'].pop('contactPerson',None)
            item.pop('supportingFiles',None)
            for e in item['expenses']:e.pop('file',None);e.pop('fileId',None);e.pop('ocr',None);e.pop('paymentProofs',None)
            item=redact_account(item,payment.get('accountNumber',''),item['payment'].get('accountNumber',''))
            for proof_id in proof_ids:item=redact_account(item,proof_id,'[付款截图]')
            self.respond(200,item);return
        if path.startswith('/api/admin/'):
            self.app.session(self,'admin')
            if method=='GET' and path=='/api/admin/submissions':
                items=self.app.list_items(query,trashed=query.get('deleted',['0'])[0]=='1')
                with self.app.db() as db:trash_count=db.execute("SELECT count(*) FROM submissions WHERE deleted_at!=''").fetchone()[0]
                self.respond(200,{'items':items,'trashCount':trash_count,'stats':{'count':len(items),'pending':sum(x['status']=='pending' for x in items),'totalAmount':round(sum(x['totalAmount'] for x in items),2),'approved':sum(x['status'] in ('approved','paid') for x in items)}});return
            match=re.fullmatch(r'/api/admin/submissions/([a-f0-9]{32})(/restore)?',path)
            if match and ((method=='DELETE' and not match[2]) or (method=='POST' and match[2])):
                self.json_body();restore=bool(match[2]);sub_id=match[1]
                with self.app.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    row=db.execute('SELECT * FROM submissions WHERE id=?',(sub_id,)).fetchone()
                    if not row:raise Problem(404,'报销记录不存在。','not_found')
                    if restore and row['deleted_at']:
                        # A deleted test invoice may have been re-submitted. Restoring
                        # it must not allow two active applications for one invoice.
                        duplicate=db.execute("SELECT 1 FROM expenses e JOIN expenses other ON other.invoice_number=e.invoice_number AND other.submission_id!=e.submission_id JOIN submissions s ON s.id=other.submission_id WHERE e.submission_id=? AND e.invoice_number!='' AND s.deleted_at=''",(sub_id,)).fetchone()
                        duplicate_file=db.execute("SELECT 1 FROM uploads u JOIN uploads other ON other.hash=u.hash AND other.submission_id!=u.submission_id JOIN submissions s ON s.id=other.submission_id WHERE u.submission_id=? AND s.deleted_at=''",(sub_id,)).fetchone()
                        if duplicate or duplicate_file:raise Problem(409,'相同票据已用于另一份有效申请，暂不能恢复。请先核对并处理重复申请。','restore_conflict')
                        db.execute("UPDATE submissions SET deleted_at='' WHERE id=?",(sub_id,));self.app.audit(db,'submission_restore',sub_id)
                    elif not restore and not row['deleted_at']:
                        db.execute('UPDATE submissions SET deleted_at=? WHERE id=?',(now(),sub_id));self.app.audit(db,'submission_delete',sub_id)
                    result=self.app.submission(db.execute('SELECT * FROM submissions WHERE id=?',(sub_id,)).fetchone(),db)
                self.respond(200,{'ok':True,'deletedAt':result['deletedAt']});return
            if method=='PATCH' and match and not match[2]:
                body=self.json_body();status=body.get('status');note=text_field(body.get('reviewNote',''),'审核备注',2000)
                if status not in STATUSES:raise Problem(400,'审核状态不正确。')
                if status=='needs_info' and not note:raise Problem(400,'请填写需要嘉宾补充的内容。')
                with self.app.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    row=db.execute('SELECT * FROM submissions WHERE id=?',(match[1],)).fetchone()
                    if not row:raise Problem(404,'报销记录不存在。')
                    if row['deleted_at']:raise Problem(409,'请先从回收站恢复申请，再处理审核。','submission_deleted')
                    db.execute('UPDATE submissions SET status=?,review_note=?,updated_at=? WHERE id=?',(status,note,now(),match[1]));self.app.audit(db,'review_'+status,match[1])
                    item=self.app.submission(db.execute('SELECT * FROM submissions WHERE id=?',(match[1],)).fetchone(),db)
                self.respond(200,item);return
            if method=='GET' and path in ('/api/admin/export.xlsx','/api/admin/export.zip'):
                items=self.app.list_items(query);xlsx=self.app.excel(items)
                with self.app.db() as db:self.app.audit(db,'export',str(len(items)))
                if path.endswith('.xlsx'):self.respond(200,xlsx,'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',{'Content-Disposition':'attachment; filename="reimbursements.xlsx"'});return
                output=io.BytesIO()
                with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
                    z.writestr('报销汇总.xlsx',xlsx)
                    for item in items:
                        for index,e in enumerate(item['expenses'],1):
                            name=re.sub(r'[^\w.\-\u4e00-\u9fff]','_',e['file']['name'])[:120]
                            z.write(self.app.files/e['fileId'],f"{item['reference']}/{index:02d}_{name}")
                            for proof_index,proof in enumerate(e.get('paymentProofs',[]),1):
                                name=re.sub(r'[^\w.\-\u4e00-\u9fff]','_',proof['name'])[:120]
                                z.write(self.app.files/proof['id'],f"{item['reference']}/付款凭证/{index:02d}_{proof_index:02d}_{name}")
                        for index,f in enumerate(item['supportingFiles'],1):
                            name=re.sub(r'[^\w.\-\u4e00-\u9fff]','_',f['name'])[:120]
                            z.write(self.app.files/f['id'],f"{item['reference']}/补充附件/{index:02d}_{name}")
                self.respond(200,output.getvalue(),'application/zip',{'Content-Disposition':'attachment; filename="reimbursement-materials.zip"'});return
            if method=='GET' and path=='/api/admin/audit':
                with self.app.db() as db:logs=[dict(x) for x in db.execute('SELECT action,subject,created_at FROM audit ORDER BY id DESC LIMIT 100')]
                self.respond(200,{'items':logs});return
        if path.startswith('/api/'):raise Problem(404,'接口不存在。','not_found')
        if method not in ('GET','HEAD'):raise Problem(405,'不支持此操作。')
        if path in ('/','/admin','/admin/'):file=ROOT/'public/index.html'
        else:
            public=(ROOT/'public').resolve();file=(public/unquote(path).lstrip('/')).resolve()
            if not file.is_relative_to(public):raise Problem(404,'页面不存在。','not_found')
        if not file.is_file() or file.name.startswith('.'):raise Problem(404,'页面不存在。','not_found')
        mime=mimetypes.guess_type(file.name)[0] or 'application/octet-stream'
        self.respond(200,file.read_bytes(),mime+('; charset=utf-8' if mime.startswith(('text/','application/javascript')) else ''))

    def save_draft(self,session):
        body=self.json_body();client=uuid_field(body.get('clientId'),'草稿客户端标记')
        sequence,base=body.get('sequence'),body.get('baseRevision')
        if any(isinstance(n,bool) or not isinstance(n,int) or not 0<=n<=9_007_199_254_740_991 for n in (sequence,base)):raise Problem(400,'草稿保存版本无效。')
        incoming=body.get('draft')
        if not isinstance(incoming,dict):raise Problem(400,'草稿内容格式不正确。')
        owner=session['owner'];committed=False;ignored=False
        self.app.rate((owner,'draft_save'),120,60)
        with self.app.db() as db:
            db.execute('BEGIN IMMEDIATE')
            key=incoming.get('submissionKey')
            if isinstance(key,str) and key and db.execute('SELECT 1 FROM submissions WHERE owner=? AND idempotency_key=?',(owner,key)).fetchone():
                committed=True
            else:
                # Expire stale content before comparing revisions, including reads
                # made through a still-valid, recently renewed browser cookie.
                self.app.draft_response(owner,self,db)
                current=db.execute('SELECT * FROM drafts WHERE owner=?',(owner,)).fetchone()
                revision=current['revision'] if current else 0
                if current and current['client_id']==client and sequence<=current['sequence']:ignored=True
                elif (not current or current['client_id']!=client) and base!=revision:
                    raise Problem(409,'草稿已在其他页面更新，请刷新后继续。','draft_conflict',{'revision':revision})
                else:
                    clean=sanitize_draft(incoming,owner,db)
                    db.execute('INSERT INTO drafts (owner,content,revision,client_id,sequence,updated_at) VALUES (?,?,?,?,?,?) ON CONFLICT(owner) DO UPDATE SET content=excluded.content,revision=excluded.revision,client_id=excluded.client_id,sequence=excluded.sequence,updated_at=excluded.updated_at',
                               (owner,json.dumps(clean,ensure_ascii=False,allow_nan=False),revision+1,client,sequence,now()))
            result=self.app.draft_response(owner,self,db)
        if committed:result.update(draft=None,committed=True)
        if ignored:result['ignored']=True
        cookie=self.app.renew_draft(self,session)
        self.respond(200,result,headers={'Set-Cookie':cookie})

    def upload(self):
        guest=self.app.session(self,'guest');self.app.rate((guest['owner'],'upload'),30,60)
        name=unquote(self.headers.get('X-Filename','')).replace('\\','/').split('/')[-1]
        name=text_field(name,'文件名',180,True)
        purpose=self.headers.get('X-Upload-Purpose','invoice')
        if purpose not in ('invoice','support','payment-proof'):raise Problem(400,'上传用途不正确。')
        client_ref=uuid_field(self.headers.get('X-Draft-Entry',''),'费用标记',False)
        mime=self.headers.get_content_type();raw=self.body(MAX_FILE)
        if not raw:raise Problem(400,'文件为空。')
        allowed={'application/pdf':('.pdf',),'image/jpeg':('.jpg','.jpeg'),'image/png':('.png',)}
        if mime not in allowed or Path(name).suffix.lower() not in allowed[mime]:raise Problem(415,'仅支持 PDF、JPG、PNG 票据。')
        if purpose=='payment-proof' and mime not in ('image/jpeg','image/png'):raise Problem(415,'付款截图仅支持 JPG、PNG 图片。')
        if mime=='application/pdf' and not raw.startswith(b'%PDF-'):raise Problem(415,'文件不是有效的 PDF。')
        if mime.startswith('image/'):
            try:
                from PIL import Image
                with Image.open(io.BytesIO(raw)) as image:
                    expected='JPEG' if mime=='image/jpeg' else 'PNG'
                    if image.format!=expected or image.width*image.height>40_000_000:raise ValueError()
                    image.verify()
            except ImportError:
                if not (raw.startswith(b'\xff\xd8\xff') if mime=='image/jpeg' else raw.startswith(b'\x89PNG\r\n\x1a\n')):raise Problem(415,'图片格式无效。')
            except Exception:raise Problem(415,'图片无效或尺寸过大，请更换清晰票据。')
        hashvalue=hashlib.sha256(raw).hexdigest()
        with self.app.db() as db:self.app.validate_upload_slot(db,guest['owner'],hashvalue)
        file_id=secrets.token_hex(16);target=self.app.files/file_id
        try:
            target.write_bytes(raw);target.chmod(0o600)
            # OCR can take several seconds. Hold the database writer lock only
            # for the final validation and insert, including concurrent uploads.
            result=recognize(target,mime) if purpose=='invoice' else {'status':'skipped','engine':'','fields':{},'recognizedFields':[],'warnings':[],'message':'附件仅用于人工核对，不进行发票识别。'}
            with self.app.db() as db:
                db.execute('BEGIN IMMEDIATE')
                self.app.validate_upload_slot(db,guest['owner'],hashvalue)
                db.execute('INSERT INTO uploads (id,owner,name,size,mime,hash,ocr,created_at,submission_id,purpose,client_ref) VALUES (?,?,?,?,?,?,?,?,NULL,?,?)',(file_id,guest['owner'],name,len(raw),mime,hashvalue,json.dumps(result,ensure_ascii=False),now(),purpose,client_ref))
        except Exception:target.unlink(missing_ok=True);raise
        self.respond(201,{'id':file_id,'name':name,'size':len(raw),'mime':mime,'ocr':result,'purpose':purpose,'clientRef':client_ref,'duplicate':False})

    def submit(self):
        guest=self.app.session(self,'guest');body=self.json_body();applicant=body.get('applicant');expenses=body.get('expenses');support=body.get('supportingFiles',[]);key=self.headers.get('Idempotency-Key')
        if key and (len(key)>100 or not re.fullmatch(r'[A-Za-z0-9_-]+',key)):raise Problem(400,'提交标记无效。')
        if key:
            with self.app.db() as db:existing=db.execute('SELECT * FROM submissions WHERE owner=? AND idempotency_key=?',(guest['owner'],key)).fetchone()
            if existing:
                if existing['deleted_at']:raise Problem(409,'这份申请已被工作人员删除，请重新进入填报页面创建新申请。','submission_deleted')
                self.respond(200,{'reference':existing['reference'],'submittedAt':existing['created_at'],'expenseCount':len(expenses or []),'totalAmount':existing['total_cents']/100});return
        if body.get('consent') is not True:raise Problem(400,'请阅读并同意资料使用说明。')
        if not isinstance(applicant,dict):raise Problem(400,'请填写嘉宾基本信息。')
        applicant={k:text_field(applicant.get(k,''),label,maximum,required) for k,label,maximum,required in [('name','姓名',60,True),('mobile','手机号',20,True),('organization','单位',120,True),('contactPerson','会务对接人',60,False)]}
        applicant['mobile']=normalize_mobile(applicant['mobile'])
        payment=payment_fields(body.get('payment'))
        if not isinstance(expenses,list) or not 1<=len(expenses)<=MAX_FILES:raise Problem(400,f'请提交 1–{MAX_FILES} 份费用票据。')
        if not isinstance(support,list) or len(support)+len(expenses)>MAX_FILES:raise Problem(400,f'每份申请最多上传 {MAX_FILES} 个文件（含补充附件）。')
        proof_total=0
        for expense in expenses:
            if not isinstance(expense,dict):raise Problem(400,'费用内容格式不正确。')
            proofs=expense.get('paymentProofIds',[])
            if not isinstance(proofs,list) or len(proofs)>3:raise Problem(400,'每笔费用最多添加 3 张付款截图。')
            proof_total+=len(proofs)
        if len(expenses)+len(support)+proof_total>MAX_FILES:raise Problem(400,f'每份申请最多上传 {MAX_FILES} 个文件（含补充附件和付款截图）。')
        note=text_field(body.get('note',''),'补充说明',2000)
        validated=[];validated_proofs=[];seen=set();invoice_seen=set()
        with self.app.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for e in expenses:
                if not isinstance(e,dict):raise Problem(400,'费用内容格式不正确。')
                file_id=e.get('fileId','')
                if not isinstance(file_id,str) or not re.fullmatch(r'[a-f0-9]{32}',file_id) or file_id in seen:raise Problem(400,'票据文件无效或重复。')
                seen.add(file_id)
                upload=db.execute('SELECT * FROM uploads WHERE id=? AND owner=?',(file_id,guest['owner'])).fetchone()
                if not upload or upload['submission_id']:raise Problem(400,'票据文件不存在、已提交或无权使用。')
                if upload['purpose']!='invoice':raise Problem(400,'费用票据的上传用途不正确。')
                if self.app.active_hash_exists(db,upload['hash']):raise Problem(409,'其中一份票据已提交，请勿重复报销。','duplicate_file')
                category=e.get('category')
                if category not in {x['id'] for x in CATEGORIES}:raise Problem(400,'请选择有效的费用类型。')
                amount=cents(e.get('amount'),'报销金额')
                tax=cents(e['taxAmount'],'税额',True) if e.get('taxAmount') not in ('',None) else None
                if tax is not None and tax>amount:raise Problem(400,'税额不能超过报销金额。')
                invoice=text_field(e.get('invoiceNumber',''),'发票号码',40)
                if invoice and (invoice in invoice_seen or db.execute("SELECT 1 FROM expenses e JOIN submissions s ON s.id=e.submission_id WHERE e.invoice_number=? AND s.deleted_at=''",(invoice,)).fetchone()):raise Problem(409,'发票号码重复，请核对后再提交。','duplicate_invoice')
                if invoice:invoice_seen.add(invoice)
                date=text_field(e.get('date',''),'开票日期',10)
                if date:
                    try:datetime.strptime(date,'%Y-%m-%d')
                    except ValueError:raise Problem(400,'开票日期格式应为 YYYY-MM-DD。')
                buyer=text_field(e.get('buyerName',''),'购买方名称',160);taxid=text_field(e.get('buyerTaxId',''),'购买方税号',30).upper();enote=text_field(e.get('note',''),'费用说明',1000)
                seller=text_field(e.get('sellerName',''),'销售方名称',160);seller_taxid=text_field(e.get('sellerTaxId',''),'销售方税号',30).upper()
                net=cents(e['netAmount'],'不含税金额',True) if e.get('netAmount') not in ('',None) else None
                expense_id=secrets.token_hex(16)
                validated.append((expense_id,file_id,category,amount,invoice,date,buyer,taxid,tax,enote,seller,seller_taxid,net))
                for proof_id in e.get('paymentProofIds',[]):
                    if not isinstance(proof_id,str) or not re.fullmatch(r'[a-f0-9]{32}',proof_id) or proof_id in seen:raise Problem(400,'付款截图无效或重复。')
                    seen.add(proof_id)
                    proof=db.execute('SELECT * FROM uploads WHERE id=? AND owner=? AND submission_id IS NULL',(proof_id,guest['owner'])).fetchone()
                    if not proof or proof['purpose']=='invoice' or proof['mime'] not in ('image/jpeg','image/png'):raise Problem(400,'付款截图不存在、用途不正确、已提交或无权使用。')
                    validated_proofs.append((expense_id,proof_id))
            for file_id in support:
                if not isinstance(file_id,str) or not re.fullmatch(r'[a-f0-9]{32}',file_id) or file_id in seen:raise Problem(400,'补充附件无效或重复。')
                seen.add(file_id)
                upload=db.execute('SELECT * FROM uploads WHERE id=? AND owner=? AND submission_id IS NULL',(file_id,guest['owner'])).fetchone()
                if not upload:raise Problem(400,'补充附件不存在、已提交或无权使用。')
                if upload['purpose']=='payment-proof':raise Problem(400,'请将付款截图关联到对应费用。')
            sub_id=secrets.token_hex(16);reference='NJ-'+datetime.now().strftime('%y%m%d')+'-'+secrets.token_hex(3).upper();timestamp=now();total=sum(x[3] for x in validated)
            db.execute('INSERT INTO submissions (id,reference,owner,applicant,note,total_cents,status,review_note,created_at,updated_at,idempotency_key,payment) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(sub_id,reference,guest['owner'],json.dumps(applicant,ensure_ascii=False),note,total,'pending','',timestamp,timestamp,key,json.dumps(payment,ensure_ascii=False)))
            for e in validated:
                db.execute('INSERT INTO expenses (id,submission_id,file_id,category,cents,invoice_number,invoice_date,buyer_name,buyer_tax_id,tax_cents,note,seller_name,seller_tax_id,net_cents) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(e[0],sub_id,*e[1:]))
                db.execute('UPDATE uploads SET submission_id=? WHERE id=?',(sub_id,e[1]))
            for file_id in support:db.execute('UPDATE uploads SET submission_id=? WHERE id=?',(sub_id,file_id))
            for expense_id,file_id in validated_proofs:
                db.execute('INSERT INTO expense_payment_proofs (expense_id,file_id) VALUES (?,?)',(expense_id,file_id))
                db.execute('UPDATE uploads SET submission_id=? WHERE id=?',(sub_id,file_id))
            db.execute('DELETE FROM drafts WHERE owner=?',(guest['owner'],))
            self.app.audit(db,'guest_submit',sub_id)
        self.respond(201,{'reference':reference,'submittedAt':timestamp,'expenseCount':len(validated),'totalAmount':total/100})


def create_server(host='127.0.0.1',port=8765,data_dir=None):
    server=ThreadingHTTPServer((host,port),Handler);server.app=App(data_dir);server.daemon_threads=True
    return server


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--host',default=os.environ.get('HOST','127.0.0.1'));parser.add_argument('--port',type=int,default=int(os.environ.get('PORT','8765')));parser.add_argument('--data-dir',default=None);args=parser.parse_args()
    server=create_server(args.host,args.port,args.data_dir)
    print(f'网站: http://{args.host}:{server.server_address[1]}\n管理端: http://{args.host}:{server.server_address[1]}/admin',flush=True)
    for label,value in server.app.generated.items():print(f'{label}（仅本次显示，请妥善保存）: {value}',flush=True)
    print('未显示凭据时请使用之前保存的值，或通过 ADMIN_PASSWORD / GUEST_INVITE_CODE 环境变量重新设置。',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()

if __name__=='__main__':main()
