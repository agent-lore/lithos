"""Tests for knowledge module - document CRUD operations."""

import pytest

from exogram.knowledge import (
    KnowledgeManager,
    generate_slug,
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
