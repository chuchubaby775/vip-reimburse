import test from 'node:test';
import assert from 'node:assert/strict';
import {createDraftStore} from '../public/draft-store.js';

const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
};
const drain = async () => {
  for (let i = 0; i < 8; i++) await Promise.resolve();
};
const failure = (status = 503, code = 'unavailable') => Object.assign(new Error(code), {status, code});

function setup(t, session = {draft: null, revision: 0}) {
  const sessions = [], writes = [], statuses = [], restorations = [];
  let sessionResult = session;
  const model = {applicant: {name: ''}, step: 1, submissionKey: 'stable-submission-key'};
  const store = createDraftStore({
    delay: 60_000,
    clientId: 'draft-test-client',
    snapshot: () => structuredClone(model),
    restore: value => restorations.push(structuredClone(value)),
    onStatus: value => statuses.push({...value}),
    api: (url, options) => {
      assert.equal(options.method, 'POST');
      if (url === '/api/guest/draft/session') {
        sessions.push(options);
        return Promise.resolve(sessionResult);
      }
      assert.equal(url, '/api/guest/draft');
      const request = {...deferred(), packet: JSON.parse(options.body)};
      writes.push(request);
      return request.promise;
    },
  });
  t.after(() => store.complete());
  return {store, model, sessions, writes, statuses, restorations,
    setSession: value => { sessionResult = value; }};
}

test('initialization restores the server snapshot and leaves an unchanged form clean', async t => {
  const saved = {draft: {applicant: {name: '示例嘉宾'}, step: 2}, revision: 7, updatedAt: 'saved-at'};
  const h = setup(t, saved);
  assert.equal(await h.store.initialize(), true);
  assert.deepEqual(h.restorations, [saved]);
  assert.equal(h.store.getState().revision, 7);
  assert.equal(h.statuses.at(-1).phase, 'restored');
  assert.equal(h.statuses.at(-1).lastSaved, 'saved-at');
  assert.equal(await h.store.flush(), true);
  assert.equal(h.writes.length, 0);
});

test('edits made while saving are serialized into a second, fresh snapshot', async t => {
  const h = setup(t, {draft: null, revision: 4});
  await h.store.initialize();
  h.model.applicant.name = '第一次填写';
  h.store.changed();
  const saving = h.store.flush();
  assert.equal(h.writes.length, 1);
  assert.deepEqual(h.writes[0].packet, {
    draft: {applicant: {name: '第一次填写'}, step: 1, submissionKey: 'stable-submission-key'},
    clientId: 'draft-test-client', sequence: 1, baseRevision: 4,
  });
  h.model.applicant.name = '后来修改';
  h.model.step = 2;
  h.store.changed();
  const concurrentFlush = h.store.flush();
  assert.equal(h.writes.length, 1, 'an in-flight save must not start a parallel request');
  h.writes[0].resolve({revision: 5, updatedAt: 'first-ack'});
  await drain();
  assert.equal(h.writes.length, 2);
  assert.equal(h.writes[0].packet.draft.applicant.name, '第一次填写');
  assert.equal(h.writes[1].packet.draft.applicant.name, '后来修改');
  assert.equal(h.writes[1].packet.draft.step, 2);
  assert.equal(h.writes[1].packet.sequence, 2);
  assert.equal(h.writes[1].packet.baseRevision, 5);
  assert.equal(h.statuses.some(s => s.phase === 'saved'), false,
    'acknowledging an older edit must not label newer edits as saved');
  h.writes[1].resolve({revision: 6, updatedAt: 'latest-ack'});
  assert.equal(await saving, true);
  assert.equal(await concurrentFlush, true);
  const state = h.store.getState();
  assert.equal(state.savedGeneration, state.generation);
  assert.equal(state.revision, 6);
  assert.equal(h.statuses.at(-1).phase, 'saved');
  assert.equal(h.statuses.at(-1).lastSaved, 'latest-ack');
});

test('a lower revision acknowledgement cannot move the local revision backwards', async t => {
  const h = setup(t, {draft: null, revision: 8});
  await h.store.initialize();
  h.store.changed();
  const saving = h.store.flush();
  h.writes[0].resolve({revision: 7});
  await saving;
  assert.equal(h.store.getState().revision, 8);
});

test('a failed save retains the latest edits and retry uses a new sequence', async t => {
  const h = setup(t, {draft: null, revision: 3});
  await h.store.initialize();
  h.model.applicant.name = '请求中的姓名';
  h.store.changed();
  const first = h.store.flush();
  h.model.applicant.name = '失败前的新姓名';
  h.store.changed();
  h.writes[0].reject(failure());
  assert.equal(await first, false);
  assert.equal(h.statuses.at(-1).phase, 'error');
  assert.ok(h.store.getState().generation > h.store.getState().savedGeneration);
  const retrying = h.store.retry();
  assert.equal(h.writes.length, 2);
  assert.equal(h.writes[1].packet.draft.applicant.name, '失败前的新姓名');
  assert.equal(h.writes[1].packet.sequence, 2);
  assert.equal(h.writes[1].packet.baseRevision, 3);
  h.writes[1].resolve({revision: 4});
  assert.equal(await retrying, true);
  assert.equal(h.statuses.at(-1).phase, 'saved');
});

