"""Tests for knowledge module - document CRUD operations."""

import asyncio
from datetime import datetime, timezone

import pytest

from lithos.knowledge import (
    KnowledgeManager,
    KnowledgeMetadata,
    generate_slug,
    normalize_url,
    parse_wiki_links,
)


class TestWikiLinkParsing:
    """Tests for wiki-link parsing."""

    def test_simple_wiki_link(self):
        """Parse simple [[target]] links."""
        content = "See [[my-document]] for details."
        links = parse_wiki_links(content)

        assert len(links) == 1
        assert links[0].target == "my-document"
        assert links[0].display == "my-document"

    def test_aliased_wiki_link(self):
        """Parse [[target|display]] links with aliases."""
        content = "Check out [[api-guide|the API documentation]] here."
        links = parse_wiki_links(content)

        assert len(links) == 1
        assert links[0].target == "api-guide"
        assert links[0].display == "the API documentation"

    def test_multiple_links(self):
        """Parse multiple links in same content."""
        content = """See [[doc-one]] and [[doc-two|Document Two]] for more.
        Also check [[folder/doc-three]]."""
        links = parse_wiki_links(content)

        assert len(links) == 3
        assert links[0].target == "doc-one"
        assert links[1].target == "doc-two"
        assert links[1].display == "Document Two"
        assert links[2].target == "folder/doc-three"

    def test_no_links(self):
        """Handle content without links."""
        content = "This document has no wiki links at all."
        links = parse_wiki_links(content)

        assert len(links) == 0

    def test_nested_brackets_ignored(self):
        """Don't parse malformed nested brackets."""
        content = "Code example: arr[[0]] is not a link."
        links = parse_wiki_links(content)

        # Should not match array indexing
        assert len(links) == 0

    def test_link_with_path(self):
        """Parse links with subdirectory paths."""
        content = "See [[procedures/deployment-guide]] for steps."
        links = parse_wiki_links(content)

        assert len(links) == 1
        assert links[0].target == "procedures/deployment-guide"


class TestSlugGeneration:
    """Tests for slug generation from titles."""

    def test_simple_title(self):
        """Generate slug from simple title."""
        assert generate_slug("Hello World") == "hello-world"

    def test_special_characters(self):
        """Remove special characters from slug."""
        assert generate_slug("What's New in Python 3.11?") == "whats-new-in-python-311"

    def test_multiple_spaces(self):
        """Collapse multiple spaces/dashes."""
        assert generate_slug("Too   Many   Spaces") == "too-many-spaces"

    def test_unicode_characters(self):
        """Handle unicode in titles."""
        slug = generate_slug("Café & Résumé")
        assert "--" not in slug  # No double dashes
        assert slug.startswith("caf")  # Handles accents

    def test_empty_title(self):
        """Handle empty title gracefully."""
        slug = generate_slug("")
        assert slug == "untitled" or len(slug) > 0

    def test_numbers_only(self):
        """Handle numeric titles."""
        slug = generate_slug("2024")
        assert "2024" in slug


