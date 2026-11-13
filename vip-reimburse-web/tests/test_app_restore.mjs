import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';

// Run the application's real snapshot, restoration and rendering functions. The
// transport is inert so these checks neither contact the server nor save data.
const source = fs.readFileSync(new URL('../public/app.js', import.meta.url), 'utf8')
  .replace(/^import\s+\{createDraftStore\}\s+from\s+['"].*?['"];?\s*\n/, '')
  .replace(/\ninit\(\);\s*$/, '');
const plain = value => JSON.parse(JSON.stringify(value));
const fileId = number => number.toString(16).padStart(32, '0');
const uploaded = (number, purpose, clientRef = '', extra = {}) => ({
  id: fileId(number), purpose, clientRef,
  name: `server-${number}.png`, size: 1200 + number, mime: 'image/png', ...extra,
});

function setup() {
  const handlers = new Map();
  const app = {innerHTML: '', addEventListener: (type, handler) => handlers.set(type, handler)};
  const document = {
    querySelector: selector => selector === '#app' ? app : null,
    querySelectorAll: () => [], addEventListener() {},
    body: {dataset: {}, style: {}}, title: '',
  };
  const store = {
    initialize() {}, changed() {}, retry() {}, flush: async () => true,
    beacon() {}, suspend() {}, resume() {}, complete() {},
    getState: () => ({completed: false}),
  };
  const context = vm.createContext({
    document, crypto: webcrypto, location: {pathname: '/', origin: 'http://example.test'},
    window: {addEventListener() {}}, navigator: {},
    createDraftStore: () => store,
    setTimeout: () => 0, clearTimeout() {},
    requestAnimationFrame: callback => callback(), Blob, TextEncoder,
    fetch: () => { throw new Error('restoration tests must not access the network'); },
  });
  vm.runInContext(source + `
    globalThis.h={state,blankExpense,draftSnapshot,restoreDraft,hasPendingFiles,
      fileCount,render,lookup,success};
  `, context, {filename: 'public/app.js'});
  const h = context.h;
  h.state.config = {eventName: '虚构大会', maxFiles: 12};
  return {...h, app, document};
}

function savedDraft(extra = {}) {
  return {
    version: 1, step: 2,
    applicant: {name: '虚构嘉宾', mobile: '13800138000', organization: '测试单位', contactPerson: '测试对接人'},
    payment: {method: 'bank', accountName: '虚构嘉宾', accountNumber: '6222000000000000000', bankName: '测试银行', bankBranch: ''},
    expenses: [], supportingFiles: [], note: '', submissionKey: 'stable-draft-key',
    ...extra,
  };
}

test('snapshot keeps edited invoice values and payment details without credentials or transient state', () => {
  const h = setup();
  const e = Object.assign(h.blankExpense(), {
    uid: 'expense-a', fileId: fileId(1), category: 'rail', amount: '90.25',
    buyerTaxId: '91510100MANUAL12345', sellerTaxId: '91511000MANUAL56789',
    note: '人工核对后的返程费用', uploading: true, recognizing: true, removing: true,
    ocr: {fields: {amount: 106, buyerTaxId: 'OCR-OLD-TAX-ID'}},
    paymentProofs: [{fileId: fileId(2), uploading: false}, {uid: 'pending-proof', uploading: true}],
  });
  h.state.applicant = savedDraft().applicant;
  h.state.payment = savedDraft().payment;
  h.state.expenses = [e];
  h.state.supportingFiles = [{fileId: fileId(3)}, {uid: 'pending-support', uploading: true}];
  h.state.consent = true;
  h.state.inviteCode = 'INVITE-MUST-NOT-PERSIST';
  h.state.admin.password = 'ADMIN-MUST-NOT-PERSIST';
  h.state.errors = {mobile: 'temporary validation error'};
  h.state.busy = true;

  const snapshot = plain(h.draftSnapshot());
  assert.deepEqual(snapshot.applicant, savedDraft().applicant);
  assert.deepEqual(snapshot.payment, savedDraft().payment);
  assert.equal(snapshot.expenses[0].amount, '90.25');
  assert.equal(snapshot.expenses[0].buyerTaxId, '91510100MANUAL12345');
  assert.deepEqual(snapshot.expenses[0].paymentProofIds, [fileId(2)]);
  assert.deepEqual(snapshot.supportingFiles, [fileId(3)]);
  const serialized = JSON.stringify(snapshot);
  for (const forbidden of ['consent', 'inviteCode', 'ADMIN-MUST-NOT-PERSIST', 'INVITE-MUST-NOT-PERSIST',
    'uploading', 'recognizing', 'removing', 'errors', 'busy', 'ocr', 'pending-proof', 'pending-support']) {
    assert.equal(serialized.includes(forbidden), false, `${forbidden} must not enter the persisted draft`);
  }
});

test('round trip preserves manual values, including intentionally cleared OCR fields, and restores all file roles', () => {
  const original = setup();
  original.state.applicant = savedDraft().applicant;
  original.state.payment = savedDraft().payment;
  original.state.step = 3;
  original.state.expenses = [Object.assign(original.blankExpense(), {
    uid: 'expense-a', fileId: fileId(1), category: 'hotel', note: '住宿人工核对',
    amount: '250.50', buyerTaxId: '91510100MANUAL12345', sellerTaxId: '', invoiceNumber: '',
    paymentProofs: [{fileId: fileId(2)}],
  })];
  original.state.supportingFiles = [{fileId: fileId(3)}];
  const draft = plain(original.draftSnapshot());
  const h = setup();
  h.restoreDraft({authenticated: true, draft, uploads: [
    uploaded(1, 'invoice', 'expense-a', {ocr: {fields: {amount: 999, buyerTaxId: 'OLD-ID', sellerTaxId: 'OLD-SELLER', invoiceNumber: 'OLD-NUMBER'}}}),
    uploaded(2, 'payment-proof', 'expense-a'), uploaded(3, 'support'),
  ]});
  const e = h.state.expenses[0];
  assert.equal(e.amount, '250.50');
  assert.equal(e.buyerTaxId, '91510100MANUAL12345');
  assert.equal(e.sellerTaxId, '', 'an intentional manual clear must survive an ordinary reload');
  assert.equal(e.invoiceNumber, '');
  assert.equal(e.name, 'server-1.png');
  assert.deepEqual(plain(h.draftSnapshot().expenses[0]), draft.expenses[0]);
  assert.deepEqual(plain(h.state.applicant), draft.applicant);
  assert.deepEqual(plain(h.state.payment), draft.payment);
  assert.equal(e.paymentProofs[0].fileId, fileId(2));
  assert.equal(h.state.supportingFiles[0].fileId, fileId(3));
  assert.equal(h.fileCount(), 3);
  assert.equal(h.hasPendingFiles(), false);
  assert.equal(h.state.step, 3);
});

test('a lost invoice response is recovered by both purpose and expense uid without overwriting manual values', () => {
  const h = setup();
  h.restoreDraft({authenticated: true, draft: savedDraft({expenses: [
    {uid: 'expense-a', fileId: '', category: 'taxi', amount: '90.25', buyerTaxId: 'MANUAL-ID', paymentProofIds: []},
    {uid: 'expense-b', fileId: '', category: 'rail', paymentProofIds: []},
  ]}), uploads: [
    uploaded(1, 'payment-proof', 'expense-a', {ocr: {fields: {amount: 999}}}),
    uploaded(2, 'support', 'expense-a'),
    uploaded(3, 'invoice', 'expense-b', {ocr: {fields: {amount: 222}}}),
    uploaded(4, 'invoice', 'expense-a', {ocr: {fields: {amount: 106, buyerTaxId: 'OCR-ID', date: '2026-11-13', taxAmount: 6}}}),
  ]});
  const [a, b] = h.state.expenses;
  assert.equal(a.fileId, fileId(4));
  assert.equal(a.amount, '90.25');
  assert.equal(a.buyerTaxId, 'MANUAL-ID');
  assert.equal(a.date, '2026-11-13', 'previously unknown fields may use the recovered OCR result');
  assert.equal(a.taxAmount, 6);
  assert.equal(b.fileId, fileId(3));
  assert.equal(b.amount, 222);
  assert.equal(a.paymentProofs[0].fileId, fileId(1));
  assert.equal(h.state.supportingFiles[0].fileId, fileId(2));
});

test('proof and support uploads with lost responses recover only into their own roles and expense', () => {
  const h = setup();
  h.restoreDraft({authenticated: true, draft: savedDraft({expenses: [
    {uid: 'expense-a', fileId: fileId(1), amount: '106', paymentProofIds: [fileId(2)]},
    {uid: 'expense-b', fileId: fileId(6), amount: '222', paymentProofIds: []},
  ], supportingFiles: []}), uploads: [
    uploaded(1, 'invoice', 'expense-a'), uploaded(2, 'payment-proof', 'expense-a'),
    uploaded(3, 'payment-proof', 'expense-a'), uploaded(4, 'payment-proof', 'expense-a'),
    uploaded(5, 'payment-proof', 'expense-b'), uploaded(6, 'invoice', 'expense-b'),
    uploaded(7, 'support'), uploaded(8, 'invoice', 'orphan-expense'),
  ]});
  assert.deepEqual(plain(h.state.expenses[0].paymentProofs.map(f => f.fileId)), [fileId(2), fileId(3), fileId(4)]);
  assert.deepEqual(plain(h.state.expenses[1].paymentProofs.map(f => f.fileId)), [fileId(5)]);
  assert.deepEqual(plain(h.state.supportingFiles.map(f => f.fileId)), [fileId(7)]);
  assert.equal(h.state.expenses[0].amount, '106', 'proof metadata must not become invoice values');
  assert.equal(h.state.expenses.length, 2, 'orphan invoice uploads must not create new expense entries');
});

test('missing, deleted and submitted files omitted by the server are never resurrected from stale ids', () => {
  const h = setup();
  // Session uploads are the server's active, unsubmitted files. Old submitted
  // file ids and deleted files can remain in a stale saved snapshot, never here.
  h.restoreDraft({authenticated: true, draft: savedDraft({expenses: [
    {uid: 'expense-a', fileId: fileId(1), amount: '123', category: 'flight', note: '保留文字', paymentProofIds: [fileId(2)]},
  ], supportingFiles: [fileId(3)]}), uploads: []});
  const e = h.state.expenses[0];
  assert.equal(e.fileId, '');
  assert.equal(e.name, '');
  assert.equal(e.ocr, undefined);
  assert.deepEqual(plain(e.paymentProofs), []);
  assert.deepEqual(plain(h.state.supportingFiles), []);
  assert.equal(e.category, 'flight');
  assert.equal(e.note, '保留文字');
  assert.equal(h.fileCount(), 0);
  assert.equal(h.hasPendingFiles(), false);
  const snapshot = JSON.stringify(h.draftSnapshot());
  for (const number of [1, 2, 3]) assert.equal(snapshot.includes(fileId(number)), false);
});

test('an unfinished upload restores as an editable empty card rather than a stuck spinner', () => {
  const h = setup();
  h.restoreDraft({authenticated: true, draft: savedDraft({expenses: [{
    uid: 'expense-empty', fileId: '', category: 'other', amount: '', note: '稍后补票',
    name: 'unfinished.png', size: 999, mime: 'image/png',
    uploading: true, recognizing: true, removing: true, paymentProofIds: [],
    paymentProofs: [{uid: 'unfinished-proof', uploading: true}],
  }]}), uploads: []});
  const e = h.state.expenses[0];
  assert.equal(e.name, '');
  assert.equal(e.size, 0);
  assert.equal(e.fileId, '');
  assert.equal(e.uploading, false);
  assert.equal(Boolean(e.recognizing || e.removing), false);
  assert.deepEqual(plain(e.paymentProofs), []);
  assert.equal(e.note, '稍后补票');
  assert.equal(h.hasPendingFiles(), false);
  h.render();
  assert.match(h.app.innerHTML, /稍后补票/);
  assert.doesNotMatch(h.app.innerHTML, /正在上传并识别|正在移除票据/);
});

test('restoration clears consent and invitation while retaining details after authentication expires', () => {
  const h = setup();
  h.state.consent = true;
  h.state.inviteCode = 'CURRENT-INVITATION';
  h.restoreDraft({authenticated: false, draft: savedDraft({step: 3,
    consent: true, inviteCode: 'SAVED-INVITATION', admin: {password: 'SAVED-ADMIN'},
    expenses: [{uid: 'expense-a', fileId: fileId(1), amount: '106', paymentProofIds: []}],
  }), uploads: [uploaded(1, 'invoice', 'expense-a')]});
  assert.equal(h.state.consent, false);
  assert.equal(h.state.inviteCode, '');
  assert.equal(h.state.admin.password, '');
  assert.equal(h.state.guestAuthenticated, false);
  assert.equal(h.state.step, 1);
  assert.match(h.state.errors.inviteCode, /重新验证邀请码/);
  assert.equal(h.state.applicant.organization, '测试单位');
  assert.equal(h.state.payment.accountNumber, '6222000000000000000');
  assert.equal(h.state.expenses[0].fileId, fileId(1));
  h.render();
  assert.match(h.app.innerHTML, /id="inviteCode"[^>]*value=""/);
  assert.doesNotMatch(h.app.innerHTML, /CURRENT-INVITATION|SAVED-INVITATION|SAVED-ADMIN/);
});

test('phone lookup renders one mobile input and multiple progress records without legacy reference or sensitive data', () => {
  const h = setup();
  h.state.view = 'lookup';
  h.state.lookup.mobile = '13800138000';
  h.state.lookup.reference = 'LEGACY-REFERENCE-MUST-NOT-RENDER';
  h.state.lookup.result = {items: [
    {reference: 'PRIVATE-REF-A', status: 'pending', totalAmount: 106, expenseCount: 1,
      createdAt: '2026-11-13T10:00:00Z', updatedAt: '2026-11-13T11:00:00Z',
      reviewNote: '等待票据核对', applicant: {name: 'PRIVATE-NAME'},
      payment: {accountNumber: 'PRIVATE-BANK'}, expenses: [{fileId: 'PRIVATE-FILE'}]},
    {reference: 'PRIVATE-REF-B', status: 'needs_info', totalAmount: 222, expenseCount: 2,
      createdAt: '2026-11-13T12:00:00Z', reviewNote: '<img src=x onerror=alert(1)>',
      supportingFiles: [{id: 'PRIVATE-SUPPORT'}], paymentProofs: [{id: 'PRIVATE-PROOF'}]},
  ]};
  h.render();
  const html = h.app.innerHTML;
  assert.deepEqual([...html.matchAll(/<input\b[^>]*\bname="([^"]+)"/g)].map(match => match[1]), ['lookup-mobile']);
  assert.match(html, /value="13800138000"/);
  assert.equal((html.match(/class="lookup-result"/g) || []).length, 2);
  for (const text of ['106.00', '222.00', '待审核', '待补充', '等待票据核对', '最后更新']) assert.ok(html.includes(text));
  for (const privateText of ['LEGACY-REFERENCE-MUST-NOT-RENDER', 'PRIVATE-REF-A', 'PRIVATE-REF-B',
    'PRIVATE-NAME', 'PRIVATE-BANK', 'PRIVATE-FILE', 'PRIVATE-SUPPORT', 'PRIVATE-PROOF',
    'lookup-reference', 'copy-reference', '/api/files/']) assert.equal(html.includes(privateText), false, privateText);
  assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'), 'review notes must render as text');
  assert.doesNotMatch(html, /<img src=x/);
});

test('success renders the submitted phone and lookup action without an application reference or copy control', () => {
  const h = setup();
  h.state.applicant.mobile = '13800138000';
  h.state.success = {reference: 'PRIVATE-APPLICATION-REFERENCE', totalAmount: 106, expenseCount: 1,
    submittedAt: '2026-11-13T10:00:00Z'};
  h.render();
  const html = h.app.innerHTML;
  assert.match(html, /您的预留手机号/);
  assert.match(html, /13800138000/);
  assert.match(html, /以后使用此手机号即可查询报销进度/);
  assert.match(html, /data-action="lookup-success"/);
  assert.doesNotMatch(html, /PRIVATE-APPLICATION-REFERENCE|申请编号|copy-reference|lookup-reference/);
});
