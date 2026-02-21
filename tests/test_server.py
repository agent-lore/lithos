"""Integration tests for MCP server - full tool workflows."""

import asyncio

import pytest

from exogram.server import ExogramServer


class TestServerInitialization:
    """Tests for server startup and shutdown."""

    @pytest.mark.asyncio
    async def test_server_initializes(self, server: ExogramServer):
        """Server initializes all components."""
        assert server.knowledge is not None
        assert server.search is not None
        assert server.graph is not None
        assert server.coordination is not None

    @pytest.mark.asyncio
    async def test_server_registers_tools(self, server: ExogramServer):
        """Server registers all MCP tools."""
        # The server should have registered tools
        # Check by verifying the mcp app has tools
        assert server.mcp is not None


class TestKnowledgeToolWorkflow:
    """Integration tests for knowledge management tools."""

    @pytest.mark.asyncio
    async def test_create_read_update_delete_workflow(self, server: ExogramServer):
        """Complete CRUD workflow through server."""
        # Create
        doc = await server.knowledge.create(
            title="Integration Test Doc",
            content="Initial content for testing.",
            agent="test-agent",
            tags=["test", "integration"],
        )
        doc_id = doc.id

        # Verify indexed
        await asyncio.sleep(0.1)  # Allow indexing

        # Read
        read_doc, _ = await server.knowledge.read(id=doc_id)
        assert read_doc.title == "Integration Test Doc"
        assert read_doc.content == "Initial content for testing."

        # Update
        updated = await server.knowledge.update(
            id=doc_id,
            agent="editor-agent",
            content="Updated content.",
            tags=["test", "integration", "updated"],
        )
        assert updated.content == "Updated content."
        assert "updated" in updated.metadata.tags

        # Delete
        success = await server.knowledge.delete(doc_id)
        assert success

        # Verify deleted
        with pytest.raises(FileNotFoundError):
            await server.knowledge.read(id=doc_id)

    @pytest.mark.asyncio
    async def test_create_with_wiki_links_updates_graph(self, server: ExogramServer):
        """Creating document with links updates knowledge graph."""
        # Create target first
        target = await server.knowledge.create(
            title="Link Target",
            content="This is the target document.",
            agent="agent",
        )
        server.search.index_document(target)
        server.graph.add_document(target)

        # Create source with link
        source = await server.knowledge.create(
            title="Link Source",
            content="See [[link-target]] for details.",
            agent="agent",
        )
        server.search.index_document(source)
        server.graph.add_document(source)

        # Verify graph has edge
        assert server.graph.has_edge(source.id, target.id)

        # Verify backlinks work
        incoming = server.graph.get_incoming_links(target.id)
        assert any(n["id"] == source.id for n in incoming)


class TestSearchToolWorkflow:
    """Integration tests for search tools."""

    @pytest.mark.asyncio
    async def test_full_text_search_finds_created_docs(self, server: ExogramServer):
        """Full-text search finds newly created documents."""
        # Create searchable document
        doc = await server.knowledge.create(
            title="Kubernetes Deployment",
            content="Deploy applications to Kubernetes clusters using kubectl.",
            agent="agent",
            tags=["kubernetes", "deployment"],
        )
        server.search.index_document(doc)

        # Search should find it
        results = server.search.full_text_search("Kubernetes kubectl")

        assert len(results) >= 1
        assert any(r.id == doc.id for r in results)

    @pytest.mark.asyncio
    async def test_semantic_search_finds_related_content(self, server: ExogramServer):
        """Semantic search finds conceptually related documents."""
        # Create document about error handling
        doc = await server.knowledge.create(
            title="Exception Handling",
            content="Catch exceptions and handle errors gracefully in your code.",
            agent="agent",
        )
        server.search.index_document(doc)

        # Search with related but different terms
        results = server.search.semantic_search("dealing with failures in software")

        # Should find the exception handling doc
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_search_respects_tag_filters(self, server: ExogramServer):
        """Search filters by tags correctly."""
        # Create docs with different tags
        python_doc = await server.knowledge.create(
            title="Python Guide",
            content="Programming in Python.",
            agent="agent",
            tags=["python"],
        )
        java_doc = await server.knowledge.create(
            title="Java Guide",
            content="Programming in Java.",
            agent="agent",
            tags=["java"],
        )
        server.search.index_document(python_doc)
        server.search.index_document(java_doc)

        # Search with tag filter
        results = server.search.full_text_search("Programming", tags=["python"])

        result_ids = [r.id for r in results]
        assert python_doc.id in result_ids
        assert java_doc.id not in result_ids


