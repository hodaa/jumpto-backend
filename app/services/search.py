"""Search service for exact phrase matching in transcripts."""

import re
from collections.abc import Sequence
from uuid import UUID

from app.core.logging import get_logger
from app.models import TranscriptWord
from app.repositories import TranscriptWordRepository
from app.schemas import TimestampResult

logger = get_logger(__name__)


class SearchService:
    """Service for searching transcripts with exact phrase matching."""

    def __init__(self, transcript_repo: TranscriptWordRepository) -> None:
        self.transcript_repo = transcript_repo

    @staticmethod
    def normalize_word(word: str) -> str:
        """
        Normalize a word for matching: lowercase and strip punctuation.

        Args:
            word: Raw word string

        Returns:
            Normalized word
        """
        # Lowercase and remove punctuation (keep alphanumeric and apostrophes for contractions)
        normalized = re.sub(r"[^\w']", "", word.lower())
        # Remove leading/trailing apostrophes
        return normalized.strip("'")

    @staticmethod
    def normalize_phrase(phrase: str) -> list[str]:
        """
        Normalize a phrase into a list of normalized words.

        Args:
            phrase: Search phrase

        Returns:
            List of normalized words
        """
        words = phrase.strip().split()
        return [SearchService.normalize_word(w) for w in words if w.strip()]

    async def search(
        self,
        video_id: UUID,
        keyword: str,
    ) -> list[TimestampResult]:
        """
        Search for exact phrase in video transcript.

        Args:
            video_id: Video UUID
            keyword: Search keyword or phrase

        Returns:
            List of timestamp results
        """
        normalized_words = self.normalize_phrase(keyword)
        if not normalized_words:
            return []

        # Get candidate words from database
        candidate_words = await self.transcript_repo.search_exact_phrase(
            video_id=video_id,
            normalized_words=normalized_words,
        )

        if not candidate_words:
            logger.debug("No candidate words found", video_id=video_id, phrase=keyword)
            return []

        # Find exact consecutive matches
        matches = self._find_consecutive_matches(
            candidate_words=candidate_words,
            target_words=normalized_words,
        )

        # Build word_index -> word map for accurate snippets (windowed fetch
        # around the matches instead of loading the whole transcript).
        ordered_words = await self.transcript_repo.get_by_index_range(
            video_id,
            start_index=self._snippet_window_start(matches, len(normalized_words)),
            end_index=self._snippet_window_end(matches, len(normalized_words)),
        )
        words_by_index = {word.word_index: word.word for word in ordered_words}

        results = [
            TimestampResult(
                timestamp=self._seconds_to_timestamp(match.start_time),
                progress_seconds=match.start_time,
                text_snippet=self._build_snippet(
                    words_by_index,
                    match.word_index,
                    len(normalized_words),
                ),
            )
            for match in matches
        ]

        logger.info(
            "Search completed",
            video_id=video_id,
            phrase=keyword,
            matches=len(results),
        )
        return results

    def _find_consecutive_matches(
        self,
        candidate_words: Sequence[TranscriptWord],
        target_words: list[str],
    ) -> list[TranscriptWord]:
        """
        Find consecutive matches of target_words in candidate_words.

        Args:
            candidate_words: All transcript words matching any target word
            target_words: Normalized target phrase words

        Returns:
            List of first words of each matching sequence
        """
        if len(target_words) == 1:
            # Single word: return all matching words
            target = target_words[0]
            return [w for w in candidate_words if self.normalize_word(w.word) == target]

        # Multi-word phrase: find consecutive sequences
        matches = []
        target_len = len(target_words)

        for i in range(len(candidate_words) - target_len + 1):
            # Check if this sequence matches
            sequence = candidate_words[i : i + target_len]

            # Verify consecutive word indices
            if not self._is_consecutive(sequence):
                continue

            # Verify words match in order
            if self._words_match(sequence, target_words):
                matches.append(sequence[0])

        return matches

    @staticmethod
    def _is_consecutive(words: Sequence[TranscriptWord]) -> bool:
        """Check if words have consecutive word_index values."""
        return all(words[i].word_index == words[i - 1].word_index + 1 for i in range(1, len(words)))

    def _words_match(
        self,
        sequence: Sequence[TranscriptWord],
        target_words: list[str],
    ) -> bool:
        """Check if sequence words match target words in order."""
        for i, word in enumerate(sequence):
            if self.normalize_word(word.word) != target_words[i]:
                return False
        return True

    @staticmethod
    def _seconds_to_timestamp(seconds: float) -> str:
        """Convert seconds to MM:SS format."""
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes:02d}:{secs:02d}"

    @staticmethod
    def _build_snippet(
        words_by_index: dict,
        start_index: int,
        phrase_len: int,
        context: int = 3,
    ) -> str:
        """
        Build a text snippet around the matched phrase.

        Args:
            words_by_index: Map of word_index to normalized word
            start_index: Index of the first word of the match
            phrase_len: Number of words in the matched phrase
            context: Number of words before/after the phrase to include

        Returns:
            Text snippet string
        """
        first = max(0, start_index - context)
        last = start_index + phrase_len + context
        return " ".join(words_by_index[i] for i in range(first, last) if i in words_by_index)

    @staticmethod
    def _snippet_window_start(matches: Sequence[TranscriptWord], phrase_len: int) -> int:
        """Return the lowest word_index a snippet window must include."""
        if not matches:
            return 0
        return max(0, min(match.word_index for match in matches) - 3)

    @staticmethod
    def _snippet_window_end(matches: Sequence[TranscriptWord], phrase_len: int) -> int:
        """Return the highest word_index a snippet window must include."""
        if not matches:
            return 0
        return max(match.word_index + phrase_len + 3 for match in matches)