test('a revision conflict preserves local edits and blocks automatic writes and beacons', async t => {
  const h = setup(t);
  await h.store.initialize();
  h.model.applicant.name = '本页未保存的姓名';
  h.store.changed();
  const saving = h.store.flush();
  h.writes[0].reject(failure(409, 'draft_conflict'));
  assert.equal(await saving, false);
  assert.equal(h.store.getState().conflict, true);
  assert.equal(h.statuses.at(-1).phase, 'conflict');
  h.store.changed(true);
  assert.equal(await h.store.retry(), false);
  assert.equal(await h.store.flush(), false);
  assert.equal(h.store.beacon(() => assert.fail('conflicted drafts must not be sent')), false);
  assert.equal(h.writes.length, 1);
  assert.equal(h.model.applicant.name, '本页未保存的姓名');
});

test('an unrelated HTTP 409 remains retryable rather than becoming a revision conflict', async t => {
  const h = setup(t);
  await h.store.initialize();
  h.store.changed();
  const saving = h.store.flush();
  h.writes[0].reject(failure(409, 'another_error'));
  assert.equal(await saving, false);
  assert.equal(h.store.getState().conflict, false);
  assert.equal(h.statuses.at(-1).phase, 'error');
  const retrying = h.store.retry();
  h.writes[1].resolve({revision: 1});
  assert.equal(await retrying, true);
});

test('Beacon queueing does not acknowledge persistence and the next save has a higher sequence', async t => {
  const h = setup(t, {draft: null, revision: 5});
  await h.store.initialize();
  h.model.applicant.name = '退出前填写';
  h.store.changed();
  const sent = [];
  assert.equal(h.store.beacon((url, body) => { sent.push({url, packet: JSON.parse(body)}); return true; }), true);
  assert.equal(sent[0].url, '/api/guest/draft');
  assert.equal(sent[0].packet.sequence, 1);
  assert.equal(sent[0].packet.baseRevision, 5);
  assert.ok(h.store.getState().generation > h.store.getState().savedGeneration);
  assert.equal(h.statuses.some(s => s.phase === 'saved'), false);
  const saving = h.store.flush();
  assert.equal(h.writes[0].packet.sequence, 2);
  h.writes[0].resolve({revision: 7});
  assert.equal(await saving, true);
  assert.equal(h.store.getState().revision, 7);
});

test('a rejected or oversized Beacon leaves changes dirty for a normal save', async t => {
  const h = setup(t);
  await h.store.initialize();
  h.store.changed();
  assert.equal(h.store.beacon(() => false), false);
  h.model.applicant.name = '大'.repeat(30_000);
  h.store.changed();
  let sendCalls = 0;
  assert.equal(h.store.beacon(() => { sendCalls++; return true; }), false);
  assert.equal(sendCalls, 0, 'the payload limit must count UTF-8 bytes');
  assert.ok(h.store.getState().generation > h.store.getState().savedGeneration);
  const saving = h.store.flush();
  h.writes[0].resolve({revision: 1});
  assert.equal(await saving, true);
});

test('suspend blocks saves and resume persists the pending snapshot', async t => {
  const h = setup(t);
  await h.store.initialize();
  h.model.step = 3;
  h.store.changed();
  h.store.suspend();
  assert.equal(h.store.getState().paused, true);
  assert.equal(await h.store.flush(), false);
  assert.equal(h.store.beacon(() => assert.fail('suspended drafts must not be sent')), false);
  assert.equal(h.writes.length, 0);
  h.store.resume();
  assert.equal(h.store.getState().paused, false);
  assert.equal(h.writes.length, 1);
  assert.equal(h.writes[0].packet.draft.step, 3);
  h.writes[0].resolve({revision: 1});
  await drain();
  assert.equal(h.statuses.at(-1).phase, 'saved');
});

test('resuming during an in-flight save sends the newer edit after its acknowledgement', async t => {
  const h = setup(t);
  await h.store.initialize();
  h.store.changed();
  const saving = h.store.flush();
  h.model.applicant.name = '尚未保存的新编辑';
  h.store.changed();
  h.store.suspend();
  h.store.resume();
  assert.equal(h.writes.length, 1);
  h.writes[0].resolve({revision: 1});
  await drain();
  assert.equal(h.writes.length, 2);
  assert.equal(h.writes[1].packet.draft.applicant.name, '尚未保存的新编辑');
  h.writes[1].resolve({revision: 2});
  assert.equal(await saving, true);
});