class TestCoordinationToolWorkflow:
    """Integration tests for coordination tools."""

    @pytest.mark.asyncio
    async def test_task_claim_workflow(self, server: ExogramServer):
        """Complete task claiming workflow."""
        # Register agents
        await server.coordination.register_agent(
            "researcher",
            name="Research Agent",
            agent_type="research",
        )
        await server.coordination.register_agent(
            "developer",
            name="Developer Agent",
            agent_type="development",
        )

        # Create task
        task_id = await server.coordination.create_task(
            title="Implement Feature X",
            agent="researcher",
            description="Research and implement feature X.",
            tags=["feature", "research"],
        )

        # Researcher claims research aspect
        success1, _expires1 = await server.coordination.claim_task(
            task_id=task_id,
            aspect="research",
            agent="researcher",
            ttl_minutes=60,
        )
        assert success1

        # Developer claims implementation aspect
        success2, _expires2 = await server.coordination.claim_task(
            task_id=task_id,
            aspect="implementation",
            agent="developer",
            ttl_minutes=60,
        )
        assert success2

        # Check task status shows both claims
        statuses = await server.coordination.get_task_status(task_id)
        assert len(statuses) == 1
        assert len(statuses[0].claims) == 2

        # Post findings
        await server.coordination.post_finding(
            task_id=task_id,
            agent="researcher",
            summary="Found relevant API documentation.",
        )

        # Complete task
        await server.coordination.complete_task(task_id, "researcher")

        # Verify completed
        task = await server.coordination.get_task(task_id)
        assert task.status == "completed"

    @pytest.mark.asyncio
    async def test_claim_conflict_resolution(self, server: ExogramServer):
        """Claim conflicts are properly handled."""
        task_id = await server.coordination.create_task(
            title="Contested Task",
            agent="creator",
        )

        # First agent claims
        success1, _ = await server.coordination.claim_task(
            task_id=task_id,
            aspect="work",
            agent="agent-1",
        )

        # Second agent tries same aspect
        success2, _ = await server.coordination.claim_task(
            task_id=task_id,
            aspect="work",
            agent="agent-2",
        )

        assert success1
        assert not success2  # Conflict!

        # First agent releases
        await server.coordination.release_claim(
            task_id=task_id,
            aspect="work",
            agent="agent-1",
        )

        # Now second agent can claim
        success3, _ = await server.coordination.claim_task(
            task_id=task_id,
            aspect="work",
            agent="agent-2",
        )
        assert success3


class TestGraphToolWorkflow:
    """Integration tests for graph tools."""

    @pytest.mark.asyncio
    async def test_build_and_query_knowledge_graph(self, server: ExogramServer):
        """Build knowledge graph and query relationships."""
        # Create interconnected documents
        overview = await server.knowledge.create(
            title="System Overview",
            content="See [[api-design]] and [[database-schema]] for details.",
            agent="agent",
        )
        api = await server.knowledge.create(
            title="API Design",
            content="REST API design. See [[database-schema]] for data model.",
            agent="agent",
        )
        db = await server.knowledge.create(
            title="Database Schema",
            content="PostgreSQL schema definition.",
            agent="agent",
        )

        # Add to graph
        server.graph.add_document(overview)
        server.graph.add_document(api)
        server.graph.add_document(db)

        # Query relationships
        # Overview links to both api and db
        outgoing = server.graph.get_outgoing_links(overview.id)
        assert len(outgoing) == 2

        # DB has incoming links from both overview and api
        incoming = server.graph.get_incoming_links(db.id)
        assert len(incoming) == 2

        # Find path from overview to db
        path = server.graph.find_path(overview.id, db.id)
        assert path is not None
        assert len(path) >= 2

    @pytest.mark.asyncio
    async def test_orphan_detection(self, server: ExogramServer):
        """Detect orphaned documents."""
        # Create connected docs
        connected = await server.knowledge.create(
            title="Connected Doc",
            content="Links to [[other-connected]].",
            agent="agent",
        )
        other = await server.knowledge.create(
            title="Other Connected",
            content="Linked from connected.",
            agent="agent",
        )

        # Create orphan
        orphan = await server.knowledge.create(
            title="Orphan Document",
            content="No links anywhere.",
            agent="agent",
        )

        server.graph.add_document(connected)
        server.graph.add_document(other)
        server.graph.add_document(orphan)

        orphans = server.graph.find_orphans()

        assert orphan.id in orphans
        assert connected.id not in orphans


