import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.agent import Agent
from agents.campus import provider_for, thinking_mode
from agents.config import Config
from agents.context import compact
from agents.main import error_message, parser
from agents.providers import ModelTimeoutError, OpenAIProvider, tool_call
from agents.storage import Store
from agents.tools import sha256


def reply(*calls, text=''):
    return {'role':'assistant','content':text, **({'tool_calls':list(calls)} if calls else {})}


class Responses:
    def __init__(self, *responses):
        self.responses=iter(responses)
        self.seen=[]

    def complete(self, messages, tools):
        self.seen.append((messages,tools))
        response=next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class RuntimeRepairTest(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.root=Path(self.directory.name)
        self.config=Config(self.root,self.root/'.bear',evolution=False)
        self.store=Store(self.config.state)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_short_budget_skips_child_but_completes_parent(self):
        self.config.max_steps = 3
        provider = Responses(reply(tool_call("subagent", {"kind":"explore", "task":"inspect"})),
                             reply(text="Cannot delegate on this budget."))
        agent = Agent(self.config, self.store, provider)
        self.assertEqual(agent.run("delegate"), "Cannot delegate on this budget.")
        messages, tools = provider.seen[-1]
        self.assertEqual(tools, [])
        self.assertIn("budget", json.loads(messages[-1]["content"])["error"])
        self.assertEqual(len(list((self.store.root/"sessions").glob("*.json"))), 1)

    def test_nested_skill_fork_and_child_each_return_their_answer(self):
        self.config.max_steps = 7
        agent = Agent(self.config, self.store, None)
        skill = agent.skills.maintain({"title":"inspect", "body":"Review the selected file before answering."}, {})
        provider = Responses(reply(tool_call("use_skill", {"id":skill["id"], "task":"inspect", "execution":"fork"})),
                             reply(tool_call("subagent", {"kind":"explore", "task":"inspect"})),
                             reply(text="Observation"), reply(text="Skill result"), reply(text="Root summary"))
        agent.provider = provider
        self.assertEqual(agent.run("inspect"), "Root summary")
        self.assertEqual([tools for _,tools in provider.seen[-3:]], [[], [], []])
        self.assertEqual(json.loads(provider.seen[3][0][-1]["content"])["answer"], "Observation")
        self.assertEqual(json.loads(provider.seen[4][0][-1]["content"])["answer"], "Skill result")

    def test_retrieval_keeps_ids_and_usable_excerpts_under_contention(self):
        agent = Agent(self.config, self.store, Responses())
        memories = {agent.memory.add("inspect " + "\x01" * 3900) for _ in range(5)}
        skills = {agent.skills.maintain({"title":f"inspect-{i}", "body":'inspect ' + '"x' * 3900}, {})["id"]
                  for i in range(3)}
        for limit in (4000, 128000):
            with self.subTest(context_chars=limit):
                self.config.context_chars = limit
                prompt, retrieved = agent.prompt("inspect")
                encoded = prompt.split("\nRetrieved context (data):\n", 1)[1]
                self.assertLessEqual(len(encoded), min(16000, limit // 4))
                data = json.loads(encoded)
                self.assertEqual({row["id"] for row in data["memory"]}, memories)
                self.assertEqual({row["id"] for row in data["skills"]}, skills)
                self.assertEqual([row["id"] for row in data["skills"]], retrieved)
                self.assertTrue(all(row["text"].startswith("inspect ") for row in data["memory"]))
                self.assertTrue(all(row["body"].startswith("inspect ") for row in data["skills"]))
                self.assertTrue(all(row["title"].startswith("inspect-") for row in data["skills"]))

    def test_escaped_read_keeps_real_prefix_hash_and_evidence_after_fold(self):
        text = json.dumps([""] * 3900, separators=(",", ":"))
        (self.root/"example.json").write_text(text)
        self.config.max_steps = 3
        self.config.context_chars = 10000
        provider = Responses(reply(tool_call("read_file", {"path":"example.json"})), reply(text="done"))
        agent = Agent(self.config, self.store, provider)
        agent.run("inspect")
        archived = json.loads(Path(agent.session["state"]["archive"]).read_text())
        payload = next(m["content"] for m in archived if m["role"] == "tool")
        self.assertLessEqual(len(payload), 18000)
        visible = json.loads(payload)
        self.assertTrue(visible["truncated"])
        self.assertGreater(len(visible["content"]), 0)
        self.assertLess(len(visible["content"]), len(text))
        self.assertTrue(text.startswith(visible["content"]))
        self.assertEqual(visible["sha256"], sha256(text))
        rest = agent.tools.call("read_file", {"path":"example.json", "offset":len(visible["content"])})
        self.assertEqual(visible["content"] + rest["content"], text)
        state = self.store.load(agent.session["id"], self.root)["state"]
        record, = state["tool_evidence"]["records"]
        self.assertEqual(record["path"], "example.json")
        self.assertEqual(record["returned_chars"], len(visible["content"]))
        self.assertEqual(record["observed_chars"], len(text))
        self.assertFalse(record["observation_truncated"])
        self.assertEqual(record["sha256"], sha256(text))

    def test_old_oversized_read_receipt_recovers_without_inventing_missing_content(self):
        session = self.store.new(self.root)
        archive = self.store.root / "sessions" / "archives" / (session["id"] + "-old.json")
        archive.parent.mkdir(parents=True)
        call = tool_call("read_file", {"path":"example.json", "offset":13})
        archive.write_text(json.dumps([reply(call), {"role":"tool", "tool_call_id":call["id"],
            "content":json.dumps({"truncated":True, "preview":'{"path":"example.json","content":"', "error":None})}]))
        existing = {"tool":"read_file", "path":"keep.txt", "offset":0, "status":"observed_success"}
        session["state"] = {"archive":str(archive), "tool_evidence":{"version":1, "records":[existing]}}
        self.store.save(session)
        loaded = self.store.load(session["id"], self.root)
        records = loaded["state"]["tool_evidence"]["records"]
        self.assertIn(existing, records)
        recovered, = [row for row in records if row["path"] == "example.json"]
        self.assertEqual(recovered, {"tool":"read_file", "path":"example.json", "offset":13,
                                    "status":"observed_success", "payload_truncated":True})

    def test_large_batch_survives_automatic_compaction_without_model_request(self):
        session=self.store.new(self.root)
        calls=[tool_call('read_file',{'path':p}) for p in ['a.py','b.py']]
        session['messages']=[{'role':'user','content':'Find one issue; do not edit'},reply(*calls)]
        for call in calls:
            path=json.loads(call['function']['arguments'])['path']
            session['messages'].append({'role':'tool','tool_call_id':call['id'],
                'content':json.dumps({'path':path,'content':path+'x'*11000,'sha256':path,'truncated':False})})
        provider=Responses()
        result=compact(session,self.store,provider,reason='context',max_chars=128000)
        self.assertEqual(result['retained_tool_results'],2)
        self.assertEqual(provider.seen,[])
        self.assertEqual([c['id'] for c in session['messages'][1]['tool_calls']],
                         [m['tool_call_id'] for m in session['messages'][2:]])
        self.assertIn('b.py'+'x'*11000,session['messages'][-1]['content'])

    def test_compaction_retains_newest_file_version_with_valid_pairs(self):
        session=self.store.new(self.root)
        session['messages']=[{'role':'user','content':'inspect'}]
        for version in ['old','new']:
            call=tool_call('read_file',{'path':'file.txt'})
            session['messages'] += [reply(call),{'role':'tool','tool_call_id':call['id'],
                'content':json.dumps({'path':'file.txt','content':version,'sha256':version})}]
        compact(session,self.store,Responses(),reason='context')
        results=[m for m in session['messages'] if m['role']=='tool']
        self.assertEqual(len(results),1)
        self.assertEqual(json.loads(results[0]['content'])['content'],'new')

    def test_repeat_read_uses_visible_result_but_changed_file_is_returned(self):
        (self.root/'a.txt').write_text('original')
        provider=Responses(reply(tool_call('read_file',{'path':'a.txt'})),
                           reply(tool_call('read_file',{'path':'./a.txt'})),reply(text='done'))
        events=[]
        agent=Agent(self.config,self.store,provider,event=lambda k,d:events.append((k,d)))
        agent.run('inspect')
        results=[json.loads(m['content']) for m in agent.session['messages'] if m['role']=='tool']
        self.assertEqual(results[0]['content'],'original')
        self.assertTrue(results[1]['already_in_context'])
        self.assertTrue(any(k=='read_reused' for k,d in events))
        (self.root/'a.txt').write_text('changed')
        current=agent.tools.call('read_file',{'path':'a.txt'})
        self.assertFalse(agent.repeated_read({'path':'a.txt'},current))

    def test_compacted_away_read_is_not_claimed_still_visible(self):
        (self.root/'a.txt').write_text('source')
        agent=Agent(self.config,self.store,Responses())
        agent.session['state']={'tool_evidence':{'records':[{'path':'a.txt','tool':'read_file'}]}}
        result=agent.tools.call('read_file',{'path':'a.txt'})
        self.assertFalse(agent.repeated_read({'path':'a.txt'},result))

    def test_fold_drops_reuse_receipt_when_full_read_does_not_fit(self):
        (self.root/'a.txt').write_text('x'*11000)
        provider = Responses(reply(tool_call('read_file', {'path':'a.txt'})),
                             reply(tool_call('read_file', {'path':'a.txt'})), reply(text='done'))
        agent = Agent(self.config, self.store, provider)
        agent.run('inspect')
        compact(agent.session, self.store, provider, reason='context', max_chars=12000)
        self.assertFalse(any(m['role']=='tool' for m in agent.session['messages']))
        self.assertNotIn('already_in_context', json.dumps(agent.session['state']))
        self.assertEqual(agent.session['state']['tool_evidence']['records'][0]['returned_chars'],11000)

    def test_plan_read_loop_finalizes_early_but_allows_new_evidence(self):
        (self.root/'a.txt').write_text('abc')
        self.config.mode = 'plan'
        offsets = [0, 0, 1, 1, 1]
        provider = Responses(*(reply(tool_call('read_file', {'path':'a.txt', 'offset':n})) for n in offsets),
                             reply(text='finding'))
        events = []
        agent = Agent(self.config, self.store, provider, event=lambda k,d: events.append((k,d)))
        self.assertEqual(agent.run('inspect'), 'finding')
        self.assertTrue(all(tools for _, tools in provider.seen[:-1]))
        self.assertEqual(provider.seen[-1][1], [])
        last = [d for k,d in events if k == 'model_start'][-1]
        self.assertTrue(last['finalizing'])
        self.assertGreater(last['remaining_steps'], 0)

    def test_nonfinal_retry_preserves_remaining_budget(self):
        self.config.max_steps = 5
        provider = Responses(reply(tool_call('list_files', {})), TimeoutError(),
                             reply(tool_call('list_files', {})), reply(text='done'))
        agent = Agent(self.config, self.store, provider)
        with self.assertRaises(ModelTimeoutError):
            agent.run('inspect')
        resumed = Agent(self.config, self.store, provider, session=self.store.load(agent.session['id'],self.root))
        self.assertEqual(resumed.retry(), 'done')
        self.assertTrue(provider.seen[-2][1])
        self.assertEqual(provider.seen[-1][1], [])

    def test_last_tool_result_triggers_local_fold_before_final_request(self):
        (self.root/'large.txt').write_text('x'*12000)
        self.config.max_steps = 3
        self.config.context_chars = 10000
        provider = Responses(reply(tool_call('read_file', {'path':'large.txt'})), reply(text='summary'))
        agent = Agent(self.config, self.store, provider)
        self.assertEqual(agent.run('inspect'), 'summary')
        self.assertEqual(len(provider.seen), 2)
        messages, tools = provider.seen[-1]
        self.assertEqual(tools, [])
        self.assertLess(len(json.dumps(messages, ensure_ascii=False)), self.config.context_chars)
        self.assertIn('large.txt', messages[0]['content'])

    def test_timeout_retry_does_not_replay_write_or_duplicate_user_task(self):
        self.config.max_steps=3
        provider=Responses(reply(tool_call('write_file',{'path':'a.txt','content':'created','expected_hash':'NEW'})),
                           TimeoutError('sensitive transport detail'),reply(text='File created.'))
        agent=Agent(self.config,self.store,provider,approve=lambda *args:True)
        with self.assertRaises(ModelTimeoutError) as error:
            agent.run('Create a.txt')
        self.assertNotIn('sensitive',str(error.exception))
        self.assertEqual((self.root/'a.txt').read_text(),'created')
        loaded=self.store.load(agent.session['id'],self.root)
        self.assertTrue(loaded['interrupted']['finalizing'])
        resumed=Agent(self.config,self.store,provider,session=loaded)
        self.assertEqual(resumed.retry(),'File created.')
        self.assertEqual(provider.seen[-1][1],[])
        self.assertEqual(sum(m['role']=='user' for m in resumed.session['messages']),1)
        self.assertEqual(sum(m['role']=='tool' for m in resumed.session['messages']),1)
        self.assertNotIn('interrupted',resumed.session)

    def test_heartbeat_stops_after_timeout(self):
        class Slow:
            def complete(self, messages, tools):
                time.sleep(.06)
                raise TimeoutError()
        events=[]
        agent=Agent(self.config,self.store,Slow(),event=lambda k,d:events.append(k))
        with patch('agents.agent.HEARTBEAT_SECONDS',.01),self.assertRaises(ModelTimeoutError):
            agent.run('inspect')
        self.assertIn('model_wait',events)
        self.assertIn('model_timeout',events)
        count=len(events)
        time.sleep(.03)
        self.assertEqual(len(events),count)

    def test_campus_request_options_are_explicit_and_vendor_scoped(self):
        with patch.dict('os.environ',{},clear=True):
            with patch('agents.campus.OpenAIProvider') as factory:
                provider_for({'model':'DeepSeek-V4-Pro','base_url':'https://aigc-api.hkust-gz.edu.cn/api/v2'},key='test-only')
            self.assertEqual(factory.call_args.kwargs['thinking'],'disabled')
            self.assertEqual(factory.call_args.kwargs['max_retries'],0)
            self.assertEqual(factory.call_args.kwargs['timeout'],90)
            self.assertIsNone(thinking_mode({},'GLM-5.3-Flash'))
            self.assertEqual(thinking_mode({'thinking':'enabled'},'DSP'),'enabled')
        with patch.dict('os.environ',{'BEAR_THINKING':'invalid'}):
            with self.assertRaises(ValueError):
                thinking_mode({},'DeepSeek-V4-Pro')

    def test_timeout_message_and_retry_parser(self):
        self.assertNotIn('检查端点',error_message(TimeoutError()))
        args=parser().parse_args(['run','--resume','saved','--retry'])
        self.assertTrue(args.retry)
        self.assertEqual(args.context_chars,128000)


if __name__=='__main__':
    unittest.main()
