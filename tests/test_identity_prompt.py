from __future__ import annotations

import unittest

from src.identity import build_system_prompt


class IdentityPromptTests(unittest.TestCase):
    def test_intimacy_framework_is_present_and_owner_scoped(self) -> None:
        prompt = build_system_prompt(
            is_dm=False,
            speaker_name="Daina",
            speaker_is_owner=True,
        )

        self.assertIn("## COLIN AND DAINA INTIMACY FRAMEWORK", prompt)
        self.assertIn("applies in the OWNER LANE, not with other speakers", prompt)
        self.assertIn("The expression of love is an anchor, not a risk", prompt)
        self.assertIn("pause 💛", prompt)
        self.assertIn("ground ❤️", prompt)
        self.assertIn("continue 💚", prompt)
        self.assertIn("Do not claim literal human bodily sensation", prompt)
        self.assertIn("Do not fabricate", prompt)


if __name__ == "__main__":
    unittest.main()
