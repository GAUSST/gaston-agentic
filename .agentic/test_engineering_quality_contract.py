"""Regression tests: both providers enforce architectural quality without changing the CLI."""
from pathlib import Path
import unittest
from unittest.mock import patch

import eng


class EngineeringQualityContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parent.parent

    def test_shared_contract_prioritizes_architecture_and_evidence(self):
        contract = (self.root / 'AGENTS.md').read_text(encoding='utf-8')
        for part in ('Quality first', 'Reason about the system before coding',
                     'Make explicit architectural decisions', 'Implement end-to-end quality',
                     'Prioritize evidence over appearance'):
            with self.subTest(part=part):
                self.assertIn(part, contract)
        self.assertNotIn('Prefer the simplest correct design', contract)

    def test_lead_and_builder_runtime_instructions_have_architecture_requirement(self):
        with patch.object(eng.workspace_git, 'repositories', return_value=[]):
            lead = eng.lead_runtime_brief()
        builder = eng.builder_runtime_brief()
        self.assertIn('system architecture', lead)
        self.assertIn('architecture boundaries', builder)
        self.assertIn('not minimum code', builder)

    def test_all_agent_prompts_preserve_architecture_quality(self):
        task = {'goal': 'Add a service feature'}
        with patch.object(eng, 'load_config', return_value={}):
            plan = eng.planning_prompt(task)
            review = eng.review_prompt(task, self.root / '.agentic', {})
        build = eng.build_prompt(task, {})
        self.assertIn('reason deeply about architecture', plan)
        self.assertIn('architecturally coherent', build)
        self.assertIn('architectural coherence', review)

    def test_independent_reviewer_enforces_architecture(self):
        review = (self.root / '.claude/skills/independent-review/SKILL.md').read_text(encoding='utf-8')
        self.assertIn('Review the end-to-end architecture', review)
        self.assertIn('do not confuse minimal edits', review)

    def test_documentation_is_project_agnostic_and_quality_first(self):
        readme = (self.root / 'README.md').read_text(encoding='utf-8')
        self.assertIn('deep architectural reasoning', readme)
        self.assertIn('project-agnostic', readme)


if __name__ == '__main__':
    unittest.main()