test('complete is terminal for changes, retries, resume, and Beacon writes', async t => {
  const h = setup(t);
  await h.store.initialize();
  h.store.changed();
  h.store.complete();
  const terminal = h.store.getState();
  h.model.applicant.name = '不应再次写入';
  h.store.changed(true);
  h.store.resume();
  assert.equal(await h.store.retry(), false);
  assert.equal(await h.store.flush(), true);
  assert.equal(h.store.beacon(() => assert.fail('completed drafts must not be sent')), false);
  assert.deepEqual(h.store.getState(), terminal);
  assert.equal(h.writes.length, 0);
  assert.equal(h.statuses.at(-1).phase, 'submitted');
});

for (const outcome of ['success', 'failure']) {
  test(`a late save ${outcome} cannot replace submitted status or revive completed state`, async t => {
    const h = setup(t);
    await h.store.initialize();
    h.store.changed();
    const saving = h.store.flush();
    h.model.applicant.name = '比在途请求更新';
    h.store.changed();
    h.store.complete();
    const terminal = h.store.getState();
    if (outcome === 'success') h.writes[0].resolve({revision: 1, updatedAt: 'late-ack'});
    else h.writes[0].reject(failure());
    await saving;
    assert.deepEqual(h.store.getState(), terminal);
    assert.equal(h.statuses.at(-1).phase, 'submitted');
    assert.equal(h.writes.length, 1);
  });
}

test('the server committed response stops further writes', async t => {
  const h = setup(t);
  await h.store.initialize();
  h.store.changed();
  const saving = h.store.flush();
  h.store.changed();
  h.writes[0].resolve({committed: true});
  assert.equal(await saving, true);
  assert.equal(h.store.getState().completed, true);
  assert.equal(h.statuses.at(-1).phase, 'submitted');
  assert.equal(h.writes.length, 1);
});

test('edits made during initialization are saved when an empty session becomes ready', async t => {
  const session = deferred();
  const h = setup(t, session.promise);
  const initializing = h.store.initialize();
  h.model.applicant.name = '加载期间填写';
  h.store.changed(true);
  assert.equal(h.writes.length, 0);
  session.resolve({draft: null, revision: 0});
  assert.equal(await initializing, true);
  await drain();
  assert.equal(h.restorations.length, 0, 'initialization must preserve edits already on the page');
  assert.equal(h.writes.length, 1, 'pre-ready edits must not remain unsaved indefinitely');
  assert.equal(h.writes[0].packet.draft.applicant.name, '加载期间填写');
  h.writes[0].resolve({revision: 1});
  await drain();
  assert.equal(h.statuses.at(-1).phase, 'saved');
});

test('an existing server draft discovered after local edits becomes a conflict', async t => {
  const session = deferred();
  const h = setup(t, session.promise);
  const initializing = h.store.initialize();
  h.model.applicant.name = '本页新编辑';
  h.store.changed(true);
  session.resolve({draft: {applicant: {name: '另一页保存'}}, revision: 8});
  assert.equal(await initializing, false);
  assert.equal(h.restorations.length, 0);
  assert.equal(h.model.applicant.name, '本页新编辑');
  assert.equal(h.store.getState().conflict, true);
  assert.equal(h.store.getState().revision, 8);
  assert.equal(await h.store.flush(), false);
  assert.equal(h.writes.length, 0);
});

test('initialization failure is visible and retry recovers the local edit', async t => {
  const session = deferred();
  const h = setup(t, session.promise);
  const initializing = h.store.initialize();
  h.model.applicant.name = '网络恢复后继续';
  h.store.changed(true);
  session.reject(failure());
  assert.equal(await initializing, false);
  assert.equal(h.statuses.at(-1).phase, 'error');
  assert.equal(h.store.getState().ready, false);
  h.setSession({draft: null, revision: 0});
  const retrying = h.store.retry();
  await drain();
  assert.equal(h.sessions.length, 2);
  assert.equal(h.writes.length, 1);
  assert.equal(h.writes[0].packet.draft.applicant.name, '网络恢复后继续');
  h.writes[0].resolve({revision: 1});
  assert.equal(await retrying, true);
});

test('a late initialization response cannot restore form data after completion', async t => {
  const session = deferred();
  const h = setup(t, session.promise);
  const initializing = h.store.initialize();
  h.store.complete();
  const terminal = h.store.getState();
  session.resolve({draft: {applicant: {name: '过期的服务器数据'}}, revision: 10});
  await initializing;
  assert.deepEqual(h.store.getState(), terminal);
  assert.equal(h.restorations.length, 0);
  assert.equal(h.statuses.at(-1).phase, 'submitted');
});