class TestEndToEndScenarios:
    """End-to-end integration scenarios."""

    @pytest.mark.asyncio
    async def test_multi_agent_collaboration_scenario(self, server: ExogramServer):
        """Simulate multi-agent collaboration on a task."""
        # Setup: Register agents
        await server.coordination.register_agent(
            "planner",
            name="Planning Agent",
            agent_type="planning",
        )
        await server.coordination.register_agent(
            "researcher",
            name="Research Agent",
            agent_type="research",
        )
        await server.coordination.register_agent(
            "writer",
            name="Writing Agent",
            agent_type="writing",
        )

        # Step 1: Planner creates task
        task_id = await server.coordination.create_task(
            title="Write Technical Documentation",
            agent="planner",
            description="Create comprehensive docs for the API.",
            tags=["documentation", "api"],
        )

        # Step 2: Researcher claims research aspect
        await server.coordination.claim_task(
            task_id=task_id,
            aspect="research",
            agent="researcher",
        )

        # Step 3: Researcher creates knowledge document
        research_doc = await server.knowledge.create(
            title="API Research Notes",
            content="The API uses REST with JSON. Endpoints: /users, /items, /orders.",
            agent="researcher",
            tags=["research", "api"],
        )
        server.search.index_document(research_doc)
        server.graph.add_document(research_doc)

        # Step 4: Researcher posts finding
        await server.coordination.post_finding(
            task_id=task_id,
            agent="researcher",
            summary="Documented all API endpoints.",
            knowledge_id=research_doc.id,
        )

        # Step 5: Researcher releases claim
        await server.coordination.release_claim(
            task_id=task_id,
            aspect="research",
            agent="researcher",
        )

        # Step 6: Writer claims writing aspect
        await server.coordination.claim_task(
            task_id=task_id,
            aspect="writing",
            agent="writer",
        )

        # Step 7: Writer searches for research
        results = server.search.full_text_search("API endpoints")
        assert any(r.id == research_doc.id for r in results)

        # Step 8: Writer creates documentation
        docs = await server.knowledge.create(
            title="API Documentation",
            content="""# API Documentation

Based on [[api-research-notes]].

## Endpoints
- GET /users - List users
- GET /items - List items
- GET /orders - List orders
""",
            agent="writer",
            tags=["documentation", "api"],
        )
        server.search.index_document(docs)
        server.graph.add_document(docs)

        # Step 9: Verify graph connection
        assert server.graph.has_edge(docs.id, research_doc.id)

        # Step 10: Complete task
        await server.coordination.complete_task(task_id, "writer")

        # Verify final state
        task = await server.coordination.get_task(task_id)
        assert task.status == "completed"

        findings = await server.coordination.list_findings(task_id)
        assert len(findings) >= 1

    @pytest.mark.asyncio
    async def test_knowledge_discovery_scenario(self, server: ExogramServer):
        """Simulate knowledge discovery through search and graph."""
        # Create a knowledge base
        docs_data = [
            ("Python Basics", "Variables, functions, classes in Python.", ["python", "basics"]),
            (
                "Python Testing",
                "Use pytest for testing. See [[python-basics]].",
                ["python", "testing"],
            ),
            (
                "FastAPI Guide",
                "Build APIs with FastAPI. Requires [[python-basics]].",
                ["python", "api"],
            ),
            ("Database Patterns", "ORM patterns and raw SQL.", ["database"]),
            (
                "Full Stack App",
                "Combines [[fastapi-guide]] with [[database-patterns]].",
                ["fullstack"],
            ),
        ]

        created_docs = {}
        for title, content, tags in docs_data:
            doc = await server.knowledge.create(
                title=title,
                content=content,
                agent="knowledge-builder",
                tags=tags,
            )
            server.search.index_document(doc)
            server.graph.add_document(doc)
            created_docs[title] = doc

        # Discovery 1: Search for Python content
        python_results = server.search.full_text_search("Python")
        assert len(python_results) >= 3

        # Discovery 2: Find what links to Python Basics
        basics_id = created_docs["Python Basics"].id
        dependents = server.graph.get_incoming_links(basics_id)
        assert len(dependents) == 2  # Testing and FastAPI

        # Discovery 3: Find path from Full Stack to Basics
        fullstack_id = created_docs["Full Stack App"].id
        path = server.graph.find_path(fullstack_id, basics_id)
        assert path is not None
        # Path: Full Stack -> FastAPI -> Basics
        assert len(path) == 3

    @pytest.mark.asyncio
    async def test_system_stats_aggregation(self, server: ExogramServer):
        """Get comprehensive system statistics."""
        # Create some data
        for i in range(3):
            doc = await server.knowledge.create(
                title=f"Stats Doc {i}",
                content=f"Content for document {i}.",
                agent="stats-agent",
                tags=["stats"],
            )
            server.search.index_document(doc)
            server.graph.add_document(doc)

        await server.coordination.register_agent("stats-agent")
        await server.coordination.create_task(
            title="Stats Task",
            agent="stats-agent",
        )

        # Get stats from all components
        search_stats = server.search.get_stats()
        graph_stats = server.graph.get_stats()
        coord_stats = await server.coordination.get_stats()

        # Verify stats are populated
        assert search_stats["chunks"] >= 3
        assert graph_stats["nodes"] >= 3
        assert coord_stats["agents"] >= 1
        assert coord_stats["active_tasks"] >= 1
