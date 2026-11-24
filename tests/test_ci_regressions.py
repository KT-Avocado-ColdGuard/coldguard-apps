import json
import unittest
from ci.ecr_check import check
from ci.gate import evaluate

class RegressionTests(unittest.TestCase):
    def test_ecr_retry_identity(self):
        self.assertEqual(check({"failures":[{"failureCode":"ImageNotFound"}]},"sha256:a"),"missing")
        data={"images":[{"imageManifest":json.dumps({"config":{"digest":"sha256:a"}})}]}
        self.assertEqual(check(data,"sha256:a"),"identical")
        with self.assertRaises(ValueError): check(data,"sha256:b")
        with self.assertRaises(ValueError): check({},"sha256:a")
    def test_clean_and_corrupt(self):
        self.assertEqual(evaluate({"SchemaVersion":2,"Results":[{"Target":"os"}]},[],"id")["blocked"],[])
        for row in ({}, {"Target":"os","Vulnerabilities":{}}, {"Target":"os","Vulnerabilities":[{}]}):
            with self.assertRaises(ValueError): evaluate({"SchemaVersion":2,"Results":[row]},[],"id")
