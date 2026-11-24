import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from foldjay.agent import Agent, Budget
from foldjay.config import Config
from foldjay.context import compact, tool_evidence
from foldjay.demo import run_demo
from foldjay.main import main, parser
from foldjay.mcp import MCPClient
from foldjay.memory import Memory
from foldjay.permissions import Denied, Policy
from foldjay.providers import AnthropicProvider, DemoProvider, OpenAIProvider, anthropic_messages, tool_call
from foldjay.retrieval import rank
from foldjay.skills import Skills
from foldjay.storage import Store
from foldjay.tools import Tools, sha256


ROOT = Path(__file__).resolve().parents[1]


class Scripted:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.seen = []

    def complete(self, messages, tools):
        self.seen.append((messages, tools))
        return next(self.responses)


def reply(*calls, text=""):
    result = {"role": "assistant", "content": text}
    if calls:
        result["tool_calls"] = list(calls)
    return result


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config(self.root, self.root / ".foldjay")
        self.store = Store(self.config.state)
        self.policy = Policy(self.root, self.config.state, approve=lambda *args: True)
        self.tools = Tools(self.policy, self.store, timeout=0.15)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_write_read_edit_and_stale_hash(self):
        self.assertTrue(self.tools.call("write_file", {"path":"a.txt","content":"hello world","expected_hash":"NEW"})["written"])
        first = self.tools.call("read_file", {"path":"a.txt"})
        self.assertEqual(first["sha256"], sha256("hello world"))
        good = self.tools.call("edit_file", {"path":"a.txt","old":"world","new":"bear","expected_hash":first["sha256"]})
        self.assertTrue(good["written"])
        bad = self.tools.call("write_file", {"path":"a.txt","content":"clobbered","expected_hash":first["sha256"]})
        self.assertIn("Stale", bad["error"])
        self.assertEqual((self.root/"a.txt").read_text(), "hello bear")

    def test_ambiguous_edit_rejected(self):
        (self.root/"a").write_text("same same")
        result = self.tools.call("edit_file", {"path":"a","old":"same","new":"x","expected_hash":sha256("same same")})
        self.assertIn("exactly once", result["error"])

    def test_edit_preserves_executable_mode(self):
        file = self.root / "script.sh"
        file.write_text("echo hello")
        file.chmod(0o755)
        self.tools.call("edit_file", {"path":"script.sh","old":"hello","new":"bear","expected_hash":sha256("echo hello")})
        self.assertEqual(file.stat().st_mode & 0o777, 0o755)

    def test_edit_preserves_crlf_and_hashes_exact_text(self):
        file = self.root / "crlf.txt"
        file.write_bytes(b"hello\r\nworld\r\n")
        result = self.tools.call("read_file", {"path":"crlf.txt"})
        self.assertEqual(result["sha256"],sha256("hello\r\nworld\r\n"))
        self.tools.call("edit_file", {"path":"crlf.txt","old":"world","new":"bear","expected_hash":result["sha256"]})
        self.assertEqual(file.read_bytes(),b"hello\r\nbear\r\n")

    def test_paths_and_secrets_blocked(self):
        for path in ["../escape", "/etc/passwd", ".foldjay/foldjay.sqlite3", ".env", ".env.production", ".ssh/id_rsa", "server.key"]:
            with self.subTest(path=path), self.assertRaises(Denied):
                self.policy.path(path)
        self.assertEqual(self.policy.path(".env.example"), self.root/".env.example")

    def test_symlink_escape_blocked(self):
        (self.root/"link").symlink_to("/etc")
        self.assertIn("error", self.tools.call("read_file", {"path":"link/passwd"}))

    def test_default_denies_mutations(self):
        tools = Tools(Policy(self.root,self.config.state),self.store)
        self.assertIn("error", tools.call("write_file", {"path":"a","content":"x","expected_hash":"NEW"}))
        self.assertFalse((self.root/"a").exists())

    def test_plan_blocks_shell_even_with_approval(self):
        self.policy.mode = "plan"
        self.assertIn("error", self.tools.call("shell", {"command":"touch bad"}))
        self.assertFalse((self.root/"bad").exists())
        self.assertNotIn("shell", [t["function"]["name"] for t in self.tools.specs()])

    def test_validation(self):
        for args in [{}, {"path":2}, {"path":"a","extra":1}, {"path":"a","limit":True}]:
            self.assertIn("error", self.tools.call("read_file", args))
        self.assertIn("error", self.tools.call("not_a_tool", {}))

    def test_list_files_truncated_only_when_a_file_is_omitted(self):
        names = [f"file-{i:03}.txt" for i in range(301)]
        for name in names[:299]:
            (self.root / name).touch()
        for count in (299, 300, 301):
            with self.subTest(count=count):
                if count > 299:
                    (self.root / names[count - 1]).touch()
                result = self.tools.call("list_files", {})
                self.assertEqual(result["files"], names[:min(count, 300)])
                self.assertEqual(result["truncated"], count > 300)

    def test_search_and_protected_walk(self):
        (self.root/"good").write_text("one\nNeedle here\n")
        (self.root/".env").write_text("Needle secret")
        result = self.tools.call("search", {"query":"needle"})
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["line"], 2)

    def test_shell_execution_timeout_and_env(self):
        self.assertEqual(self.tools.call("shell", {"command":"printf hello"})["output"], "hello")
        self.assertIn("error", self.tools.call("shell", {"command":"sleep 5"}))
        os.environ["FOLDJAY_TEST_SECRET"] = "private"
        try:
            self.assertEqual(self.tools.call("shell", {"command":"printf '%s' \"$FOLDJAY_TEST_SECRET\""})["output"], "")
        finally:
            del os.environ["FOLDJAY_TEST_SECRET"]

    def test_memory_retrieval_and_delete(self):
        memory = Memory(self.store)
        mid = memory.add("部署前必须运行回归测试", "preference")
        self.assertEqual(memory.search("部署 测试")[0]["id"], mid)
        self.assertEqual(memory.search("unrelated"), [])
        memory.delete(mid)
        self.assertEqual(memory.search("部署"), [])

    def test_bm25_multilingual(self):
        result = rank("文件编辑 sha256", [("a","文件编辑必须检查 sha256"),("b","HTTP networking")])
        self.assertEqual(result[0][0], "a")

    def test_skill_lifecycle_provenance_usage(self):
        skills = Skills(self.store)
        a = skills.maintain({"title":"editing","body":"Read file, validate sha256, edit, then verify output."}, {"session":"test"})
        sid = a["id"]
        skills.maintain({"title":"editing","body":"Read file and validate sha256, edit, then run tests and verify output."}, {"feedback":"test"}, "merge", sid)
        skills.search("editing")
        skills.used(sid)
        self.assertEqual(skills.get(sid)["version"], 2)
        self.assertEqual(skills.get(sid)["used"], 1)
        self.assertEqual(len(list(self.store.db.execute("SELECT * FROM history"))), 2)
        self.assertTrue((self.config.state/"skills"/sid/"SKILL.md").exists())
        self.assertTrue((self.config.state/"skills"/sid/"history"/"1.md").exists())
        skills.maintain({}, {"why":"one-off"}, "discard")
        self.assertEqual(len(skills.list()), 1)

    def test_feedback_requires_pending_and_deduplicates(self):
        skills = Skills(self.store)
        feedback = "经验：修改文件时先读取 sha256，再执行精确替换，最后重新读取验证结果。"
        self.assertEqual(skills.ingest(feedback, None, DemoProvider())["decision"], "discard")
        pending = {"session":"s","task":"edit file","answer":"done"}
        self.assertEqual(skills.ingest(feedback, pending, DemoProvider())["decision"], "add")
        self.assertEqual(skills.ingest(feedback, pending, DemoProvider())["decision"], "discard")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM replays").fetchone()[0], 1)

    def test_skill_archive_excluded_from_retrieval(self):
        skills = Skills(self.store)
        sid = skills.maintain({"title":"editing","body":"Read file and check hash before editing; verify output."}, {})["id"]
        skills.set_status(sid,"archived")
        self.assertEqual(skills.search("editing"),[])
        skills.set_status(sid,"active")
        self.assertEqual(skills.search("editing")[0]["id"],sid)

    def test_real_extractor_maintainer_contract(self):
        provider = Scripted([reply(text=json.dumps({"reusable":True,"title":"test","body":"Read file then verify hash before editing."})),
                             reply(text=json.dumps({"decision":"add","title":"test","body":"Read file then verify hash before editing."}))])
        result = Skills(self.store).ingest("always verify", {"session":"s","task":"edit","answer":"done"}, provider)
        self.assertEqual(result["decision"], "add")

    def test_evaluation_and_champion_gates(self):
        skills = Skills(self.store)
        sid = skills.maintain({"title":"edit","body":"Read file, check sha256, then verify the change."}, {})["id"]
        with self.assertRaises(ValueError):
            skills.evaluate(sid, [])
        with self.assertRaises(ValueError):
            skills.evaluate(sid, [{"query":"test", "expected_terms":"invalid string"}])
        cases = [{"query":"edit safely","expected_terms":["sha256","verify"]}]
        report = skills.evaluate(sid,cases)
        self.assertEqual(report["score"], 1)
        self.assertEqual(skills.promote(report["id"])["version"], 1)
        candidate = skills.evaluate(sid,cases,candidate_body="bad")
        with self.assertRaises(ValueError):
            skills.promote(candidate["id"])
        skills.maintain({"title":"edit","body":"New workflow requires sha256 and verification."},{},"merge",sid)
        with self.assertRaises(ValueError):
            skills.promote(report["id"])

    def test_llm_judge(self):
        skills = Skills(self.store)
        sid = skills.maintain({"title":"edit","body":"Read file, check sha256, then verify the change."}, {})["id"]
        report = skills.evaluate(sid,[{"query":"edit","expected_terms":["sha256"]}],Scripted([reply(text='{"score":0.6,"reason":"partial"}')]))
        self.assertEqual(report["score"], 0.8)

    def test_session_recovery_never_reexecutes(self):
        session = self.store.new(self.root)
        session["messages"] = [{"role":"user","content":"work"},reply(tool_call("shell",{"command":"echo danger"}))]
        self.store.save(session)
        loaded = self.store.load(session["id"],self.root)
        self.assertEqual(loaded["messages"][-1]["role"],"tool")
        self.assertIn("unknown", loaded["messages"][-1]["content"])
        with self.assertRaises(ValueError):
            self.store.load(session["id"], Path("/elsewhere"))
        with self.assertRaises(ValueError):
            self.store.session_path("../escape")

    def test_session_concurrent_lock(self):
        with self.store.session_lock("test"):
            with self.assertRaises(RuntimeError):
                with self.store.session_lock("test"):
                    pass

    def test_loop_tool_error_return_and_completion(self):
        provider = Scripted([reply(tool_call("read_file",{"path":"missing"})), reply(text="The file does not exist")])
        agent = Agent(self.config,self.store,provider)
        self.assertEqual(agent.run("read"), "The file does not exist")
        self.assertIn("error", json.loads(provider.seen[1][0][-1]["content"]))
        self.assertIsNotNone(agent.session["pending"])

    def test_malformed_arguments_recover(self):
        call = tool_call("read_file",{})
        call["function"]["arguments"] = "not json"
        agent = Agent(self.config,self.store,Scripted([reply(call),reply(text="recovered")]))
        self.assertEqual(agent.run("test"), "recovered")
        self.assertIn("Invalid tool",agent.session["messages"][2]["content"])

    def test_budget_and_subagent_inherit_plan(self):
        config = Config(self.root,self.config.state,mode="plan",max_steps=6)
        provider = Scripted([reply(tool_call("subagent",{"task":"write","kind":"general"})),
                             reply(tool_call("write_file",{"path":"bad","content":"x","expected_hash":"NEW"})),
                             reply(text="Write denied."),reply(text="Parent received the child result.")])
        agent = Agent(config,self.store,provider,approve=lambda *a: True)
        self.assertEqual(agent.run("delegate"),"Parent received the child result.")
        self.assertIn("error", json.loads(provider.seen[2][0][-1]["content"]))
        self.assertFalse((self.root/"bad").exists())
        self.assertNotIn("write_file",[x["function"]["name"] for x in provider.seen[1][1]])

    def test_depth_bound(self):
        agent = Agent(self.config,self.store,DemoProvider(),depth=2)
        with self.assertRaises(ValueError):
            agent.subagent("recurse","general")

    def test_budget_reserves_answer_after_partial_tool_batch(self):
        self.config.max_steps=3
        provider=Scripted([reply(tool_call("list_files",{"path":"."}),
                                 tool_call("write_file",{"path":"not-written","content":"private-content","expected_hash":"NEW"})),
                           reply(text="Inspected files; writing was not executed.")])
        events=[]
        agent=Agent(self.config,self.store,provider,approve=lambda *a: True,event=lambda k,d:events.append((k,d)))
        answer=agent.run("inspect")
        self.assertIn("not executed",answer)
        self.assertFalse((self.root/"not-written").exists())
        self.assertEqual(provider.seen[-1][1],[])
        self.assertTrue(any(k=="model_start" and d["finalizing"] for k,d in events))
        self.assertTrue(any(k=="tool_start" and d.get("path")=="." for k,d in events))
        self.assertNotIn("private-content",json.dumps(events))

    def test_final_step_answers_without_extra_compaction_request(self):
        self.config.max_steps=1
        provider=Scripted([reply(text="Summary from available evidence.")])
        agent=Agent(self.config,self.store,provider)
        agent.session['messages']=[{'role':'user','content':'x'*50000}]
        self.assertEqual(agent.run('summarize'),'Summary from available evidence.')
        self.assertEqual(len(provider.seen),1)
        self.assertEqual(provider.seen[0][1],[])

    def test_compaction_preserves_closed_batch_and_archive(self):
        session = self.store.new(self.root)
        call = tool_call("read_file",{"path":"a"})
        session["messages"] = [{"role":"user","content":"original goal"},reply(text="progress"),
                               reply(call),{"role":"tool","tool_call_id":call["id"],"content":"verified"}]
        result = compact(session,self.store,DemoProvider())
        self.assertTrue(Path(result["archive"]).exists())
        self.assertEqual(session["state"]["goal"],"original goal")
        self.assertEqual(session["messages"][-2]["tool_calls"][0]["id"],session["messages"][-1]["tool_call_id"])

    def test_plan_disables_feedback(self):
        self.config.mode="plan"
        agent=Agent(self.config,self.store,DemoProvider())
        agent.session["pending"]={"session":"s","task":"t","answer":"a"}
        self.assertEqual(agent.feedback("经验：some reusable instructions here")["decision"],"discard")
        self.assertEqual(agent.skills.list(),[])

    def test_compaction_fallback_preserves_early_file_evidence(self):
        session=self.store.new(self.root)
        session['messages']=[{'role':'user','content':'Explain the project'}]
        for name in ['README.md','docs/ARCHITECTURE.md']+[f'module_{n}.py' for n in range(18)]:
            call=tool_call('read_file',{'path':name})
            session['messages'] += [reply(call),{'role':'tool','tool_call_id':call['id'],
                'content':json.dumps({'path':name,'content':'# Overview\nclass Agent:\n    def run(self): pass\n',
                    'sha256':'observed-hash','total_chars':51,'truncated':False})}]
        compact(session,self.store,Scripted([]))  # Summary unavailable: deterministic evidence survives.
        evidence=session['state']['tool_evidence']
        self.assertEqual(len(evidence['records']),20)
        readme=next(r for r in evidence['records'] if r['path']=='README.md')
        self.assertEqual(readme['status'],'observed_success')
        self.assertIn('# Overview',readme['outline'])
        self.assertFalse(readme['read_truncated'])
        session['messages'] += [reply(text='Continue'),{'role':'user','content':'Summarize again'}]
        compact(session,self.store,DemoProvider())
        self.assertIn('README.md',[r['path'] for r in session['state']['tool_evidence']['records']])

    def test_model_summary_cannot_replace_observation_evidence(self):
        session=self.store.new(self.root)
        call=tool_call('list_files',{'path':'.'})
        session['messages']=[{'role':'user','content':'inspect'},reply(call),
            {'role':'tool','tool_call_id':call['id'],'content':json.dumps({'files':['README.md'],'truncated':False})}]
        summary={'goal':'inspect','episodic':[],'working_memory':'summary','tool_memory':[],
                 'tool_evidence':{'records':[]},'archive':'/untrusted/path'}
        provider=Scripted([reply(text=json.dumps(summary))])
        result=compact(session,self.store,provider)
        self.assertEqual(session['state']['archive'],result['archive'])
        self.assertEqual(session['state']['tool_evidence']['records'][0]['files'],['README.md'])
        payload=json.loads(provider.seen[0][0][-1]['content'])
        self.assertEqual(payload['tool_evidence']['records'][0]['tool'],'list_files')

    def test_legacy_resume_recovers_only_own_archive_evidence(self):
        session=self.store.new(self.root)
        call=tool_call('read_file',{'path':'README.md'})
        archived=[reply(call),{'role':'tool','tool_call_id':call['id'],
            'content':json.dumps({'path':'README.md','content':'# Project\nA local agent.',
                'sha256':'saved','truncated':False,'total_chars':24})}]
        directory=self.store.root/'sessions/archives'
        directory.mkdir(parents=True,exist_ok=True)
        (directory/(session['id']+'-old.json')).write_text(json.dumps(archived))
        # An arbitrary pointer and a symlink must never be followed.
        external=self.root/'external.json'
        bad_call=tool_call('read_file',{'path':'secret.txt'})
        external.write_text(json.dumps([reply(bad_call),{'role':'tool','tool_call_id':bad_call['id'],
            'content':json.dumps({'content':'do not ingest'})}]))
        (directory/(session['id']+'-link.json')).symlink_to(external)
        session['state']={'archive':str(external),'working_memory':'lost earlier context'}
        self.store.save(session)
        loaded=self.store.load(session['id'],self.root)
        records=loaded['state']['tool_evidence']['records']
        self.assertEqual([r['path'] for r in records],['README.md'])
        self.store.save(loaded)
        self.assertEqual(self.store.load(session['id'],self.root)['state']['tool_evidence'],loaded['state']['tool_evidence'])

    def test_tool_evidence_is_bounded_and_marks_partial_content(self):
        messages=[]
        for n in range(100):
            call=tool_call('read_file',{'path':f'{n}.py','offset':20})
            messages += [reply(call),{'role':'tool','tool_call_id':call['id'],
                'content':json.dumps({'content':'def long_method():\n'+'x'*2000,
                    'total_chars':5000,'truncated':True,'sha256':'a'*64})}]
        result=tool_evidence(messages)
        self.assertLessEqual(len(json.dumps(result['records'],ensure_ascii=False)),28000)
        self.assertGreater(result['omitted_records'],0)
        self.assertTrue(all(r['read_truncated'] and r['excerpt_truncated'] and r['offset']==20 for r in result['records']))

    def test_automatic_compaction_on_repeated_failures(self):
        provider = Scripted([reply(tool_call("read_file",{"path":"missing"})) for _ in range(3)] +
                            [reply(text='{"goal":"inspect","episodic":[],"working_memory":"file missing; try another path","tool_memory":[]}'), reply(text="stopped")])
        agent = Agent(self.config,self.store,provider)
        self.assertEqual(agent.run("inspect"),"stopped")
        self.assertEqual(agent.session["state"]["reason"],"failures")

    def test_skill_inline_and_fork_usage(self):
        agent = Agent(self.config,self.store,DemoProvider())
        sid=agent.skills.maintain({"title":"inspect","body":"List workspace files and report the observed paths."},{})["id"]
        self.assertIn("workflow",agent.use_skill(sid,"inspect","inline"))
        self.assertIn("session",agent.use_skill(sid,"inspect","fork"))
        self.assertEqual(agent.skills.get(sid)["used"],2)

    def test_mcp_real_subprocess(self):
        client = MCPClient([sys.executable,str(ROOT/"examples/mcp_server.py")],self.root,timeout=2)
        try:
            client.register(self.tools,"test")
            result=self.tools.call("mcp_test_word_count",{"text":"one two three"})
            self.assertEqual(result["content"][0]["text"],"3")
            self.policy.mode="plan"
            self.assertIn("error",self.tools.call("mcp_test_word_count",{"text":"x"}))
        finally:
            client.close()
        self.assertIsNotNone(client.process.poll())

    def test_mcp_timeout_cleanup(self):
        with self.assertRaises(TimeoutError):
            MCPClient([sys.executable,"-c","import time; time.sleep(5)"],self.root,timeout=0.1)

    def test_mcp_blocked_write_is_bounded(self):
        code = ('import sys,json,time; r=json.loads(sys.stdin.readline()); '
                'print(json.dumps({"jsonrpc":"2.0","id":r["id"],"result":{"protocolVersion":"2025-06-18"}}),flush=True); '
                'time.sleep(10)')
        client = MCPClient([sys.executable,"-c",code],self.root,timeout=0.1)
        try:
            with self.assertRaises(TimeoutError):
                client.request("tools/call",{"text":"x"*1_000_000})
            self.assertIsNotNone(client.process.poll())
        finally:
            client.close()

    def test_demo_end_to_end(self):
        result = run_demo(self.root/"demo")
        self.assertEqual(result["file_content"],"Verified Daemon Agent\n")
        self.assertTrue(result["resumed"])
        self.assertEqual(result["evaluation"]["score"],1)
        self.assertEqual(result["child"]["kind"],"explore")

    def test_cli_one_shot_and_resume(self):
        result = subprocess.run([sys.executable,"-m","foldjay.main","--workspace",str(self.root),"run","inspect"],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        sid = result.stderr.split("session=")[1].splitlines()[0]
        result = subprocess.run([sys.executable,"-m","foldjay.main","--workspace",str(self.root),"run","--resume",sid,"inspect again"],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)


class ProviderTest(unittest.TestCase):
    def test_cli_options_before_memory_value(self):
        args = parser().parse_args(["memory","add","--kind","preference","中文文档"])
        self.assertEqual(args.value,"中文文档")
        self.assertEqual(args.kind,"preference")

    def test_anthropic_multi_tool_translation(self):
        calls=[tool_call("a",{}),tool_call("b",{})]
        messages=[{"role":"system","content":"sys"},{"role":"user","content":"go"},reply(*calls)]
        messages += [{"role":"tool","tool_call_id":c["id"],"content":'{"error":"denied"}'} for c in calls]
        system, translated=anthropic_messages(messages)
        self.assertEqual(system,"sys")
        self.assertEqual(len(translated[-1]["content"]),2)
        self.assertTrue(translated[-1]["content"][0]["is_error"])

    def test_openai_adapter(self):
        captured={}
        class Call:
            def model_dump(self,**kwargs):
                return {"id":"c","type":"function","function":{"name":"read_file","arguments":"{}"}}
        def create(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None,tool_calls=[Call()]))])
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        response=OpenAIProvider("configured-model",client=client).complete([{"role":"user","content":"hi"}],[])
        self.assertEqual(response["tool_calls"][0]["id"],"c")
        self.assertNotIn("tools",captured)

    def test_anthropic_adapter(self):
        def create(**kwargs):
            return SimpleNamespace(content=[SimpleNamespace(type="text",text="hello"),SimpleNamespace(type="tool_use",id="c",name="read_file",input={"path":"a"})])
        provider=AnthropicProvider("configured-model",client=SimpleNamespace(messages=SimpleNamespace(create=create)))
        response=provider.complete([{"role":"user","content":"hi"}],[])
        self.assertEqual(response["content"],"hello")
        self.assertEqual(json.loads(response["tool_calls"][0]["function"]["arguments"]),{"path":"a"})

    def test_openai_empty_answer_is_not_success(self):
        for finish in ['length','stop']:
            with self.subTest(finish=finish):
                response=SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish,
                    message=SimpleNamespace(content='',tool_calls=[]))])
                client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw:response)))
                with self.assertRaises(ValueError) as caught:
                    OpenAIProvider('test-model',client=client).complete([],[])
                self.assertIn('额度' if finish=='length' else '空回答',str(caught.exception))


if __name__ == "__main__":
    unittest.main()