class TestKnowledgeManager:
    """Tests for KnowledgeManager CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_document(self, knowledge_manager: KnowledgeManager):
        """Create a new document with all fields."""
        doc = await knowledge_manager.create(
            title="Test Document",
            content="This is test content.",
            agent="test-agent",
            tags=["test", "example"],
            confidence=0.9,
        )

        assert doc.id is not None
        assert len(doc.id) == 36  # UUID format
        assert doc.title == "Test Document"
        assert doc.content == "This is test content."
        assert doc.metadata.author == "test-agent"
        assert "test" in doc.metadata.tags
        assert doc.metadata.confidence == 0.9
        assert doc.path.suffix == ".md"

    @pytest.mark.asyncio
    async def test_create_document_generates_uuid(self, knowledge_manager: KnowledgeManager):
        """Each document gets a unique UUID."""
        doc1 = await knowledge_manager.create(
            title="Doc One",
            content="Content one",
            agent="agent",
        )
        doc2 = await knowledge_manager.create(
            title="Doc Two",
            content="Content two",
            agent="agent",
        )

        assert doc1.id != doc2.id

    @pytest.mark.asyncio
    async def test_create_document_with_path(self, knowledge_manager: KnowledgeManager):
        """Create document in subdirectory."""
        doc = await knowledge_manager.create(
            title="Deployment Guide",
            content="Steps to deploy.",
            agent="agent",
            path="procedures",
        )

        assert "procedures" in str(doc.path)

    @pytest.mark.asyncio
    async def test_read_document_by_id(self, knowledge_manager: KnowledgeManager):
        """Read document by UUID."""
        created = await knowledge_manager.create(
            title="Readable Doc",
            content="Content to read.",
            agent="agent",
        )

        doc, truncated = await knowledge_manager.read(id=created.id)

        assert doc.id == created.id
        assert doc.title == "Readable Doc"
        assert doc.content == "Content to read."
        assert not truncated

    @pytest.mark.asyncio
    async def test_read_document_by_path(self, knowledge_manager: KnowledgeManager):
        """Read document by file path."""
        created = await knowledge_manager.create(
            title="Path Test",
            content="Find me by path.",
            agent="agent",
        )

        doc, _ = await knowledge_manager.read(path=str(created.path))

        assert doc.id == created.id

    @pytest.mark.asyncio
    async def test_read_with_truncation(self, knowledge_manager: KnowledgeManager):
        """Truncate long content when requested."""
        long_content = "A" * 10000
        created = await knowledge_manager.create(
            title="Long Doc",
            content=long_content,
            agent="agent",
        )

        doc, truncated = await knowledge_manager.read(id=created.id, max_length=100)

        assert truncated
        assert len(doc.content) == 100

    @pytest.mark.asyncio
    async def test_read_nonexistent_raises(self, knowledge_manager: KnowledgeManager):
        """Reading nonexistent document raises error."""
        with pytest.raises(FileNotFoundError):
            await knowledge_manager.read(id="nonexistent-uuid")

    @pytest.mark.asyncio
    async def test_update_document_content(self, knowledge_manager: KnowledgeManager):
        """Update document content."""
        created = await knowledge_manager.create(
            title="Original Title",
            content="Original content.",
            agent="agent-1",
        )

        updated = await knowledge_manager.update(
            id=created.id,
            agent="agent-2",
            content="Updated content.",
        )

        assert updated.content == "Updated content."
        assert updated.title == "Original Title"  # Unchanged
        assert "agent-2" in updated.metadata.contributors

    @pytest.mark.asyncio
    async def test_update_adds_contributor(self, knowledge_manager: KnowledgeManager):
        """Updating adds agent to contributors list."""
        created = await knowledge_manager.create(
            title="Collab Doc",
            content="Initial.",
            agent="author",
        )

        await knowledge_manager.update(id=created.id, agent="editor-1", content="Edit 1")
        await knowledge_manager.update(id=created.id, agent="editor-2", content="Edit 2")

        doc, _ = await knowledge_manager.read(id=created.id)

        assert "editor-1" in doc.metadata.contributors
        assert "editor-2" in doc.metadata.contributors

    @pytest.mark.asyncio
    async def test_update_preserves_original_author(self, knowledge_manager: KnowledgeManager):
        """Original author is preserved on updates."""
        created = await knowledge_manager.create(
            title="Authored Doc",
            content="By original author.",
            agent="original-author",
        )

        updated = await knowledge_manager.update(
            id=created.id,
            agent="different-agent",
            content="Modified.",
        )

        assert updated.metadata.author == "original-author"

    @pytest.mark.asyncio
    async def test_update_title_refreshes_slug_index(self, knowledge_manager: KnowledgeManager):
        """Changing title updates slug lookup index."""
        created = await knowledge_manager.create(
            title="Old Title",
            content="Slug update test.",
            agent="author",
        )

        assert knowledge_manager.get_id_by_slug("old-title") == created.id

        await knowledge_manager.update(
            id=created.id,
            agent="editor",
            title="New Title",
        )

        assert knowledge_manager.get_id_by_slug("new-title") == created.id
        assert knowledge_manager.get_id_by_slug("old-title") is None

    @pytest.mark.asyncio
    async def test_delete_document(self, knowledge_manager: KnowledgeManager):
        """Delete document removes file."""
        created = await knowledge_manager.create(
            title="To Delete",
            content="Will be deleted.",
            agent="agent",
        )

        success = await knowledge_manager.delete(created.id)

        assert success
        with pytest.raises(FileNotFoundError):
            await knowledge_manager.read(id=created.id)

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self, knowledge_manager: KnowledgeManager):
        """Deleting nonexistent document returns False."""
        success = await knowledge_manager.delete("nonexistent-id")
        assert not success

    @pytest.mark.asyncio
    async def test_list_documents(
        self, knowledge_manager: KnowledgeManager, sample_documents: list
    ):
        """List all documents with pagination."""
        # Create sample documents
        for doc_data in sample_documents:
            await knowledge_manager.create(
                title=doc_data["title"],
                content=doc_data["content"],
                agent="test-agent",
                tags=doc_data["tags"],
            )

        docs, total = await knowledge_manager.list_all(limit=3)

        assert total == len(sample_documents)
        assert len(docs) == 3

    @pytest.mark.asyncio
    async def test_list_filter_by_tags(
        self, knowledge_manager: KnowledgeManager, sample_documents: list
    ):
        """Filter documents by tags."""
        for doc_data in sample_documents:
            await knowledge_manager.create(
                title=doc_data["title"],
                content=doc_data["content"],
                agent="test-agent",
                tags=doc_data["tags"],
            )

        docs, total = await knowledge_manager.list_all(tags=["python"])

        assert total == 2  # "Python Best Practices" and "Testing Guide"
        for doc in docs:
            assert "python" in doc.metadata.tags

    @pytest.mark.asyncio
    async def test_list_filter_by_path_prefix(self, knowledge_manager: KnowledgeManager):
        """Filter documents by path prefix."""
        procedures_doc = await knowledge_manager.create(
            title="Deploy Procedure",
            content="Deployment steps.",
            agent="agent",
            path="procedures",
        )
        await knowledge_manager.create(
            title="API Guide",
            content="API details.",
            agent="agent",
            path="guides",
        )

        docs, total = await knowledge_manager.list_all(path_prefix="procedures")

        assert total == 1
        assert len(docs) == 1
        assert docs[0].id == procedures_doc.id
        assert str(docs[0].path).startswith("procedures")

    @pytest.mark.asyncio
    async def test_list_filter_by_since(self, knowledge_manager: KnowledgeManager):
        """Filter documents by updated timestamp."""
        await knowledge_manager.create(
            title="Old Note",
            content="Created first.",
            agent="agent",
        )

        cutoff = datetime.now(timezone.utc)
        await asyncio.sleep(0.02)

        new_doc = await knowledge_manager.create(
            title="New Note",
            content="Created later.",
            agent="agent",
        )

        docs, total = await knowledge_manager.list_all(since=cutoff)

        assert total == 1
        assert len(docs) == 1
        assert docs[0].id == new_doc.id

    @pytest.mark.asyncio
    async def test_get_all_tags(self, knowledge_manager: KnowledgeManager, sample_documents: list):
        """Get all tags with counts."""
        for doc_data in sample_documents:
            await knowledge_manager.create(
                title=doc_data["title"],
                content=doc_data["content"],
                agent="test-agent",
                tags=doc_data["tags"],
            )

        tags = await knowledge_manager.get_all_tags()

        assert "python" in tags
        assert tags["python"] == 2


class TestDocumentPersistence:
    """Tests for document file persistence."""

    @pytest.mark.asyncio
    async def test_document_survives_reload(self, knowledge_manager: KnowledgeManager):
        """Document can be read after manager recreation."""
        created = await knowledge_manager.create(
            title="Persistent Doc",
            content="Should survive reload.",
            agent="agent",
            tags=["persistent"],
        )
        doc_id = created.id

        # Create new manager instance
        new_manager = KnowledgeManager()
        doc, _ = await new_manager.read(id=doc_id)

        assert doc.title == "Persistent Doc"
        assert doc.content == "Should survive reload."
        assert "persistent" in doc.metadata.tags

    @pytest.mark.asyncio
    async def test_frontmatter_format(self, knowledge_manager: KnowledgeManager, test_config):
        """Verify frontmatter is properly formatted YAML."""
        import yaml

        created = await knowledge_manager.create(
            title="Frontmatter Test",
            content="Body content.",
            agent="agent",
            tags=["tag1", "tag2"],
        )

        # Read raw file
        file_path = test_config.storage.knowledge_path / created.path
        raw_content = file_path.read_text()

        # Should have frontmatter delimiters
        assert raw_content.startswith("---")
        assert "---" in raw_content[3:]  # Second delimiter

        # Extract and parse frontmatter
        parts = raw_content.split("---", 2)
        frontmatter = yaml.safe_load(parts[1])

        assert frontmatter["id"] == created.id
        assert frontmatter["title"] == "Frontmatter Test"
        assert "tag1" in frontmatter["tags"]

    @pytest.mark.asyncio
    async def test_wiki_links_preserved(self, knowledge_manager: KnowledgeManager):
        """Wiki links in content are preserved through save/load."""
        content_with_links = "See [[other-doc]] and [[folder/nested|Nested Doc]]."

        created = await knowledge_manager.create(
            title="Links Test",
            content=content_with_links,
            agent="agent",
        )

        doc, _ = await knowledge_manager.read(id=created.id)

        assert "[[other-doc]]" in doc.content
        assert "[[folder/nested|Nested Doc]]" in doc.content
        assert len(doc.links) == 2

    @pytest.mark.asyncio
    async def test_create_rejects_path_traversal(self, knowledge_manager: KnowledgeManager):
        """Create blocks path traversal outside knowledge directory."""
        with pytest.raises(ValueError, match="within knowledge directory"):
            await knowledge_manager.create(
                title="Unsafe Path",
                content="Should fail.",
                agent="agent",
                path="../outside",
            )

    @pytest.mark.asyncio
    async def test_read_rejects_path_traversal(self, knowledge_manager: KnowledgeManager):
        """Read blocks path traversal outside knowledge directory."""
        with pytest.raises(ValueError, match="within knowledge directory"):
            await knowledge_manager.read(path="../outside.md")


class TestNormalizeUrl:
    """Tests for normalize_url() canonicalization."""

    def test_lowercases_scheme_and_host(self):
        assert normalize_url("HTTPS://Example.COM/Path") == "https://example.com/Path"

    def test_removes_fragment(self):
        assert normalize_url("https://example.com/page#section") == "https://example.com/page"

    def test_removes_default_https_port(self):
        assert normalize_url("https://example.com:443/page") == "https://example.com/page"

    def test_removes_default_http_port(self):
        assert normalize_url("http://example.com:80/page") == "http://example.com/page"

    def test_preserves_non_default_port(self):
        assert normalize_url("https://example.com:8080/page") == "https://example.com:8080/page"

    def test_strips_trailing_slash_non_root(self):
        assert normalize_url("https://example.com/page/") == "https://example.com/page"

    def test_preserves_root_trailing_slash(self):
        assert normalize_url("https://example.com/") == "https://example.com/"

    def test_sorts_query_params(self):
        result = normalize_url("https://example.com/search?z=1&a=2")
        assert result == "https://example.com/search?a=2&z=1"

    def test_removes_utm_source(self):
        result = normalize_url("https://example.com/page?utm_source=twitter&ref=123")
        assert result == "https://example.com/page?ref=123"

    def test_removes_utm_medium(self):
        result = normalize_url("https://example.com/page?utm_medium=email")
        assert result == "https://example.com/page"

    def test_removes_utm_campaign(self):
        result = normalize_url("https://example.com/page?utm_campaign=launch")
        assert result == "https://example.com/page"

    def test_removes_utm_term(self):
        result = normalize_url("https://example.com/page?utm_term=test")
        assert result == "https://example.com/page"

    def test_removes_utm_content(self):
        result = normalize_url("https://example.com/page?utm_content=btn")
        assert result == "https://example.com/page"

    def test_removes_fbclid(self):
        result = normalize_url("https://example.com/page?fbclid=abc123")
        assert result == "https://example.com/page"

    def test_preserves_ref_param(self):
        result = normalize_url("https://example.com/page?ref=abc")
        assert result == "https://example.com/page?ref=abc"

    def test_rejects_ftp_scheme(self):
        with pytest.raises(ValueError, match="Only http/https"):
            normalize_url("ftp://example.com/file")

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError, match="empty or whitespace"):
            normalize_url("")

    def test_rejects_whitespace_only(self):
        with pytest.raises(ValueError, match="empty or whitespace"):
            normalize_url("   ")

    def test_rejects_no_scheme(self):
        with pytest.raises(ValueError, match="Only http/https"):
            normalize_url("example.com/page")

    def test_strips_surrounding_whitespace(self):
        result = normalize_url("  https://example.com/page  ")
        assert result == "https://example.com/page"

    def test_combined_normalization(self):
        """Multiple rules applied together."""
        result = normalize_url(
            "HTTPS://Example.COM:443/page/?utm_source=x&b=2&a=1#frag"
        )
        assert result == "https://example.com/page?a=1&b=2"

    def test_preserves_path_case(self):
        """Path is case-sensitive (unlike host)."""
        result = normalize_url("https://example.com/CamelCase/Path")
        assert result == "https://example.com/CamelCase/Path"

    def test_preserves_query_value_case(self):
        result = normalize_url("https://example.com/page?key=CamelValue")
        assert result == "https://example.com/page?key=CamelValue"


class TestSourceUrlField:
    """Tests for source_url field on KnowledgeMetadata."""

    def test_source_url_in_known_metadata_keys(self):
        """source_url is a recognised metadata key."""
        from lithos.knowledge import _KNOWN_METADATA_KEYS

        assert "source_url" in _KNOWN_METADATA_KEYS

    def test_metadata_source_url_default_none(self):
        """source_url defaults to None."""
        meta = KnowledgeMetadata(
            id="test-id",
            title="Test",
            author="agent",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        assert meta.source_url is None

    def test_to_dict_includes_source_url_when_set(self):
        """to_dict() includes source_url when it has a value."""
        meta = KnowledgeMetadata(
            id="test-id",
            title="Test",
            author="agent",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            source_url="https://example.com/page",
        )
        d = meta.to_dict()
        assert d["source_url"] == "https://example.com/page"

    def test_to_dict_omits_source_url_when_none(self):
        """to_dict() omits source_url when None."""
        meta = KnowledgeMetadata(
            id="test-id",
            title="Test",
            author="agent",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        d = meta.to_dict()
        assert "source_url" not in d

    def test_from_dict_reads_source_url(self):
        """from_dict() reads source_url from frontmatter data."""
        data = {
            "id": "test-id",
            "title": "Test",
            "author": "agent",
            "source_url": "https://example.com/page",
        }
        meta = KnowledgeMetadata.from_dict(data)
        assert meta.source_url == "https://example.com/page"

    def test_from_dict_defaults_source_url_to_none(self):
        """from_dict() defaults source_url to None when absent."""
        data = {"id": "test-id", "title": "Test", "author": "agent"}
        meta = KnowledgeMetadata.from_dict(data)
        assert meta.source_url is None

    @pytest.mark.asyncio
    async def test_round_trip_source_url(self, knowledge_manager: KnowledgeManager, test_config):
        """Write a doc with source_url, read it back, value matches."""
        created = await knowledge_manager.create(
            title="URL Doc",
            content="Has a source URL.",
            agent="agent",
            source_url="https://example.com/article",
        )

        doc, _ = await knowledge_manager.read(id=created.id)
        assert doc.metadata.source_url == "https://example.com/article"

        # Also verify raw frontmatter on disk
        import yaml

        file_path = test_config.storage.knowledge_path / created.path
        raw = file_path.read_text()
        parts = raw.split("---", 2)
        fm = yaml.safe_load(parts[1])
        assert fm["source_url"] == "https://example.com/article"

    @pytest.mark.asyncio
    async def test_existing_docs_without_source_url_load_fine(
        self, knowledge_manager: KnowledgeManager, test_config
    ):
        """Documents without source_url load without error (defaults to None)."""
        # Create doc without source_url
        created = await knowledge_manager.create(
            title="No URL Doc",
            content="No source URL.",
            agent="agent",
        )

        doc, _ = await knowledge_manager.read(id=created.id)
        assert doc.metadata.source_url is None


class TestDedupMapAndLock:
    """Tests for US-003: _source_url_to_id map, _write_lock, and _UNSET sentinel."""

    def test_unset_sentinel_exists(self):
        """Module-level _UNSET sentinel exists."""
        from lithos.knowledge import _UNSET

        assert _UNSET is not None
        # It should be a unique object, not True/False/None
        assert _UNSET is not True
        assert _UNSET is not False

    def test_manager_has_source_url_map(self, knowledge_manager: KnowledgeManager):
        """KnowledgeManager has _source_url_to_id dict."""
        assert hasattr(knowledge_manager, "_source_url_to_id")
        assert isinstance(knowledge_manager._source_url_to_id, dict)

    def test_manager_has_write_lock(self, knowledge_manager: KnowledgeManager):
        """KnowledgeManager has _write_lock asyncio.Lock."""
        assert hasattr(knowledge_manager, "_write_lock")
        assert isinstance(knowledge_manager._write_lock, asyncio.Lock)

    @pytest.mark.asyncio
    async def test_scan_populates_source_url_map(self, test_config):
        """_scan_existing populates _source_url_to_id from on-disk documents."""
        # Create a doc with source_url using a first manager
        mgr1 = KnowledgeManager()
        await mgr1.create(
            title="Scanned Doc",
            content="Content.",
            agent="agent",
            source_url="https://example.com/scanned",
        )

        # Create a new manager that scans on init
        mgr2 = KnowledgeManager()
        norm = normalize_url("https://example.com/scanned")
        assert norm in mgr2._source_url_to_id

    @pytest.mark.asyncio
    async def test_scan_normalizes_urls_on_load(self, test_config):
        """_scan_existing normalizes URLs when building the map."""
        mgr1 = KnowledgeManager()
        await mgr1.create(
            title="Case Doc",
            content="Content.",
            agent="agent",
            source_url="https://EXAMPLE.COM/Page",
        )

        mgr2 = KnowledgeManager()
        # Should be stored with normalized URL
        assert "https://example.com/Page" in mgr2._source_url_to_id

    @pytest.mark.asyncio
    async def test_scan_skips_docs_without_source_url(self, test_config):
        """_scan_existing doesn't add entries for docs without source_url."""
        mgr1 = KnowledgeManager()
        await mgr1.create(
            title="No URL",
            content="No source URL.",
            agent="agent",
        )

        mgr2 = KnowledgeManager()
        assert len(mgr2._source_url_to_id) == 0
