# SPDX-FileCopyrightText: 2026 John Harkness
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import re
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
START_HERE = REPOSITORY_ROOT / "START_HERE.md"
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
FENCED_BLOCK = re.compile(r"```.*?```", re.DOTALL)
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
COPYABLE_PROMPT = re.compile(r"<details>.*?```text\n(.*?)\n```.*?</details>", re.DOTALL)


def _github_anchor(heading: str) -> str:
    without_markup = re.sub(r"[`*_~]", "", heading).strip().lower()
    without_punctuation = re.sub(r"[^\w\- ]", "", without_markup)
    return re.sub(r"\s+", "-", without_punctuation)


def _markdown_documents() -> list[Path]:
    return sorted(REPOSITORY_ROOT.glob("*.md")) + sorted(
        (REPOSITORY_ROOT / "docs").glob("*.md")
    )


class DocumentationTests(unittest.TestCase):
    def test_readme_first_screen_points_to_start_here(self) -> None:
        first_screen = (
            (REPOSITORY_ROOT / "README.md")
            .read_text(encoding="utf-8")
            .splitlines()[:12]
        )
        self.assertIn("[START HERE](START_HERE.md)", "\n".join(first_screen))
        self.assertTrue(START_HERE.is_file())
        self.assertFalse((REPOSITORY_ROOT / "docs" / "FIRST_CONTACT.md").exists())

    def test_optional_documents_identify_their_audience_and_start_here(self) -> None:
        references = (
            "AGENTS.md",
            "CONTRIBUTING.md",
            "docs/IMPLEMENTATION_NOTES.md",
            "PROTOCOL.md",
            "SECURITY.md",
            "docs/CODEX_TO_CLAUDE.md",
            "docs/PROJECT_JOURNAL.md",
            "docs/PUBLIC_RELEASE_CHECKLIST.md",
        )
        for relative_path in references:
            with self.subTest(document=relative_path):
                first_lines = (
                    (REPOSITORY_ROOT / relative_path)
                    .read_text(encoding="utf-8")
                    .splitlines()[:12]
                )
                introduction = "\n".join(first_lines)
                self.assertIn("**Audience:**", introduction)
                self.assertIn("START HERE", introduction)

    def test_start_here_requires_no_manual_path_substitution(self) -> None:
        content = START_HERE.read_text(encoding="utf-8")
        self.assertNotIn("PROJECT_ROOT", content)
        self.assertNotIn("CAM_CHECKOUT", content)
        self.assertIn("CLONED_CAM_REPO_LOCATION", content)
        self.assertIn("same operating-system account", content)
        self.assertIn("compatible POSIX computer", content)
        self.assertIn("not a placeholder you must edit", content.lower())
        self.assertIn("current working directory as the intended project", content)
        self.assertIn("Once per CAM clone", content)
        self.assertIn("For each project", content)
        self.assertIn("A replacement does not require", content)
        self.assertIn("another CAM clone", content)
        self.assertIn("Replacing an enrolled session", content)
        self.assertIn("Do not use ordinary enrollment", content)
        self.assertIn("<git-dir>/cam1/worktree-id", content)
        self.assertIn("journal.jsonl", content)
        self.assertNotIn("</br>", content)

    def test_checkout_discovery_requires_human_selection_before_execution(self) -> None:
        content = START_HERE.read_text(encoding="utf-8")
        self.assertGreaterEqual(content.count("Do not import or execute code"), 2)
        self.assertGreaterEqual(content.count("origin remote"), 2)
        self.assertGreaterEqual(content.count("full HEAD commit"), 2)
        self.assertGreaterEqual(content.count("Use CAM checkout ABSOLUTE_PATH."), 2)
        self.assertGreaterEqual(content.count("validation-profile"), 3)

    def test_copyable_prompts_preserve_bootstrap_order_and_project_cwd(self) -> None:
        content = START_HERE.read_text(encoding="utf-8")
        prompt_source = re.sub(r"(?m)^>", "", content)
        prompts = COPYABLE_PROMPT.findall(prompt_source)
        self.assertEqual(len(prompts), 2)
        for prompt in prompts:
            self.assertNotIn("CAM_CHECKOUT", prompt)
            self.assertNotIn("PROJECT_ROOT", prompt)
            self.assertNotIn("--project-root", prompt)
            self.assertNotRegex(prompt, r"(?m)^\s*cd\s")
            self.assertIn("current working directory as the intended project", prompt)
            self.assertIn("Do not import or execute code", prompt)
            self.assertIn("never fetch, pull", prompt)
            self.assertIn("START_HERE.md", prompt)
            self.assertNotIn("docs/FIRST_CONTACT.md", prompt)
            self.assertIn("doctor guidance in section 3", prompt)
            self.assertIn("sections 4, 5", prompt)
            self.assertIn(
                "do not run the intentionally nonzero PATH-discovery form", prompt
            )
            self.assertIn("Require doctor to exit zero and report ok:true", prompt)
            self.assertLess(
                prompt.index("validation-profile"),
                prompt.index("onboarding prepare"),
            )

    def test_all_local_markdown_links_and_fragments_resolve(self) -> None:
        failures: list[str] = []
        for document in _markdown_documents():
            content = FENCED_BLOCK.sub("", document.read_text(encoding="utf-8"))
            for raw_target in MARKDOWN_LINK.findall(content):
                target = raw_target.strip().strip("<>")
                split = urlsplit(target)
                if split.scheme or split.netloc:
                    continue
                linked_path = (
                    document
                    if not split.path
                    else (document.parent / unquote(split.path)).resolve()
                )
                if not linked_path.exists():
                    failures.append(
                        f"{document.relative_to(REPOSITORY_ROOT)} -> {target}"
                    )
                    continue
                if not split.fragment or not linked_path.is_file():
                    continue
                linked_content = linked_path.read_text(encoding="utf-8")
                anchors = {
                    _github_anchor(value) for value in HEADING.findall(linked_content)
                }
                if unquote(split.fragment).lower() not in anchors:
                    failures.append(
                        f"{document.relative_to(REPOSITORY_ROOT)} -> {target} "
                        "(missing fragment)"
                    )
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
