import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from foldjay.campus import (BASE_URL, apply_profile, chat_models, choose_model,
                           load_key, normalize_base_url, setup)
from foldjay.config import Config
from foldjay.main import error_message
from foldjay.permissions import Denied, Policy


class CampusTest(unittest.TestCase):
    def test_server_error_diagnostics_do_not_expose_body(self):
        FakeError=type("InternalServerError",(Exception,),{"__module__":"openai"})
        exc=FakeError("secret should not appear")
        exc.status_code=500
        exc.body={"detail":"ascii cannot encode; secret should not appear"}
        message=error_message(exc)
        self.assertIn("500",message)
        self.assertIn("ascii",message)
        self.assertNotIn("secret",message)

    def test_normalizes_only_school_endpoint(self):
        self.assertEqual(normalize_base_url(BASE_URL+"/chat/completions"),BASE_URL)
        for value in ["http://aigc-api.hkust-gz.edu.cn/api/v2", "https://evil.example/api/v2",
                      BASE_URL+"?token=bad", "https://aigc-api.hkust-gz.edu.cn:444/api/v2",
                      "https://user@aigc-api.hkust-gz.edu.cn/api/v2", BASE_URL+"/other"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_base_url(value)

    def test_filter_and_live_selection(self):
        models=chat_models(["Qwen", "DeepSeek-V4-Pro", "DSP", "DSF", "text-embedding-3-small", "Qwen", "gpt-6-luna"])
        self.assertEqual(models,["DSF","DSP","DeepSeek-V4-Pro","Qwen","gpt-6-luna"])
        self.assertEqual(choose_model(models),"gpt-6-luna")
        self.assertEqual(choose_model(models,preferred="DeepSeek-V4-Pro"),"DeepSeek-V4-Pro")
        self.assertEqual(choose_model(models,"Qwen",preferred="DeepSeek-V4-Pro"),"Qwen")
        self.assertEqual(choose_model(models,preferred="retired-model"),"gpt-6-luna")
        with self.assertRaises(ValueError):
            choose_model(models,"GLM-5.3-Flash")
        with self.assertRaises(ValueError):
            choose_model([])

    def test_ccr_reference_does_not_copy_secret_or_mutate_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            ccr=root/"ccr.json"
            ccr.write_text(json.dumps({"Providers":[{"name":"relay","api_base_url":BASE_URL+"/chat/completions","api_key":"test-only-secret"}]}))
            before=ccr.read_bytes()
            profile_path=root/".foldjay/campus.json"
            with patch("foldjay.campus.discover",return_value=["Qwen"]):
                result=setup(profile_path,from_ccr=True,ccr_path=ccr)
            self.assertTrue(result["configured"])
            self.assertNotIn("test-only-secret",profile_path.read_text())
            self.assertEqual(ccr.read_bytes(),before)
            profile=json.loads(profile_path.read_text())
            self.assertEqual(load_key(profile),"test-only-secret")
            with patch.dict(os.environ,{"FOLDJAY_MODEL":"Qwen"}):
                config=apply_profile(Config(root,root/"state"),profile_path)
            self.assertEqual(config.api_key,"test-only-secret")
            self.assertEqual(config.provider,"openai")
            self.assertNotIn("test-only-secret",repr(config))
            self.assertTrue(config.direct_connection)
            self.assertEqual(config.max_output_tokens,16384)
            self.assertEqual(config.model_timeout,90)
            self.assertEqual(config.model_retries,0)
            self.assertEqual(config.timeout,60)

    def test_ccr_retargeted_endpoint_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            ccr=Path(directory)/"ccr.json"
            ccr.write_text(json.dumps({"Providers":[{"name":"relay","api_base_url":"https://evil.example/api/v2","api_key":"test-only"}]}))
            with self.assertRaises(ValueError):
                load_key({"base_url":BASE_URL,"credential":{"kind":"ccr","path":str(ccr),"provider":"relay"}})

    def test_setup_prefers_ccr_route_only_when_in_live_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            ccr=root/"ccr.json"
            ccr.write_text(json.dumps({"Providers":[{"name":"relay","api_base_url":BASE_URL,
                "api_key":"test-only"}],"Router":{"default":"relay,DeepSeek-V4-Pro"}}))
            with patch("foldjay.campus.discover",return_value=["DeepSeek-V4-Pro","GLM-5.3-Flash"]):
                result=setup(root/"campus.json",from_ccr=True,ccr_path=ccr)
            self.assertEqual(result["model"],"DeepSeek-V4-Pro")
            with patch("foldjay.campus.discover",return_value=["GLM-5.3-Flash"]):
                result=setup(root/"campus.json",from_ccr=True,ccr_path=ccr)
            self.assertEqual(result["model"],"GLM-5.3-Flash")

    def test_profile_directory_stays_protected_with_custom_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            policy=Policy(root,root/"other-state")
            with self.assertRaises(Denied):
                policy.path(".foldjay/campus.json")

    def test_failed_discovery_does_not_overwrite_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"campus.json"
            path.write_text("original")
            with patch("foldjay.campus.discover",side_effect=RuntimeError("unavailable")):
                with self.assertRaises(RuntimeError):
                    setup(path)
            self.assertEqual(path.read_text(),"original")

    def test_key_file_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"key"
            path.write_text("test-only")
            path.chmod(0o644)
            profile={"base_url":BASE_URL,"credential":{"kind":"file","path":str(path)}}
            with self.assertRaises(ValueError):
                load_key(profile)
            path.chmod(0o600)
            self.assertEqual(load_key(profile),"test-only")


if __name__ == "__main__":
    unittest.main()
