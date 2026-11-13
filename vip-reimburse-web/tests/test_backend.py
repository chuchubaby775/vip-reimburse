import http.cookiejar
import http.client
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
import sqlite3
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4
import urllib.error
import urllib.request
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from server import App,DEFAULT_INVITE_CODE,DRAFT_LIFETIME,create_server,digest,password_ok,payment_fields
from ocr import extract_fields

class BackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.previous={k:os.environ.get(k) for k in ('ADMIN_PASSWORD','GUEST_INVITE_CODE')}
        os.environ['ADMIN_PASSWORD']='synthetic-test-password-34';os.environ['GUEST_INVITE_CODE']='TESTINVITE'
        work=ROOT.parent.parent/'work';work.mkdir(exist_ok=True)
        cls.temp=tempfile.TemporaryDirectory(prefix='backend-tests-',dir=work)
        cls.server=create_server('127.0.0.1',0,cls.temp.name)
        cls.base=f'http://127.0.0.1:{cls.server.server_address[1]}'
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(3);cls.temp.cleanup()
        for k,v in cls.previous.items():
            if v is None:os.environ.pop(k,None)
            else:os.environ[k]=v
    def setUp(self):
        with self.server.app.lock:self.server.app.rates.clear()
    def client(self):
        jar=http.cookiejar.CookieJar();client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar));client.jar=jar;return client
    def req(self,client,path,body=None,method=None,headers=None):
        hs=headers or {};data=body
        if isinstance(body,dict):data=json.dumps(body).encode();hs={**hs,'Content-Type':'application/json'}
        request=urllib.request.Request(self.base+path,data=data,headers=hs,method=method)
        try:
            with client.open(request) as response:
                raw=response.read();return response.status,json.loads(raw) if 'application/json' in response.headers.get('Content-Type','') else raw
        except urllib.error.HTTPError as e:return e.code,json.loads(e.read())
    def pdf(self,number):
        from reportlab.pdfgen import canvas
        stream=io.BytesIO();c=canvas.Canvas(stream,invariant=1)
        for i,line in enumerate([f'Invoice Number: {number}','Invoice Date: 2026-11-12','Buyer Name: Synthetic Conference','Buyer Tax ID: 91510000TEST000001A','Total Amount: 123.45','Tax Amount: 3.45']):c.drawString(40,750-i*30,line)
        c.save();return stream.getvalue()
    def guest(self):
        client=self.client();self.assertEqual(self.req(client,'/api/guest/session',{'inviteCode':'TESTINVITE'})[0],200);return client
    def upload(self,client,number,purpose='invoice',uid=''):
        status,result=self.req(client,'/api/uploads',self.pdf(number),headers={'Content-Type':'application/pdf','X-Filename':'synthetic-invoice.pdf','X-Upload-Purpose':purpose,'X-Draft-Entry':uid})
        self.assertEqual(status,201,result);return result
    def proof(self,client,uid,color=1):
        from PIL import Image
        stream=io.BytesIO();Image.new('RGB',(40,40),(color,30,90)).save(stream,format='PNG')
        status,result=self.req(client,'/api/uploads',stream.getvalue(),headers={'Content-Type':'image/png','X-Filename':f'payment-proof-{color}.png','X-Upload-Purpose':'payment-proof','X-Draft-Entry':uid})
        self.assertEqual(status,201,result);return result
    def draft_session(self,client):
        status,result=self.req(client,'/api/guest/draft/session',{});self.assertEqual(status,200,result);return result
    def save(self,client,draft,client_id=None,sequence=1,base=0):
        return self.req(client,'/api/guest/draft',{'draft':draft,'clientId':client_id or str(uuid4()),'sequence':sequence,'baseRevision':base})
    def payload(self,file,number):return {'applicant':{'name':'测试嘉宾','mobile':'13800000000','organization':'虚构测试机构','contactPerson':'测试对接人'},'payment':{'method':'bank','accountName':'测试嘉宾','accountNumber':'0012 3456 7890 1234','bankName':'虚构测试银行','bankBranch':'内江测试支行'},'expenses':[{'fileId':file['id'],'category':'rail','amount':123.45,'invoiceNumber':number,'date':'2026-11-12','buyerName':'虚构大会','buyerTaxId':'91510000TEST000001A','sellerName':'虚构交通服务公司','sellerTaxId':'91510100TEST000002B','netAmount':120,'taxAmount':3.45,'note':'测试数据'}],'note':'仅用于自动验证','consent':True}
    def test_complete_flow_and_security(self):
        anonymous=self.client()
        self.assertEqual(self.req(anonymous,'/api/admin/submissions')[0],401)
        self.assertEqual(self.req(anonymous,'/api/admin/export.xlsx')[0],401)
        self.assertEqual(self.req(anonymous,'/api/admin/login',{'password':'synthetic-test-password-34'},headers={'Origin':'https://evil.example'})[0],403)
        guest=self.guest();file=self.upload(guest,'12345678901234567890')
        self.assertEqual(file['ocr']['status'],'done');self.assertEqual(file['ocr']['fields']['amount'],123.45)
        other=self.guest()
        self.assertEqual(self.req(other,'/api/uploads/'+file['id']+'/recognize',{})[0],404)
        status,reprocessed=self.req(guest,'/api/uploads/'+file['id']+'/recognize',{})
        self.assertEqual(status,200);self.assertEqual(reprocessed['ocr']['fields']['amount'],123.45)
        # Re-checking the invite must preserve ownership of files already uploaded.
        self.assertEqual(self.req(guest,'/api/guest/session',{'inviteCode':'TESTINVITE'})[0],200)
        self.assertEqual(self.req(guest,'/api/files/'+file['id'])[0],200)
        self.assertEqual(self.req(other,'/api/files/'+file['id'])[0],403)
        self.assertEqual(self.req(other,'/api/submissions',self.payload(file,'12345678901234567890'))[0],400)
        self.assertEqual(self.req(guest,'/api/uploads',b'<script>evil</script>',headers={'Content-Type':'image/png','X-Filename':'bad.png'})[0],415)
        body=self.payload(file,'12345678901234567890');body['expenses'][0]['amount']='NaN'
        self.assertEqual(self.req(guest,'/api/submissions',body)[0],400)
        supporting=self.upload(guest,'SUPPORT12345678901234','support')
        body=self.payload(file,'12345678901234567890');body['supportingFiles']=[supporting['id']]
        status,submitted=self.req(guest,'/api/submissions',body,headers={'Idempotency-Key':'flow-test-1'})
        self.assertEqual(status,201,submitted);self.assertEqual(submitted['totalAmount'],123.45)
        self.assertNotIn('payment',submitted)
        self.assertNotIn('0012345678901234',json.dumps(submitted))
        self.assertEqual(self.req(guest,'/api/submissions',body,headers={'Idempotency-Key':'flow-test-1'})[0],200)
        self.assertEqual(self.req(guest,'/api/uploads/'+file['id'],method='DELETE')[0],409)
        self.assertEqual(self.req(guest,'/api/uploads/'+file['id']+'/recognize',{})[0],409)
        lookup={'reference':submitted['reference'],'mobile':'13800000000'}
        status,result=self.req(anonymous,'/api/guest/lookup',lookup);self.assertEqual(status,200);self.assertNotIn('file',result['expenses'][0]);self.assertNotIn('supportingFiles',result);self.assertEqual(result['applicant']['mobile'],'138****0000')
        self.assertEqual(result['payment']['accountNumber'],'****1234')
        self.assertEqual(result['payment']['accountName'],'测**')
        self.assertEqual(self.req(anonymous,'/api/guest/lookup',{**lookup,'mobile':'13900000000'})[0],404)
        admin=self.client();self.assertEqual(self.req(admin,'/api/admin/login',{'password':'synthetic-test-password-34'})[0],200)
        status,result=self.req(admin,'/api/admin/submissions');self.assertEqual(status,200)
        item=next(x for x in result['items'] if x['reference']==submitted['reference'])
        self.assertEqual(len(item['supportingFiles']),1)
        self.assertEqual(item['expenses'][0]['sellerTaxId'],'91510100TEST000002B')
        self.assertEqual(item['expenses'][0]['sellerName'],'虚构交通服务公司')
        self.assertEqual(item['expenses'][0]['netAmount'],120)
        self.assertEqual(item['payment']['accountNumber'],'0012345678901234')
        self.assertEqual(self.req(admin,'/api/files/'+file['id'])[0],200)
        self.assertEqual(self.req(admin,'/api/admin/submissions/'+item['id'],{'status':'needs_info','reviewNote':''},method='PATCH')[0],400)
        self.assertEqual(self.req(admin,'/api/admin/submissions/'+item['id'],{'status':'approved','reviewNote':'测试审核通过'},method='PATCH')[0],200)
        status,xlsx=self.req(admin,'/api/admin/export.xlsx');self.assertEqual(status,200)
        from openpyxl import load_workbook
        workbook=load_workbook(io.BytesIO(xlsx));self.assertEqual(workbook.active.cell(2,2).value,'测试嘉宾');self.assertEqual(workbook.active.cell(2,7).value,123.45)
        self.assertEqual(workbook.active.cell(2,19).value,'91510100TEST000002B');self.assertEqual(workbook.active.cell(2,20).value,120)
        self.assertEqual(workbook.active.cell(2,23).value,'0012345678901234');self.assertEqual(workbook.active.cell(2,23).data_type,'s')
        status,raw=self.req(admin,'/api/admin/export.zip');self.assertEqual(status,200)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            self.assertTrue(any(x.endswith('synthetic-invoice.pdf') for x in z.namelist()))
            self.assertTrue(any('补充附件' in x for x in z.namelist()))
        self.assertEqual(self.req(admin,'/api/admin/logout',{})[0],200);self.assertEqual(self.req(admin,'/api/admin/submissions')[0],401)
        # A second guest cannot re-submit an identical file.
        self.assertEqual(self.req(other,'/api/uploads',self.pdf('12345678901234567890'),headers={'Content-Type':'application/pdf','X-Filename':'duplicate.pdf'})[0],409)

    def test_payment_roundtrips_exports_and_category_storage(self):
        from openpyxl import load_workbook
        admin=self.client();self.assertEqual(self.req(admin,'/api/admin/login',{'password':'synthetic-test-password-34'})[0],200)
        payments=[{'method':'bank','accountName':'银行测试嘉宾','accountNumber':'0000 1234 5678 9012','bankName':'虚构银行','bankBranch':'=虚构测试支行'},
                  {'method':'alipay','accountName':'支付宝测试嘉宾','accountNumber':'guest.finance+test@example.com','bankName':'此项应清空','bankBranch':'此项也应清空'}]
        for index,payment in enumerate(payments):
            with self.subTest(method=payment['method']):
                guest=self.guest();numbers=[f'PAY{index}2026000000000{n}' for n in range(5 if index==0 else 1)]
                files=[self.upload(guest,number) for number in numbers]
                body=self.payload(files[0],numbers[0]);body['payment']=payment;body['expenses']=[]
                for n,(file,number) in enumerate(zip(files,numbers)):
                    e=self.payload(file,number)['expenses'][0]
                    e.update(category=['rail','flight','taxi','hotel','other'][n],amount=10+n,netAmount=10+n,taxAmount=0)
                    body['expenses'].append(e)
                account=payment['accountNumber'].replace(' ','')
                body['note']='原始测试账号 '+payment['accountNumber']+'；连续账号 '+account
                status,submitted=self.req(guest,'/api/submissions',body)
                self.assertEqual(status,201,submitted);self.assertNotIn(account,json.dumps(submitted));self.assertNotIn('payment',submitted)
                self.assertEqual(submitted['expenseCount'],len(files))
                self.assertEqual(self.req(guest,'/api/admin/submissions')[0],401)
                self.assertEqual(self.req(guest,'/api/admin/export.xlsx')[0],401)
                status,looked_up=self.req(self.client(),'/api/guest/lookup',{'reference':submitted['reference'],'mobile':'13800000000'})
                self.assertEqual(status,200,looked_up);self.assertNotIn(account,json.dumps(looked_up));self.assertNotIn(payment['accountNumber'],json.dumps(looked_up))
                self.assertNotEqual(looked_up['payment']['accountNumber'],account)
                status,listed=self.req(admin,'/api/admin/submissions?q='+submitted['reference'])
                self.assertEqual(status,200);self.assertEqual(len(listed['items']),1)
                item=listed['items'][0];expected=payment_fields(payment)
                self.assertEqual(item['payment'],expected)
                self.assertEqual([e['category'] for e in item['expenses']],['rail','flight','taxi','hotel','other'][:len(files)])
                self.assertEqual([e['amount'] for e in item['expenses']],list(range(10,10+len(files))))
                if index:
                    self.assertEqual(item['payment']['bankName'],'');self.assertEqual(item['payment']['bankBranch'],'')
                self.assertEqual(self.req(guest,'/api/admin/submissions/'+item['id'],{'status':'paid','reviewNote':''},method='PATCH')[0],401)
                status,reviewed=self.req(admin,'/api/admin/submissions/'+item['id'],{'status':'approved','reviewNote':'收款核对 '+account},method='PATCH')
                self.assertEqual(status,200);self.assertEqual(reviewed['payment'],expected)
                # A finance comment repeating the account must remain masked in public lookup.
                status,looked_up=self.req(self.client(),'/api/guest/lookup',{'reference':submitted['reference'],'mobile':'13800000000'})
                self.assertEqual(status,200);self.assertNotIn(account,json.dumps(looked_up))
                status,raw=self.req(admin,'/api/admin/export.xlsx?q='+submitted['reference']);self.assertEqual(status,200)
                book=load_workbook(io.BytesIO(raw))
                for sheet,start in [(book['报销明细'],21),(book['申请汇总'],10)]:
                    self.assertEqual([sheet.cell(1,start+i).value for i in range(5)],['收款方式','收款人','账号','银行','支行'])
                    self.assertEqual(sheet.cell(2,start+1).value,payment['accountName'])
                    self.assertEqual(sheet.cell(2,start+2).value,account);self.assertEqual(sheet.cell(2,start+2).data_type,'s')
                    self.assertEqual(sheet.cell(2,start+3).value or '',expected['bankName'])
                    self.assertEqual(sheet.cell(2,start+4).value or '',expected['bankBranch'])
                    if expected['bankBranch']:self.assertEqual(sheet.cell(2,start+4).data_type,'s')
                status,audit=self.req(admin,'/api/admin/audit');self.assertEqual(status,200)
                self.assertNotIn(account,json.dumps(audit))

    def test_payment_validation_and_phone_forms(self):
        guest=self.guest();file=self.upload(guest,'VALIDPAY202600000001');base=self.payload(file,'VALIDPAY202600000001')
        invalid=[None,{},[],{'method':'cash'},{'method':{}},
                 {**base['payment'],'accountName':''},{**base['payment'],'accountNumber':''},
                 {**base['payment'],'bankName':''},{**base['payment'],'accountNumber':123456789012},
                 {**base['payment'],'accountNumber':'12345678901'},{**base['payment'],'accountNumber':'1'*26},
                 {**base['payment'],'accountNumber':'12345678901X'},
                 {'method':'alipay','accountName':'测试','accountNumber':'not-an-account'},
                 {'method':'alipay','accountName':'测试','accountNumber':'1380000000'},
                 {'method':'alipay','accountName':'测试','accountNumber':'a..b@example.com'},
                 {'method':'alipay','accountName':'测试','accountNumber':'a@-invalid.com'}]
        for payment in invalid:
            with self.subTest(payment=payment):
                body=deepcopy(base);body['payment']=payment
                status,result=self.req(guest,'/api/submissions',body);self.assertEqual(status,400,result)
                self.assertNotIn('123456789012',json.dumps(result))
        body=deepcopy(base);body.pop('payment')
        self.assertEqual(self.req(guest,'/api/submissions',body)[0],400)
        for account in ('13800000000','+8613800000000','8613800000000','guest+vip@example.com'):
            clean=payment_fields({'method':'alipay','accountName':'测试','accountNumber':account,'bankName':{'ignore':'irrelevant'},'bankBranch':None})
            self.assertEqual(clean['accountNumber'],account);self.assertEqual(clean['bankName'],'');self.assertEqual(clean['bankBranch'],'')
        # Failed validation leaves the original private upload available for a corrected submission.
        self.assertEqual(self.req(guest,'/api/files/'+file['id'])[0],200)
        body=deepcopy(base);body['payment']={'method':'alipay','accountName':'测试嘉宾','accountNumber':'+8613800000000'}
        self.assertEqual(self.req(guest,'/api/submissions',body)[0],201)

    def test_payment_migration_preserves_legacy_records(self):
        work=ROOT.parent.parent/'work'
        with tempfile.TemporaryDirectory(prefix='migration-tests-',dir=work) as tmp:
            path=Path(tmp)/'records.sqlite3'
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE submissions (id TEXT PRIMARY KEY,reference TEXT UNIQUE NOT NULL,owner TEXT NOT NULL,applicant TEXT NOT NULL,note TEXT NOT NULL,total_cents INTEGER NOT NULL,status TEXT NOT NULL,review_note TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,idempotency_key TEXT)")
                db.execute('INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?,?,?,?)',('a'*32,'NJ-LEGACY','old-owner',json.dumps({'name':'旧测试嘉宾','mobile':'13800000000','organization':'','contactPerson':''}),'原记录',100,'pending','','old-time','old-time',None))
            app=App(tmp)
            with app.db() as db:
                row=db.execute('SELECT * FROM submissions').fetchone()
                self.assertEqual(row['reference'],'NJ-LEGACY');self.assertEqual(row['note'],'原记录');self.assertEqual(row['total_cents'],100)
                self.assertEqual(row['payment'],'{}');self.assertEqual(app.submission(row,db)['payment'],{});self.assertEqual(row['deleted_at'],'');self.assertEqual(len(app.list_items({})),1)
            # Re-opening the migrated database neither resets records nor repeats ALTER TABLE.
            app=App(tmp)
            with app.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM submissions').fetchone()[0],1)

    def test_payment_proof_validation_archive_and_required_organization(self):
        from openpyxl import load_workbook
        guest=self.guest();uid=str(uuid4());invoice=self.upload(guest,'PROOF20260000000001',uid=uid)
        with patch('server.recognize',side_effect=AssertionError('attachments must not call invoice OCR')):
            first=self.proof(guest,uid,11);second=self.proof(guest,uid,12)
            supporting=self.upload(guest,'PROOFSUPPORT20260001','support')
        self.assertEqual(first['ocr']['status'],'skipped');self.assertEqual(first['purpose'],'payment-proof');self.assertEqual(first['clientRef'],uid)
        self.assertEqual(supporting['ocr']['status'],'skipped')
        self.assertEqual(self.req(guest,'/api/uploads/'+first['id']+'/recognize',{})[0],400)
        self.assertEqual(self.req(guest,'/api/uploads',self.pdf('BADPROOFPDF'),headers={'Content-Type':'application/pdf','X-Filename':'proof.pdf','X-Upload-Purpose':'payment-proof'})[0],415)
        self.assertEqual(self.req(guest,'/api/uploads',self.pdf('BADPURPOSE'),headers={'Content-Type':'application/pdf','X-Filename':'proof.pdf','X-Upload-Purpose':'unknown'})[0],400)
        self.assertEqual(self.req(guest,'/api/uploads',self.pdf('BADCLIENTREF'),headers={'Content-Type':'application/pdf','X-Filename':'proof.pdf','X-Draft-Entry':'not-a-uuid'})[0],400)
        body=self.payload(invoice,'PROOF20260000000001');body['expenses'][0]['paymentProofIds']=[first['id'],second['id']];body['supportingFiles']=[supporting['id']]
        bad=deepcopy(body);bad['applicant']['organization']=' '
        self.assertEqual(self.req(guest,'/api/submissions',bad)[0],400)
        bad=deepcopy(body);bad['expenses'][0]['paymentProofIds']=[first['id']]*4
        self.assertEqual(self.req(guest,'/api/submissions',bad)[0],400)
        bad=deepcopy(body);bad['expenses'][0]['paymentProofIds']=[invoice['id']]
        self.assertEqual(self.req(guest,'/api/submissions',bad)[0],400)
        bad=deepcopy(body);bad['expenses']=bad['expenses']*12
        self.assertEqual(self.req(guest,'/api/submissions',bad)[0],400)
        other=self.guest();foreign=self.proof(other,str(uuid4()),13)
        self.assertEqual(self.req(other,'/api/files/'+first['id'])[0],403)
        self.assertEqual(self.req(other,'/api/uploads/'+first['id'],method='DELETE')[0],403)
        bad=deepcopy(body);bad['expenses'][0]['paymentProofIds']=[foreign['id']]
        self.assertEqual(self.req(guest,'/api/submissions',bad)[0],400)
        status,submitted=self.req(guest,'/api/submissions',body)
        self.assertEqual(status,201,submitted);self.assertEqual(submitted['totalAmount'],123.45);self.assertEqual(submitted['expenseCount'],1)
        self.assertEqual(self.req(guest,'/api/uploads/'+first['id'],method='DELETE')[0],409)
        status,looked_up=self.req(self.client(),'/api/guest/lookup',{'reference':submitted['reference'],'mobile':'13800000000'})
        self.assertEqual(status,200);self.assertNotIn('paymentProofs',looked_up['expenses'][0]);self.assertNotIn(first['id'],json.dumps(looked_up))
        admin=self.client();self.assertEqual(self.req(admin,'/api/admin/login',{'password':'synthetic-test-password-34'})[0],200)
        status,listed=self.req(admin,'/api/admin/submissions?q='+submitted['reference']);self.assertEqual(status,200)
        item=listed['items'][0]
        self.assertEqual([p['id'] for p in item['expenses'][0]['paymentProofs']],[first['id'],second['id']])
        self.assertEqual([p['id'] for p in item['supportingFiles']],[supporting['id']])
        self.assertEqual(self.req(admin,'/api/files/'+first['id'])[0],200)
        status,raw=self.req(admin,'/api/admin/export.xlsx?q='+submitted['reference']);self.assertEqual(status,200)
        book=load_workbook(io.BytesIO(raw))
        for sheet in book:
            self.assertEqual(sheet.cell(1,sheet.max_column).value,'付款凭证截图文件名')
            self.assertIn(first['name'],sheet.cell(2,sheet.max_column).value)
            self.assertIn(second['name'],sheet.cell(2,sheet.max_column).value)
        status,raw=self.req(admin,'/api/admin/export.zip?q='+submitted['reference']);self.assertEqual(status,200)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            self.assertEqual(len([name for name in z.namelist() if '/付款凭证/' in name]),2)
            self.assertEqual(len([name for name in z.namelist() if '/补充附件/' in name]),1)

    def test_draft_partial_refresh_sanitization_and_guest_owner_link(self):
        client=self.client();session=self.draft_session(client)
        self.assertIsNone(session['draft']);self.assertEqual(session['revision'],0);self.assertFalse(session['authenticated'])
        cookie=next(c for c in client.jar if c.name=='vip_draft')
        self.assertIn('HttpOnly',cookie._rest);self.assertEqual(cookie._rest.get('SameSite'),'Strict')
        self.assertGreater(cookie.expires,time.time()+DRAFT_LIFETIME-60)
        client_id=str(uuid4());uid=str(uuid4())
        partial={'version':1,'step':2,'applicant':{'name':'尚未填完','organization':'','password':'hidden-secret'},'payment':{'accountNumber':'0012','password':'hidden-secret'},'expenses':[{'uid':uid,'amount':'12.','uploading':True}],'supportingFiles':[],'note':'草稿测试','submissionKey':str(uuid4()),'inviteCode':'hidden-secret','consent':True,'admin':'hidden-secret','password':'hidden-secret','uploading':True}
        status,saved=self.save(client,partial,client_id);self.assertEqual(status,200,saved);self.assertEqual(saved['revision'],1)
        self.assertEqual(saved['draft']['applicant']['organization'],'');self.assertEqual(saved['draft']['payment']['accountNumber'],'0012')
        self.assertEqual(saved['draft']['expenses'][0]['amount'],'12.');self.assertNotIn('hidden-secret',json.dumps(saved));self.assertNotIn('uploading',json.dumps(saved))
        status,reloaded=self.req(client,'/api/guest/draft');self.assertEqual(status,200);self.assertEqual(reloaded['draft'],saved['draft'])
        self.assertEqual(self.req(client,'/api/guest/session',{'inviteCode':'TESTINVITE'})[0],200)
        invoice=self.upload(client,'DRAFTLINK20260000001',uid=uid)
        proof=self.proof(client,uid,21)
        partial['expenses'][0].update(fileId=invoice['id'],paymentProofIds=[proof['id']])
        status,saved=self.save(client,partial,client_id,sequence=2,base=1);self.assertEqual(status,200,saved);self.assertTrue(saved['authenticated'])
        self.assertEqual({u['id'] for u in saved['uploads']},{invoice['id'],proof['id']})
        self.assertTrue(all(u['clientRef']==uid for u in saved['uploads']))
        repeated=self.draft_session(client);self.assertEqual(repeated['draft'],saved['draft']);self.assertTrue(repeated['authenticated'])
        self.assertEqual(self.save(client,{'expenses':[{'paymentProofIds':[proof['id']]*4}]},client_id,sequence=3,base=2)[0],400)
        self.assertEqual(self.save(client,{'note':'x'*2001},client_id,sequence=3,base=2)[0],400)
        self.assertEqual(self.req(client,'/api/guest/draft',{'draft':{},'clientId':'invalid','sequence':3,'baseRevision':2})[0],400)
        self.assertEqual(self.req(client,'/api/guest/draft',{'draft':{},'clientId':client_id,'sequence':True,'baseRevision':2})[0],400)
        # Deleting an upload invalidates its saved references without exposing any new metadata.
        self.assertEqual(self.req(client,'/api/uploads/'+proof['id'],method='DELETE')[0],200)
        status,cleaned=self.req(client,'/api/guest/draft');self.assertEqual(status,200);self.assertNotIn(proof['id'],json.dumps(cleaned))
        with self.server.app.db() as db:
            guest_owners={r[0] for r in db.execute('SELECT owner FROM sessions WHERE token IN (?,?)',(digest(next(c.value for c in client.jar if c.name=='vip_draft')),digest(next(c.value for c in client.jar if c.name=='vip_guest'))))}
            self.assertEqual(len(guest_owners),1)

    def test_draft_sequence_conflict_and_cross_guest_isolation(self):
        first=self.guest();self.draft_session(first);invoice=self.upload(first,'DRAFTORDER2026000001')
        client_a,client_b=str(uuid4()),str(uuid4())
        status,one=self.save(first,{'note':'一','expenses':[{'fileId':invoice['id']}]},client_a);self.assertEqual(status,200)
        status,three=self.save(first,{'note':'三','expenses':[{'fileId':invoice['id']}]},client_a,sequence=3,base=0);self.assertEqual(status,200);self.assertEqual(three['revision'],2)
        status,late=self.save(first,{'note':'二'},client_a,sequence=2,base=0);self.assertEqual(status,200);self.assertTrue(late['ignored']);self.assertEqual(late['draft']['note'],'三');self.assertEqual(late['revision'],2)
        status,conflict=self.save(first,{'note':'其他页面'},client_b,sequence=1,base=1);self.assertEqual(status,409);self.assertEqual(conflict['error'],'draft_conflict');self.assertEqual(conflict['revision'],2)
        status,changed=self.save(first,{'note':'其他页面'},client_b,sequence=1,base=2);self.assertEqual(status,200);self.assertEqual(changed['revision'],3)
        other=self.guest();self.draft_session(other)
        status,foreign=self.save(other,{'expenses':[{'fileId':invoice['id'],'paymentProofIds':[invoice['id']]}],'supportingFiles':[invoice['id']]})
        self.assertEqual(status,200);self.assertNotIn(invoice['id'],json.dumps(foreign));self.assertEqual(foreign['uploads'],[])
        self.assertEqual(self.req(other,'/api/files/'+invoice['id'])[0],403)
        status,unchanged=self.req(first,'/api/guest/draft');self.assertEqual(status,200);self.assertEqual(unchanged['draft']['note'],'其他页面')
        anonymous=self.client();self.assertEqual(self.req(anonymous,'/api/guest/draft')[0],401)
        self.assertIsNone(self.draft_session(anonymous)['draft'])

    def test_draft_expiration_and_guest_reauthentication(self):
        client=self.guest();self.draft_session(client);invoice=self.upload(client,'DRAFTEXPIRY20260001')
        status,saved=self.save(client,{'note':'过期测试','expenses':[{'fileId':invoice['id']}]});self.assertEqual(status,200)
        draft_token=digest(next(c.value for c in client.jar if c.name=='vip_draft'))
        with self.server.app.db() as db:
            owner=db.execute('SELECT owner FROM sessions WHERE token=?',(draft_token,)).fetchone()[0]
            db.execute('UPDATE sessions SET expires=? WHERE owner=? AND kind=?',(time.time()-1,owner,'guest'))
        status,reloaded=self.req(client,'/api/guest/draft');self.assertEqual(status,200);self.assertFalse(reloaded['authenticated']);self.assertEqual(reloaded['uploads'][0]['id'],invoice['id'])
        self.assertEqual(self.req(client,'/api/guest/session',{'inviteCode':'TESTINVITE'})[0],200)
        self.assertEqual(self.req(client,'/api/files/'+invoice['id'])[0],200)
        with self.server.app.db() as db:
            self.assertEqual(db.execute('SELECT owner FROM sessions WHERE token=?',(digest(next(c.value for c in client.jar if c.name=='vip_guest')),)).fetchone()[0],owner)
            db.execute('UPDATE drafts SET updated_at=? WHERE owner=?',('2000-01-01T00:00:00+00:00',owner))
        status,expired=self.req(client,'/api/guest/draft');self.assertEqual(status,200);self.assertIsNone(expired['draft']);self.assertEqual(expired['revision'],0)
        with self.server.app.db() as db:db.execute('UPDATE sessions SET expires=? WHERE token=?',(time.time()-1,draft_token))
        self.assertEqual(self.req(client,'/api/guest/draft')[0],401)
        fresh=self.client();self.assertIsNone(self.draft_session(fresh)['draft'])

    def test_draft_submission_clears_content_and_rejects_late_save(self):
        client=self.client();self.draft_session(client);self.assertEqual(self.req(client,'/api/guest/session',{'inviteCode':'TESTINVITE'})[0],200)
        uid=str(uuid4());invoice=self.upload(client,'DRAFTCOMMIT20260001',uid=uid);proof=self.proof(client,uid,31)
        body=self.payload(invoice,'DRAFTCOMMIT20260001');body['expenses'][0]['uid']=uid;body['expenses'][0]['paymentProofIds']=[proof['id']]
        client_id,key=str(uuid4()),str(uuid4());draft={**body,'submissionKey':key,'version':1,'step':3}
        status,saved=self.save(client,draft,client_id);self.assertEqual(status,200,saved)
        status,submitted=self.req(client,'/api/submissions',body,headers={'Idempotency-Key':key});self.assertEqual(status,201,submitted)
        status,cleared=self.req(client,'/api/guest/draft');self.assertEqual(status,200);self.assertIsNone(cleared['draft']);self.assertEqual(cleared['uploads'],[])
        status,late=self.save(client,draft,client_id,sequence=2,base=1);self.assertEqual(status,200,late);self.assertTrue(late['committed']);self.assertIsNone(late['draft']);self.assertEqual(late['uploads'],[])
        with self.server.app.db() as db:
            owner=db.execute('SELECT owner FROM submissions WHERE reference=?',(submitted['reference'],)).fetchone()[0]
            self.assertEqual(db.execute('SELECT COUNT(*) FROM drafts WHERE owner=?',(owner,)).fetchone()[0],0)
        # A fresh application can still be saved; old committed payloads cannot recreate their content.
        status,new=self.save(client,{'note':'下一份','submissionKey':str(uuid4())},str(uuid4()),sequence=1,base=0);self.assertEqual(status,200,new)
        status,late=self.save(client,draft,client_id,sequence=3,base=1);self.assertEqual(status,200);self.assertTrue(late['committed'])
        status,latest=self.req(client,'/api/guest/draft');self.assertEqual(status,200);self.assertEqual(latest['draft']['note'],'下一份')

    def test_default_invite_persists_and_env_override_preserves_admin(self):
        work=ROOT.parent.parent/'work'
        with tempfile.TemporaryDirectory(prefix='invite-tests-',dir=work) as tmp,patch.dict(os.environ):
            os.environ.pop('ADMIN_PASSWORD',None);os.environ.pop('GUEST_INVITE_CODE',None)
            app=App(tmp)
            self.assertEqual(DEFAULT_INVITE_CODE,'01AI2026VIP');self.assertTrue(password_ok(DEFAULT_INVITE_CODE,app.config['invite_hash']))
            admin_hash=app.config['admin_hash'];invite_hash=app.config['invite_hash']
            self.assertTrue(password_ok(app.generated['管理员密码'],admin_hash))
            repeated=App(tmp);self.assertEqual(repeated.config['admin_hash'],admin_hash);self.assertEqual(repeated.config['invite_hash'],invite_hash)
            self.assertNotIn(DEFAULT_INVITE_CODE,Path(tmp,'config.json').read_text())
            os.environ['GUEST_INVITE_CODE']='CUSTOM-VIP-CODE'
            overridden=App(tmp);self.assertTrue(password_ok('CUSTOM-VIP-CODE',overridden.config['invite_hash']));self.assertEqual(overridden.config['admin_hash'],admin_hash)
            os.environ.pop('GUEST_INVITE_CODE')
            persisted=App(tmp);self.assertTrue(password_ok('CUSTOM-VIP-CODE',persisted.config['invite_hash']));self.assertEqual(persisted.config['admin_hash'],admin_hash)

    def test_phone_only_lookup_minimal_multiple_records_and_normalization(self):
        records=[];files=[]
        for index,mobile in enumerate(('+86 139-1111-2222','13911112222','13733334444')):
            guest=self.guest();number=f'PHONELOOKUP20260000{index}'
            file=self.upload(guest,number);files.append(file);body=self.payload(file,number);body['applicant']['mobile']=mobile
            status,submitted=self.req(guest,'/api/submissions',body);self.assertEqual(status,201,submitted);records.append(submitted)
        with self.server.app.db() as db:
            rows=[db.execute('SELECT * FROM submissions WHERE reference=?',(item['reference'],)).fetchone() for item in records]
            # Include a historical unnormalized number to verify compatibility.
            applicant=json.loads(rows[0]['applicant']);applicant['mobile']='+86 139-1111-2222'
            db.execute('UPDATE submissions SET applicant=?,created_at=?,updated_at=?,status=?,review_note=? WHERE id=?',
                       (json.dumps(applicant),'2026-11-11T01:00:00+00:00','2026-11-12T01:00:00+00:00','approved','已核对',rows[0]['id']))
            note='请重新核对，账号 0012 3456 7890 1234，内部编号 '+rows[1]['reference']+'，文件 '+files[1]['id']
            db.execute('UPDATE submissions SET created_at=?,updated_at=?,status=?,review_note=? WHERE id=?',
                       ('2026-11-12T01:00:00+00:00','2026-11-13T01:00:00+00:00','needs_info',note,rows[1]['id']))
        anonymous=self.client();status,looked_up=self.req(anonymous,'/api/guest/lookup',{'mobile':'+86 139-1111-2222'})
        self.assertEqual(status,200,looked_up);self.assertEqual(set(looked_up),{'items'});self.assertEqual(len(looked_up['items']),2)
        expected={'status','totalAmount','expenseCount','createdAt','updatedAt','reviewNote'}
        self.assertTrue(all(set(item)==expected for item in looked_up['items']))
        self.assertEqual([item['status'] for item in looked_up['items']],['needs_info','approved'])
        self.assertEqual([item['expenseCount'] for item in looked_up['items']],[1,1]);self.assertEqual(looked_up['items'][0]['totalAmount'],123.45)
        raw=json.dumps(looked_up,ensure_ascii=False)
        for private in ('0012345678901234','0012 3456 7890 1234','测试嘉宾','虚构测试机构',records[0]['reference'],records[1]['reference'],rows[0]['id'],rows[1]['id'],files[1]['id']):self.assertNotIn(private,raw)
        status,legacy=self.req(anonymous,'/api/guest/lookup',{'mobile':'8613911112222','reference':records[0]['reference']});self.assertEqual(status,200);self.assertEqual(legacy['status'],'approved')
        status,other=self.req(anonymous,'/api/guest/lookup',{'mobile':'13733334444'});self.assertEqual(status,200);self.assertEqual(len(other['items']),1);self.assertEqual(other['items'][0]['status'],'pending')
        status,missing=self.req(anonymous,'/api/guest/lookup',{'mobile':'13655556666'});self.assertEqual(status,200);self.assertEqual(missing,{'items':[]})
        self.assertEqual(self.req(anonymous,'/api/guest/lookup',{'mobile':''})[0],400)
        self.assertEqual(self.req(anonymous,'/api/guest/lookup',{'mobile':'invalid'})[0],400)
        self.assertEqual(self.req(anonymous,'/api/guest/lookup',{})[0],400)

    def test_phone_only_lookup_rate_limit_is_preserved(self):
        anonymous=self.client()
        for _ in range(8):self.assertEqual(self.req(anonymous,'/api/guest/lookup',{'mobile':'13555556666'})[0],200)
        status,result=self.req(anonymous,'/api/guest/lookup',{'mobile':'13555556666'});self.assertEqual(status,429);self.assertEqual(result['error'],'rate_limit')

    def test_concurrent_upload_rechecks_capacity_and_hash_after_ocr(self):
        for mode in ('capacity','duplicate'):
            with self.subTest(mode=mode):
                guest=self.guest()
                if mode=='capacity':
                    for index in range(11):self.upload(guest,f'CAPACITYSEED2026{index:02d}','support')
                barrier=threading.Barrier(2)
                def overlapping_ocr(path,mime):
                    barrier.wait(timeout=10)
                    return {'status':'done','engine':'synthetic-concurrency-test','fields':{},'recognizedFields':[],'warnings':[],'message':''}
                raws=[self.pdf('OVERLAP'+mode+str(index if mode=='capacity' else 0)) for index in range(2)]
                before_files=set(self.server.app.files.iterdir())
                def upload(index):
                    return self.req(guest,'/api/uploads',raws[index],headers={'Content-Type':'application/pdf','X-Filename':f'concurrent-{index}.pdf'})
                with patch('server.recognize',side_effect=overlapping_ocr) as recognition,ThreadPoolExecutor(max_workers=2) as executor:
                    futures=[executor.submit(upload,index) for index in range(2)]
                    results=[future.result(timeout=20) for future in futures]
                self.assertEqual(recognition.call_count,2)
                self.assertEqual(sorted(status for status,result in results),[201,400 if mode=='capacity' else 409],results)
                success=next(result for status,result in results if status==201)
                with self.server.app.db() as db:
                    owner=db.execute('SELECT owner FROM uploads WHERE id=?',(success['id'],)).fetchone()[0]
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM uploads WHERE owner=? AND submission_id IS NULL',(owner,)).fetchone()[0],12 if mode=='capacity' else 1)
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM uploads WHERE owner=? AND name LIKE ?',(owner,'concurrent-%')).fetchone()[0],1)
                # The losing request must remove its temporary file after rollback.
                self.assertEqual(set(self.server.app.files.iterdir())-before_files,{self.server.app.files/success['id']})

    def test_failed_ocr_upload_removes_private_file(self):
        guest=self.guest();before_files=set(self.server.app.files.iterdir())
        with self.server.app.db() as db:before_rows=db.execute('SELECT COUNT(*) FROM uploads').fetchone()[0]
        with patch('server.recognize',side_effect=RuntimeError('synthetic OCR failure')):
            status,result=self.req(guest,'/api/uploads',self.pdf('FAILURECLEANUP2026'),headers={'Content-Type':'application/pdf','X-Filename':'ocr-error.pdf'})
        self.assertEqual(status,500);self.assertEqual(result['error'],'server_error')
        self.assertEqual(set(self.server.app.files.iterdir()),before_files)
        with self.server.app.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM uploads').fetchone()[0],before_rows)

    def test_upload_rechecks_another_guests_submission_while_ocr_runs(self):
        first=self.guest();number='CROSSSUBMISSION2026';file=self.upload(first,number);second=self.guest()
        started,release=threading.Event(),threading.Event()
        def blocked_ocr(path,mime):
            started.set()
            if not release.wait(10):raise RuntimeError('test did not release OCR')
            return {'status':'done','fields':{}}
        before_files=set(self.server.app.files.iterdir())
        with patch('server.recognize',side_effect=blocked_ocr),ThreadPoolExecutor(max_workers=1) as executor:
            future=executor.submit(self.req,second,'/api/uploads',self.pdf(number),None,{'Content-Type':'application/pdf','X-Filename':'concurrently-submitted.pdf'})
            try:
                self.assertTrue(started.wait(5))
                # The first guest can commit while OCR remains blocked: OCR holds no writer lock.
                status,submitted=self.req(first,'/api/submissions',self.payload(file,number));self.assertEqual(status,201,submitted)
            finally:release.set()
            status,result=future.result(timeout=15)
        self.assertEqual(status,409,result);self.assertEqual(result['error'],'duplicate_file')
        self.assertEqual(set(self.server.app.files.iterdir()),before_files)
    def test_parser_is_conservative(self):
        fields=extract_fields('发票号码：12345678901234567890\n开票日期：2026年11月12日\n购买方名称：虚构内江大会\n购买方税号：91510000TEST000001A\n价税合计（小写）￥123.45\n税额合计：3.45')
        self.assertEqual(fields['buyerName'],'虚构内江大会');self.assertEqual(fields['date'],'2026-11-12');self.assertEqual(fields['amount'],123.45)
        self.assertEqual(extract_fields('nothing legible')['amount'],'')
    def test_logout_consumes_request_body_on_persistent_connection(self):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=5)
        connection.request('POST','/api/admin/login',json.dumps({'password':'synthetic-test-password-34'}),{'Content-Type':'application/json'})
        response=connection.getresponse();self.assertEqual(response.status,200);cookie=response.getheader('Set-Cookie').split(';')[0];response.read()
        connection.request('POST','/api/admin/logout','{}',{'Content-Type':'application/json','Cookie':cookie})
        response=connection.getresponse();self.assertEqual(response.status,200);response.read()
        connection.request('GET','/api/config')
        response=connection.getresponse();self.assertEqual(response.status,200);response.read();connection.close()

if __name__=='__main__':unittest.main()
