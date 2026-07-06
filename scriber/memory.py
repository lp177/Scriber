"""Per-user Markdown memory files, refreshed by AI after each meeting.

Scriber keeps one concise Markdown "memory" file per Discord user under
``{data_dir}/memory/{user_id}.md``. After every meeting a participant took part
in, the file is regenerated from its previous content plus the new meeting's
summary and a transcript excerpt, so proper nouns and project names stay correct
across sessions.

This module must NOT import :mod:`scriber.summary.summarizer` at module level, to
avoid an import cycle; the summarizer instance is passed into
:meth:`MemoryManager.update_from_meeting`. It only depends on that object
exposing an ``async complete(system_prompt, user_prompt) -> str`` coroutine,
captured here by the :class:`_Completer` protocol.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Protocol

logger = logging.getLogger(__name__)

#: Per-user cap (characters) when injecting a memory file as summary context.
MEMORY_CONTEXT_CHAR_LIMIT = 4000

#: Transcript excerpt cap (characters) inside the memory-update prompt.
MEMORY_TRANSCRIPT_CHAR_LIMIT = 12000

#: System prompt driving the per-user memory refresh.
MEMORY_SYSTEM_PROMPT = (
    "You maintain a concise personal memory file, written in Markdown, about "
    "ONE meeting participant. You are given their current memory file and a new "
    "meeting they attended (its summary plus a transcript excerpt). Return the "
    "FULL updated memory file.\n"
    "Guidelines:\n"
    "- Keep it concise (target under 400 words).\n"
    "- Correct the spelling of names and project names.\n"
    "- Preserve stable facts the user may have hand-edited, unless the new "
    "meeting clearly contradicts them.\n"
    "- Use these level-2 sections, omitting any that would be empty:\n"
    '  "## Profile" (name / aliases, role),\n'
    '  "## Projects & topics",\n'
    '  "## Key facts",\n'
    '  "## Recent meetings" (append one short dated bullet per meeting, keeping '
    "only the ~8 most recent).\n"
    "Output ONLY the Markdown file content: no code fences, no preamble, no "
    "commentary."
)


class _Completer(Protocol):
    """Minimal contract required of the summarizer passed into this module."""

    async def complete(self, system_prompt: str, user_prompt: str) -> str:
        """Return a completion for the given system/user prompt pair."""
        ...


class MemoryManager:
    """Reads/writes per-user memory files and drives their AI refreshes."""

    def __init__(self, memory_dir: pathlib.Path) -> None:
        """Store the memory directory; it is created lazily on first write."""
        self._memory_dir = pathlib.Path(memory_dir)

    def path_for(self, user_id: str) -> pathlib.Path:
        """Return the memory file path for ``user_id`` (``{dir}/{user_id}.md``).

        ``user_id`` is a Discord snowflake (digits) and therefore path-safe, but
        any value containing ``/``, ``\\`` or ``..`` is rejected defensively so a
        crafted id can never escape the memory directory.

        Raises:
            ValueError: if ``user_id`` is empty or contains unsafe characters.
        """
        if not user_id or "/" in user_id or "\\" in user_id or ".." in user_id:
            raise ValueError(f"unsafe user_id: {user_id!r}")
        return self._memory_dir / f"{user_id}.md"

    def read(self, user_id: str) -> str:
        """Return the memory file's text, or ``""`` if it does not exist."""
        try:
            return self.path_for(user_id).read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def write(self, user_id: str, content: str) -> pathlib.Path:
        """Write ``content`` (utf-8) to the user's memory file; return its path."""
        path = self.path_for(user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def context_block(self, participants: list[tuple[str, str]]) -> str:
        """Build the participant-context block for a summary prompt.

        ``participants`` is a list of ``(user_id, display_name)`` pairs. For each
        participant that has a non-empty memory file, a section
        ``"### {display_name}\\n" + memory`` is appended, verbatim when within
        :data:`MEMORY_CONTEXT_CHAR_LIMIT` characters, otherwise truncated to the
        first ``MEMORY_CONTEXT_CHAR_LIMIT`` characters followed by
        ``"\\n…(truncated)"``.

        Returns:
            The concatenated block, or ``""`` if no participant has memory.
        """
        sections: list[str] = []
        for user_id, display_name in participants:
            memory = self.read(user_id)
            if not memory.strip():
                continue
            if len(memory) > MEMORY_CONTEXT_CHAR_LIMIT:
                memory = memory[:MEMORY_CONTEXT_CHAR_LIMIT] + "\n…(truncated)"
            sections.append(f"### {display_name}\n{memory}")
        return "\n\n".join(sections)

    async def update_from_meeting(
        self,
        summarizer: _Completer,
        user_id: str,
        display_name: str,
        transcript_text: str,
        summary_text: str,
        when_iso: str,
    ) -> str:
        """Regenerate one participant's memory file from a finished meeting.

        Builds a prompt from the existing memory (or a ``(none yet)``
        placeholder), the meeting date, its summary and a transcript excerpt
        truncated to :data:`MEMORY_TRANSCRIPT_CHAR_LIMIT`, asks ``summarizer`` to
        produce the full updated file, writes it and returns the new content.

        ``summarizer`` is passed in (rather than imported) to avoid an import
        cycle; it only needs an ``async complete(system_prompt, user_prompt)``.

        Raises:
            SummaryError: propagated from ``summarizer.complete`` so the caller
                can treat memory updates as best-effort.
        """
        existing = self.read(user_id) or "(none yet)"
        transcript_excerpt = transcript_text[:MEMORY_TRANSCRIPT_CHAR_LIMIT]
        if len(transcript_text) > MEMORY_TRANSCRIPT_CHAR_LIMIT:
            transcript_excerpt += "\n…(truncated)"

        user_prompt = "\n".join(
            [
                f"Participant display name: {display_name}",
                f"Meeting date (ISO8601 UTC): {when_iso}",
                "",
                "Current memory file:",
                existing,
                "",
                "New meeting summary:",
                summary_text,
                "",
                "New meeting transcript excerpt:",
                transcript_excerpt,
            ]
        )

        new_memory = await summarizer.complete(MEMORY_SYSTEM_PROMPT, user_prompt)
        self.write(user_id, new_memory)
        return new_memory
