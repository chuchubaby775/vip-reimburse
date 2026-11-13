"""Deletion uses an independent synthetic database; no live records are touched."""
import io
import unittest
import zipfile
from uuid import uuid4
import test_backend as fixtures

class AdminDeletionTests(unittest.TestCase):
    setUpClass=classmethod(fixtures.BackendTests.setUpClass.__func__)
    tearDownClass=classmethod(fixtures.BackendTests.tearDownClass.__func__)
    setUp=fixtures.BackendTests.setUp
    client=fixtures.BackendTests.client
    req=fixtures.BackendTests.req
    pdf=fixtures.BackendTests.pdf
    guest=fixtures.BackendTests.guest
    upload=fixtures.BackendTests.upload
    proof=fixtures.BackendTests.proof
    payload=fixtures.BackendTests.payload

    def admin(self):
        client=self.client();self.assertEqual(self.req(client,'/api/admin/login',{'password':'synthetic-test-password-34'})[0],200);return client
    def find(self,admin,reference,trash=False):
        return self.req(admin,'/api/admin/submissions?q='+reference+('&deleted=1' if trash else ''))[1]
    def create(self,guest,number,invoice_number=None,extras=False):
        uid=str(uuid4());file=self.upload(guest,number,uid=uid);body=self.payload(file,invoice_number or number);body['applicant']['mobile']='13900001111';files=[file]
        if extras:
            proof=self.proof(guest,uid,61);support=self.upload(guest,number+'S','support');body['expenses'][0]['paymentProofIds']=[proof['id']];body['supportingFiles']=[support['id']];files += [proof,support]
        status,result=self.req(guest,'/api/submissions',body,headers={'Idempotency-Key':number});self.assertEqual(status,201,result);return result,files,body

    def test_delete_and_restore_preserve_all_materials_and_exclude_public_and_exports(self):
        from openpyxl import load_workbook
        admin=self.admin();guest=self.guest();submitted,files,body=self.create(guest,'TRASHROUNDTRIP00001',extras=True)
        before=self.find(admin,submitted['reference'])['items'][0];sid=before['id'];url='/api/admin/submissions/'+sid
        self.assertEqual(self.req(guest,url,{},method='DELETE')[0],401)
        self.assertEqual(self.req(self.client(),'/api/admin/submissions?deleted=1')[0],401)
        self.assertEqual(self.req(admin,url,{},method='DELETE',headers={'Origin':'https://evil.example'})[0],403)
        for _ in range(2):self.assertEqual(self.req(admin,url,{},method='DELETE')[0],200)
        active=self.find(admin,submitted['reference']);self.assertEqual(active['items'],[]);self.assertEqual(active['stats']['totalAmount'],0)
        trash=self.find(admin,submitted['reference'],True);self.assertEqual(len(trash['items']),1);self.assertTrue(trash['items'][0]['deletedAt']);self.assertGreaterEqual(trash['trashCount'],1)
        self.assertEqual(self.req(admin,url,{'status':'paid','reviewNote':'should not save'},method='PATCH')[0],409)
        self.assertEqual(self.req(self.client(),'/api/guest/lookup',{'mobile':'13900001111','reference':submitted['reference']})[0],404)
        public=self.req(self.client(),'/api/guest/lookup',{'mobile':'13900001111'})[1]
        self.assertEqual(public['items'],[])
        for file in files:
            self.assertEqual(self.req(guest,'/api/files/'+file['id'])[0],404)
            self.assertEqual(self.req(admin,'/api/files/'+file['id'])[0],200)
            self.assertTrue((self.server.app.files/file['id']).is_file())
        query='?q='+submitted['reference']+'&deleted=1'
        status,raw=self.req(admin,'/api/admin/export.xlsx'+query);self.assertEqual(status,200);self.assertEqual(load_workbook(io.BytesIO(raw)).active.max_row,1)
        status,raw=self.req(admin,'/api/admin/export.zip'+query);self.assertEqual(status,200)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:self.assertEqual(z.namelist(),['报销汇总.xlsx'])
        self.assertEqual(self.req(guest,'/api/submissions',body,headers={'Idempotency-Key':'TRASHROUNDTRIP00001'})[0],409)
        self.assertEqual(self.req(guest,url+'/restore',{})[0],401)
        self.assertEqual(self.req(admin,url+'/restore',{},headers={'Origin':'https://evil.example'})[0],403)
        for _ in range(2):self.assertEqual(self.req(admin,url+'/restore',{})[0],200)
        self.assertEqual(self.find(admin,submitted['reference'])['items'][0],before)
        self.assertEqual(self.req(guest,'/api/files/'+files[0]['id'])[0],200)
        self.assertEqual(len(self.req(self.client(),'/api/guest/lookup',{'mobile':'13900001111'})[1]['items']),1)
        with self.server.app.db() as db:
            actions=[r['action'] for r in db.execute('SELECT action FROM audit WHERE subject=?',(sid,))]
            self.assertEqual(actions.count('submission_delete'),1);self.assertEqual(actions.count('submission_restore'),1)
        self.assertEqual(self.req(admin,'/api/admin/submissions/'+'0'*32,{},method='DELETE')[0],404)
        self.assertEqual(self.req(admin,url,{},method='DELETE')[0],200)

    def test_deleted_invoice_can_be_retested_and_duplicate_restore_is_blocked(self):
        admin=self.admin();guest=self.guest();number='TRASHREUSE000000001';old,files,_=self.create(guest,number)
        oldurl='/api/admin/submissions/'+self.find(admin,old['reference'])['items'][0]['id']
        self.assertEqual(self.req(admin,oldurl,{},method='DELETE')[0],200)
        replacement=self.upload(guest,number);self.assertNotEqual(files[0]['id'],replacement['id'])
        status,new=self.req(guest,'/api/submissions',self.payload(replacement,number));self.assertEqual(status,201,new)
        newurl='/api/admin/submissions/'+self.find(admin,new['reference'])['items'][0]['id']
        status,error=self.req(admin,oldurl+'/restore',{});self.assertEqual(status,409);self.assertEqual(error['error'],'restore_conflict')
        self.assertTrue(self.find(admin,old['reference'],True)['items'][0]['deletedAt'])
        self.assertEqual(self.req(admin,newurl,{},method='DELETE')[0],200)
        self.assertEqual(self.req(admin,oldurl+'/restore',{})[0],200)
        self.assertEqual(self.req(admin,newurl+'/restore',{})[0],409)
        self.assertEqual(self.req(admin,oldurl,{},method='DELETE')[0],200)

    def test_invoice_number_conflict_blocks_restore_even_for_different_file_contents(self):
        admin=self.admin();guest=self.guest();number='TRASHNUMBER00000001';old,_,_=self.create(guest,'TRASHFILEA000000001',number)
        oldurl='/api/admin/submissions/'+self.find(admin,old['reference'])['items'][0]['id'];self.assertEqual(self.req(admin,oldurl,{},method='DELETE')[0],200)
        new,_,_=self.create(guest,'TRASHFILEB000000001',number)
        self.assertEqual(self.req(admin,oldurl+'/restore',{})[0],409)
        newurl='/api/admin/submissions/'+self.find(admin,new['reference'])['items'][0]['id'];self.assertEqual(self.req(admin,newurl,{},method='DELETE')[0],200)
