import json
import unittest
import context_brief

class ContextBriefBoundedJSONRegression(unittest.TestCase):
    def test_long_context_remains_parseable_and_reports_omissions(self):
        brief={"project_roots":[],"named_file_hints":[],"existing_change_paths":["src/"+str(i)+"/"+"x"*300 for i in range(30)],"existing_changes_not_listed":2,"source":"local_project_discovery_not_implementation_evidence"}
        raw=context_brief.render(brief)
        self.assertLessEqual(len(raw),6500)
        try:
            decoded=json.loads(raw)
        except json.JSONDecodeError as exc:
            self.fail("Bounded context is invalid JSON: "+str(exc))
        self.assertTrue(decoded["context_truncated"])
        self.assertEqual(len(decoded["existing_change_paths"])+decoded["existing_changes_not_listed"],32)
        self.assertEqual(len(brief["existing_change_paths"]),30)
