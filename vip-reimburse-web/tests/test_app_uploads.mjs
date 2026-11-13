import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';

// Exercise the actual app's asynchronous operations without a browser or network.
// Only DOM rendering, the draft transport, and HTTP responses are substituted.
const source = fs.readFileSync(new URL('../public/app.js', import.meta.url), 'utf8')
  .replace(/^import .*\n/, '')
  .replace(/\ninit\(\);\s*$/, '');
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
};
const drain = async () => {
  for (let i = 0; i < 12; i++) await Promise.resolve();
};
const file = (name = 'sample.png') => ({name, type: 'image/png', size: 100});
const id = number => number.toString(16).padStart(32, '0');
const metadata = (number, extra = {}) => ({id: id(number), name: `file-${number}.png`, size: 100, mime: 'image/png', ...extra});
const plain = value => JSON.parse(JSON.stringify(value));

function setup() {
  let flush = Promise.resolve(true);
  const requests = [], messages = [], changes = [];
  const store = {
    changed: immediate => changes.push(immediate),
    flush: () => flush,
    initialize() {}, retry() {}, beacon() {}, suspend() {}, resume() {}, complete() {},
    getState: () => ({completed: false}),
  };
  const root = {addEventListener() {}};
  const context = vm.createContext({
    crypto: webcrypto, createDraftStore: () => store,
    document: {querySelector: () => root, addEventListener() {}, body: {style: {}}},
    location: {pathname: '/'}, window: {addEventListener() {}}, navigator: {},
    setTimeout: () => 0, clearTimeout() {}, TextEncoder, Blob,
    requestAnimationFrame: callback => callback(),
    __http: (url, options) => {
      const request = {...deferred(), url, options};
      requests.push(request);
      return request.promise;
    },
    __message: text => messages.push(text),
  });
  vm.runInContext(source + `
    render=()=>{};focusHeading=()=>{};toast=__message;api=__http;
    globalThis.h={state,blankExpense,uploadFiles,removeExpense,removePaymentProof,
      removeSupportingFile,recognizeAgain,hasPendingFiles,fileCount,nextStep,
      restoreDraft,draftSnapshot};
  `, context);
  const h = context.h;
  h.state.config = {maxFiles: 12, maxFileSize: 10_000};
  h.state.guestAuthenticated = true;
  return {...h, requests, messages, changes, setFlush: promise => { flush = promise; },
    expense(extra = {}) {
      const entry = Object.assign(h.blankExpense(), extra);
      h.state.expenses.push(entry);
      return entry;
    }};
}

test('two same-expense uploads waiting for a draft save cannot overwrite one another', async () => {
  const h = setup(), ready = deferred(), e = h.expense();
  h.setFlush(ready.promise);
  const first = h.uploadFiles([file('first.png')], 'invoice', e.uid);
  const duplicate = h.uploadFiles([file('second.png')], 'invoice', e.uid);
  assert.equal(h.hasPendingFiles(), true);
  assert.equal(h.requests.length, 0);
  ready.resolve(true);
  await drain();
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].options.headers['X-Draft-Entry'], e.uid);
  assert.equal(h.requests[0].options.body.name, 'first.png');
  h.requests[0].resolve(metadata(1, {ocr: {fields: {amount: 106}}}));
  await Promise.all([first, duplicate]);
  assert.equal(e.fileId, id(1));
  assert.equal(e.amount, 106);
  assert.equal(h.hasPendingFiles(), false);
});

test('preparing a proof upload blocks deletion, recognition, and advancing to review', async () => {
  const h = setup(), ready = deferred();
  const proof = {uid: 'existing-proof', fileId: id(2), name: 'existing.png'};
  const e = h.expense({fileId: id(1), amount: 106, category: 'rail', paymentProofs: [proof]});
  h.state.step = 2;
  h.setFlush(ready.promise);
  const uploading = h.uploadFiles([file()], 'payment-proof', e.uid);
  await h.removeExpense(e.uid);
  await h.removeExpense(e.uid, true);
  await h.removePaymentProof(e.uid, proof.uid);
  await h.recognizeAgain(e.uid);
  await h.nextStep();
  assert.equal(h.requests.length, 0);
  assert.equal(h.state.expenses.length, 1);
  assert.equal(e.paymentProofs.length, 1);
  assert.equal(h.state.step, 2);
  ready.resolve(true);
  await drain();
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].options.headers['X-Upload-Purpose'], 'payment-proof');
  h.requests[0].resolve(metadata(3));
  await uploading;
  assert.equal(e.paymentProofs.length, 2);
});

