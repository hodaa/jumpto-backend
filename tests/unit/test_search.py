"""Unit tests for SearchService."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.models import TranscriptWord
from app.repositories import TranscriptWordRepository
from app.services.search import SearchService


@pytest.fixture
def mock_transcript_repo() -> MagicMock:
    """Create a mock TranscriptWordRepository."""
    repo = MagicMock(spec=TranscriptWordRepository)
    repo.search_exact_phrase = AsyncMock()
    return repo


@pytest.fixture
def search_service(mock_transcript_repo: MagicMock) -> SearchService:
    """Create SearchService with mock repository."""
    return SearchService(mock_transcript_repo)


class TestNormalizeWord:
    """Tests for word normalization."""

    def test_lowercase(self) -> None:
        assert SearchService.normalize_word("HELLO") == "hello"

    def test_strip_punctuation(self) -> None:
        assert SearchService.normalize_word("hello!") == "hello"
        assert SearchService.normalize_word("hello.") == "hello"
        assert SearchService.normalize_word("hello,") == "hello"
        assert SearchService.normalize_word("?hello!") == "hello"

    def test_keep_apostrophes(self) -> None:
        assert SearchService.normalize_word("don't") == "don't"
        assert SearchService.normalize_word("can't") == "can't"

    def test_strip_leading_trailing_apostrophes(self) -> None:
        assert SearchService.normalize_word("'hello'") == "hello"
        assert SearchService.normalize_word("'don't'") == "don't"

    def test_empty_string(self) -> None:
        assert SearchService.normalize_word("") == ""
        assert SearchService.normalize_word("!!!") == ""


class TestNormalizePhrase:
    """Tests for phrase normalization."""

    def test_single_word(self) -> None:
        result = SearchService.normalize_phrase("hello")
        assert result == ["hello"]

    def test_multi_word(self) -> None:
        result = SearchService.normalize_phrase("hello world")
        assert result == ["hello", "world"]

    def test_extra_whitespace(self) -> None:
        result = SearchService.normalize_phrase("  hello   world  ")
        assert result == ["hello", "world"]

    def test_punctuation_in_phrase(self) -> None:
        result = SearchService.normalize_phrase("hello, world!")
        assert result == ["hello", "world"]


class TestSearchSingleWord:
    """Tests for single word search."""

    @pytest.mark.asyncio
    async def test_single_word_match(
        self,
        search_service: SearchService,
        mock_transcript_repo: MagicMock,
    ) -> None:
        video_id = uuid4()
        mock_words = [
            TranscriptWord(
                id=uuid4(),
                video_id=video_id,
                word_index=0,
                word="hello",
                start_time=0.0,
                end_time=0.5,
            ),
            TranscriptWord(
                id=uuid4(),
                video_id=video_id,
                word_index=2,
                word="hello",
                start_time=2.0,
                end_time=2.5,
            ),
        ]
        mock_transcript_repo.search_exact_phrase.return_value = mock_words

        results = await search_service.search(video_id, "hello")

        assert len(results) == 2
        assert results[0].timestamp == "00:00"
        assert results[0].progress_seconds == 0.0
        assert results[1].timestamp == "00:02"
        assert results[1].progress_seconds == 2.0

    @pytest.mark.asyncio
    async def test_case_insensitive(
        self,
        search_service: SearchService,
        mock_transcript_repo: MagicMock,
    ) -> None:
        video_id = uuid4()
        mock_words = [
            TranscriptWord(
                id=uuid4(),
                video_id=video_id,
                word_index=0,
                word="Hello",
                start_time=0.0,
                end_time=0.5,
            ),
        ]
        mock_transcript_repo.search_exact_phrase.return_value = mock_words

        results = await search_service.search(video_id, "HELLO")

        assert len(results) == 1
        assert results[0].progress_seconds == 0.0

    @pytest.mark.asyncio
    async def test_no_match(
        self,
        search_service: SearchService,
        mock_transcript_repo: MagicMock,
    ) -> None:
        video_id = uuid4()
        mock_transcript_repo.search_exact_phrase.return_value = []

        results = await search_service.search(video_id, "nonexistent")

        assert results == []


class TestSearchMultiWordPhrase:
    """Tests for multi-word phrase search."""

    @pytest.mark.asyncio
    async def test_exact_consecutive_match(
        self,
        search_service: SearchService,
        mock_transcript_repo: MagicMock,
    ) -> None:
        video_id = uuid4()
        word1 = TranscriptWord(
            id=uuid4(), video_id=video_id, word_index=0, word="never", start_time=0.0, end_time=0.5
        )
        word2 = TranscriptWord(
            id=uuid4(), video_id=video_id, word_index=1, word="gonna", start_time=0.5, end_time=1.0
        )
        word3 = TranscriptWord(
            id=uuid4(), video_id=video_id, word_index=2, word="give", start_time=1.0, end_time=1.3
        )
        mock_transcript_repo.search_exact_phrase.return_value = [word1, word2, word3]

        results = await search_service.search(video_id, "never gonna give")

        assert len(results) == 1
        assert results[0].timestamp == "00:00"
        assert results[0].progress_seconds == 0.0

    @pytest.mark.asyncio
    async def test_non_consecutive_no_match(
        self,
        search_service: SearchService,
        mock_transcript_repo: MagicMock,
    ) -> None:
        video_id = uuid4()
        word1 = TranscriptWord(
            id=uuid4(), video_id=video_id, word_index=0, word="never", start_time=0.0, end_time=0.5
        )
        word2 = TranscriptWord(
            id=uuid4(), video_id=video_id, word_index=2, word="gonna", start_time=1.0, end_time=1.5
        )  # Gap!
        word3 = TranscriptWord(
            id=uuid4(), video_id=video_id, word_index=3, word="give", start_time=1.5, end_time=1.8
        )
        mock_transcript_repo.search_exact_phrase.return_value = [word1, word2, word3]

        results = await search_service.search(video_id, "never gonna give")

        assert results == []  # Non-consecutive indices

    @pytest.mark.asyncio
    async def test_out_of_order_no_match(
        self,
        search_service: SearchService,
        mock_transcript_repo: MagicMock,
    ) -> None:
        video_id = uuid4()
        word1 = TranscriptWord(
            id=uuid4(), video_id=video_id, word_index=0, word="give", start_time=0.0, end_time=0.5
        )
        word2 = TranscriptWord(
            id=uuid4(), video_id=video_id, word_index=1, word="never", start_time=0.5, end_time=1.0
        )
        word3 = TranscriptWord(
            id=uuid4(), video_id=video_id, word_index=2, word="gonna", start_time=1.0, end_time=1.5
        )
        mock_transcript_repo.search_exact_phrase.return_value = [word1, word2, word3]

        results = await search_service.search(video_id, "never gonna give")

        assert results == []  # Wrong order

    @pytest.mark.asyncio
    async def test_multiple_occurrences(
        self,
        search_service: SearchService,
        mock_transcript_repo: MagicMock,
    ) -> None:
        video_id = uuid4()
        # First occurrence
        w1 = TranscriptWord(
            id=uuid4(), video_id=video_id, word_index=0, word="never", start_time=0.0, end_time=0.5
        )
        w2 = TranscriptWord(
            id=uuid4(), video_id=video_id, word_index=1, word="gonna", start_time=0.5, end_time=1.0
        )
        # Second occurrence
        w3 = TranscriptWord(
            id=uuid4(), video_id=video_id, word_index=5, word="never", start_time=5.0, end_time=5.5
        )
        w4 = TranscriptWord(
            id=uuid4(), video_id=video_id, word_index=6, word="gonna", start_time=5.5, end_time=6.0
        )
        mock_transcript_repo.search_exact_phrase.return_value = [w1, w2, w3, w4]

        results = await search_service.search(video_id, "never gonna")

        assert len(results) == 2
        assert results[0].progress_seconds == 0.0
        assert results[1].progress_seconds == 5.0


class TestWordBoundaries:
    """Tests for word boundary matching (no partial matches)."""

    @pytest.mark.asyncio
    async def test_no_partial_match_in_larger_word(
        self,
        search_service: SearchService,
        mock_transcript_repo: MagicMock,
    ) -> None:
        video_id = uuid4()
        # "beloved" contains "love" but should not match
        word = TranscriptWord(
            id=uuid4(),
            video_id=video_id,
            word_index=0,
            word="beloved",
            start_time=0.0,
            end_time=0.5,
        )
        mock_transcript_repo.search_exact_phrase.return_value = [word]

        results = await search_service.search(video_id, "love")

        assert results == []

    @pytest.mark.asyncio
    async def test_no_partial_match_at_end(
        self,
        search_service: SearchService,
        mock_transcript_repo: MagicMock,
    ) -> None:
        video_id = uuid4()
        # "lovely" contains "love" but should not match
        word = TranscriptWord(
            id=uuid4(), video_id=video_id, word_index=0, word="lovely", start_time=0.0, end_time=0.5
        )
        mock_transcript_repo.search_exact_phrase.return_value = [word]

        results = await search_service.search(video_id, "love")

        assert results == []