test('different expenses can upload concurrently and keep their own responses', async () => {
  const h = setup(), ready = deferred(), a = h.expense(), b = h.expense();
  h.setFlush(ready.promise);
  const first = h.uploadFiles([file('a.png')], 'invoice', a.uid);
  const second = h.uploadFiles([file('b.png')], 'invoice', b.uid);
  ready.resolve(true);
  await drain();
  assert.equal(h.requests.length, 2);
  h.requests[1].resolve(metadata(2, {ocr: {fields: {amount: 222}}}));
  h.requests[0].resolve(metadata(1, {ocr: {fields: {amount: 111}}}));
  await Promise.all([first, second]);
  assert.equal(a.fileId, id(1));
  assert.equal(a.amount, 111);
  assert.equal(b.fileId, id(2));
  assert.equal(b.amount, 222);
});

test('simultaneous preparations recheck total capacity before starting an upload', async () => {
  const h = setup(), ready = deferred(), a = h.expense(), b = h.expense();
  for (let i = 1; i <= 11; i++) h.state.supportingFiles.push({uid: `s-${i}`, fileId: id(i)});
  h.setFlush(ready.promise);
  const first = h.uploadFiles([file('a.png')], 'invoice', a.uid);
  const second = h.uploadFiles([file('b.png')], 'invoice', b.uid);
  ready.resolve(true);
  await drain();
  assert.equal(h.requests.length, 1, 'the last available slot must be reserved by its uploading placeholder');
  assert.equal(h.fileCount(), 12);
  h.requests[0].resolve(metadata(12));
  await Promise.all([first, second]);
  assert.equal(h.fileCount(), 12);
  assert.equal([a, b].filter(e => e.fileId).length, 1);
  assert.equal(h.hasPendingFiles(), false);
});

test('a proof batch stays associated with its expense and never overwrites invoice values', async () => {
  const h = setup();
  const e = h.expense({fileId: id(1), amount: 106, paymentProofs: [{uid: 'existing', fileId: id(2)}]});
  const uploading = h.uploadFiles([file('proof-a.png'), file('proof-b.png')], 'payment-proof', e.uid);
  await drain();
  assert.equal(h.requests.length, 1);
  assert.equal(e.paymentProofs.length, 2);
  assert.equal(h.fileCount(), 3);
  h.requests[0].resolve(metadata(3, {ocr: {fields: {amount: 999}}}));
  await drain();
  assert.equal(h.requests.length, 2);
  assert.equal(e.paymentProofs.length, 3);
  h.requests[1].resolve(metadata(4, {ocr: {fields: {amount: 999}}}));
  await uploading;
  assert.equal(e.amount, 106);
  assert.equal(e.fileId, id(1));
  assert.deepEqual(plain(h.draftSnapshot().expenses[0].paymentProofIds), [id(2), id(3), id(4)]);
  await h.uploadFiles([file('too-many.png')], 'payment-proof', e.uid);
  assert.equal(h.requests.length, 2);
  assert.equal(h.hasPendingFiles(), false);
});

test('a support batch keeps its lock while multiple files upload sequentially', async () => {
  const h = setup(), ready = deferred();
  h.setFlush(ready.promise);
  const first = h.uploadFiles([file('a.png'), file('b.png')], 'support');
  const duplicate = h.uploadFiles([file('c.png')], 'support');
  ready.resolve(true);
  await drain();
  assert.equal(h.requests.length, 1);
  h.requests[0].resolve(metadata(1));
  await drain();
  assert.equal(h.requests.length, 2);
  assert.equal(h.hasPendingFiles(), true);
  h.requests[1].resolve(metadata(2));
  await Promise.all([first, duplicate]);
  assert.deepEqual(plain(h.state.supportingFiles.map(f => f.fileId)), [id(1), id(2)]);
  assert.equal(h.hasPendingFiles(), false);
});

for (const reason of ['removed-parent', 'busy']) {
  test(`upload preparation cancels if ${reason} is discovered after the draft save`, async () => {
    const h = setup(), ready = deferred(), e = h.expense();
    h.setFlush(ready.promise);
    const uploading = h.uploadFiles([file()], 'invoice', e.uid);
    if (reason === 'removed-parent') h.state.expenses.splice(0, 1);
    else h.state.busy = true;
    ready.resolve(true);
    await uploading;
    assert.equal(h.requests.length, 0);
    assert.equal(h.hasPendingFiles(), false);
  });
}

test('failed draft persistence releases preparation so the user can retry uploading', async () => {
  const h = setup(), e = h.expense();
  h.setFlush(Promise.resolve(false));
  await h.uploadFiles([file()], 'invoice', e.uid);
  assert.equal(h.requests.length, 0);
  assert.equal(h.hasPendingFiles(), false);
  h.setFlush(Promise.resolve(true));
  const uploading = h.uploadFiles([file()], 'invoice', e.uid);
  await drain();
  assert.equal(h.requests.length, 1);
  h.requests[0].resolve(metadata(1));
  await uploading;
  assert.equal(e.fileId, id(1));
});

test('a failed upload drops its placeholder and releases the preparation lock', async () => {
  const h = setup(), e = h.expense({fileId: id(1)});
  const uploading = h.uploadFiles([file()], 'payment-proof', e.uid);
  await drain();
  assert.equal(e.paymentProofs.length, 1);
  h.requests[0].reject(new Error('connection lost'));
  await uploading;
  assert.equal(e.paymentProofs.length, 0);
  assert.equal(e.fileId, id(1));
  assert.equal(h.hasPendingFiles(), false);
  assert.ok(h.messages.includes('connection lost'));
});

test('removing only an invoice preserves the purpose and associated payment proofs', async () => {
  const h = setup();
  const proof = {uid: 'proof', fileId: id(2)};
  const e = h.expense({fileId: id(1), amount: 106, category: 'rail', note: '返程', paymentProofs: [proof]});
  const removing = h.removeExpense(e.uid, true);
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].url, `/api/uploads/${id(1)}`);
  h.requests[0].resolve({ok: true});
  await removing;
  assert.equal(e.fileId, '');
  assert.equal(e.amount, '');
  assert.equal(e.category, 'rail');
  assert.equal(e.note, '返程');
  assert.equal(e.paymentProofs[0], proof);
});

test('partial expense deletion removes only successfully deleted proofs', async () => {
  const h = setup();
  const first = {uid: 'p1', fileId: id(2)}, second = {uid: 'p2', fileId: id(3)};
  const e = h.expense({fileId: id(1), paymentProofs: [first, second]});
  const removing = h.removeExpense(e.uid);
  assert.equal(h.requests[0].url, `/api/uploads/${id(2)}`);
  h.requests[0].resolve({ok: true});
  await drain();
  assert.equal(e.paymentProofs.length, 1);
  assert.equal(h.requests[1].url, `/api/uploads/${id(3)}`);
  h.requests[1].reject(new Error('delete failed'));
  await removing;
  assert.equal(h.state.expenses.length, 1);
  assert.equal(e.fileId, id(1));
  assert.equal(e.paymentProofs[0], second);
  assert.equal(e.removing, false);
  assert.ok(h.changes.includes(true), 'successful partial deletion must be saved');
});

test('restoration recovers upload responses by parent uid while preserving manual invoice fields', () => {
  const h = setup(), uid = webcrypto.randomUUID();
  const invoice = metadata(1, {purpose: 'invoice', clientRef: uid, ocr: {fields: {amount: 106, date: '2026-11-13'}}});
  const proof = metadata(2, {purpose: 'payment-proof', clientRef: uid});
  const support = metadata(3, {purpose: 'support', clientRef: ''});
  h.restoreDraft({authenticated: false,
    draft: {step: 2, applicant: {name: '示例嘉宾'}, payment: {}, submissionKey: 'restore-key',
      expenses: [{uid, amount: '90', category: 'rail', paymentProofIds: []}], supportingFiles: []},
    uploads: [invoice, proof, support]});
  const e = h.state.expenses[0];
  assert.equal(e.fileId, id(1));
  assert.equal(e.amount, '90');
  assert.equal(e.date, '2026-11-13');
  assert.equal(e.paymentProofs[0].fileId, id(2));
  assert.equal(h.state.supportingFiles[0].fileId, id(3));
  assert.equal(h.fileCount(), 3);
  assert.equal(h.hasPendingFiles(), false);
  assert.equal(h.state.step, 1, 'an expired invitation session must be verified again');
  assert.equal(h.state.submissionKey, 'restore-key');
});

test('transport OCR fills an empty category and preserves a category selected by the guest', async () => {
  const h=setup(),rail=h.expense(),chosen=h.expense({category:'other'});
  const a=h.uploadFiles([file('rail.png')],'invoice',rail.uid);
  const b=h.uploadFiles([file('flight.png')],'invoice',chosen.uid);
  await drain();
  h.requests[0].resolve(metadata(71,{ocr:{suggestedCategory:'rail',documentLabel:'电子发票（铁路电子客票）',fields:{amount:493}}}));
  h.requests[1].resolve(metadata(72,{ocr:{suggestedCategory:'flight',documentLabel:'电子发票（航空运输电子客票行程单）',fields:{amount:670,taxAmount:51.19}}}));
  await Promise.all([a,b]);
  assert.equal(rail.category,'rail');assert.equal(rail.amount,493);
  assert.equal(chosen.category,'other');assert.equal(chosen.amount,670);
});

test('re-recognizing a transport invoice keeps manual fields and a selected category', async () => {
  const h=setup(),e=h.expense({fileId:id(80),category:'hotel',amount:'650',buyerTaxId:'MANUALLY-CORRECTED',ocr:{fields:{amount:600}}});
  const pending=h.recognizeAgain(e.uid);await drain();
  h.requests[0].resolve({ocr:{suggestedCategory:'flight',fields:{amount:670,buyerTaxId:'91510100MA6ABC1234'}}});
  await pending;
  assert.equal(e.category,'hotel');assert.equal(e.amount,'650');assert.equal(e.buyerTaxId,'MANUALLY-CORRECTED');
});
